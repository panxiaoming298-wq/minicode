import { describe, expect, it } from "vitest";

import { reduceRunEvent } from "./timeline";

describe("reduceRunEvent", () => {
  it("pairs tool results with the latest matching running tool", () => {
    const started = reduceRunEvent([], {
      kind: "tool_start",
      sequence: 1,
      timestamp: 1,
      toolName: "read_file",
      input: { path: "README.md" },
    });
    const finished = reduceRunEvent(started, {
      kind: "tool_result",
      sequence: 2,
      timestamp: 2,
      toolName: "read_file",
      output: "ok",
      isError: false,
      durationMs: 12,
    });
    expect(finished[0]).toMatchObject({ type: "tool", status: "success", output: "ok", durationMs: 12 });
  });

  it("replaces streamed text with the final assistant message", () => {
    const streaming = reduceRunEvent([], {
      kind: "assistant_stream",
      sequence: 1,
      timestamp: 1,
      content: "hel",
    });
    const next = reduceRunEvent(streaming, {
      kind: "assistant_stream",
      sequence: 2,
      timestamp: 2,
      content: "lo",
    });
    const final = reduceRunEvent(next, {
      kind: "assistant",
      sequence: 3,
      timestamp: 3,
      content: "hello",
    });
    expect(final).toEqual([
      { id: "event-3", type: "assistant", content: "hello", timestamp: 3 },
    ]);
  });

  it("settles active analysis rows when the run completes", () => {
    const thinking = reduceRunEvent([], {
      kind: "thinking_status",
      sequence: 1,
      timestamp: 1,
      content: "正在分析任务和工作区上下文",
    });
    const completed = reduceRunEvent(thinking, {
      kind: "status",
      sequence: 2,
      timestamp: 2,
      status: "completed",
    });

    expect(completed).toEqual([
      {
        id: "event-1",
        type: "progress",
        content: "分析任务和工作区上下文完成",
        timestamp: 1,
        status: "complete",
      },
    ]);
  });
});
