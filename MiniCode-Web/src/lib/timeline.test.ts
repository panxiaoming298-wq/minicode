import { describe, expect, it } from "vitest";

import { historyToTimeline, limitLiveTimeline, reduceRunEvent } from "./timeline";

describe("reduceRunEvent", () => {
  it("bounds live event items while preserving an explicit retention notice", () => {
    let items = reduceRunEvent([], {
      kind: "progress",
      sequence: 1,
      timestamp: 1,
      content: "one",
    });
    items = reduceRunEvent(items, {
      kind: "progress",
      sequence: 2,
      timestamp: 2,
      content: "two",
    });
    items = reduceRunEvent(items, {
      kind: "progress",
      sequence: 3,
      timestamp: 3,
      content: "three",
    });

    const limited = limitLiveTimeline(items, 2);

    expect(limited.map((item) => item.id)).toEqual([
      "event-retention-notice",
      "event-2",
      "event-3",
    ]);
  });

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
      content: "正在分析当前任务",
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
        content: "分析当前任务完成",
        timestamp: 1,
        status: "complete",
        source: "thinking",
      },
    ]);
  });

  it("shows task analysis only once across multiple model steps", () => {
    const first = reduceRunEvent([], {
      kind: "thinking_status",
      sequence: 1,
      timestamp: 1,
      step: 1,
      content: "正在分析当前任务",
    });
    const tool = reduceRunEvent(first, {
      kind: "tool_start",
      sequence: 2,
      timestamp: 2,
      step: 1,
      toolName: "web_search",
      input: { query: "C罗近况" },
    });
    const repeated = reduceRunEvent(tool, {
      kind: "thinking_status",
      sequence: 3,
      timestamp: 3,
      step: 2,
      content: "正在分析当前任务",
    });

    expect(repeated).toEqual(tool);
    expect(repeated.filter((item) => item.type === "progress" && item.source === "thinking")).toHaveLength(1);
  });

  it("restores completion-gate rejections as errors instead of answers", () => {
    expect(historyToTimeline([
      { role: "user", content: "修复任务" },
      {
        role: "assistant",
        content: "Runtime completion gate rejected delivery: verification failed",
      },
    ])).toEqual([
      { id: "history-user-0", type: "user", content: "修复任务", timestamp: 0 },
      {
        id: "history-error-1",
        type: "error",
        content: "Runtime completion gate rejected delivery: verification failed",
        timestamp: 0,
      },
    ]);
  });

  it("restores persisted tool calls and pairs results by tool use id", () => {
    expect(historyToTimeline([
      { role: "assistant_tool_call", toolUseId: "call-1", toolName: "read_file", input: { path: "README.md" } },
      { role: "tool_result", toolUseId: "call-1", toolName: "read_file", content: "# MiniCode", isError: false },
    ])).toEqual([
      {
        id: "history-tool-0",
        type: "tool",
        toolName: "read_file",
        input: { path: "README.md" },
        output: "# MiniCode",
        timestamp: 0,
        status: "success",
      },
    ]);
  });

  it("turns runtime phase events into visible progress rows", () => {
    const runtime = reduceRunEvent([], {
      kind: "runtime",
      sequence: 9,
      timestamp: 10,
      runtime: { category: "phase", phase: "verify", message: "checking evidence" },
    });
    expect(runtime).toEqual([
      {
        id: "event-9",
        type: "progress",
        content: "运行阶段 · 核验",
        timestamp: 10,
        status: "running",
        source: "runtime",
      },
    ]);
    expect(reduceRunEvent(runtime, {
      kind: "progress",
      sequence: 10,
      timestamp: 11,
      content: "Runtime phase: verify, checking evidence",
    })).toEqual(runtime);
  });

  it("shows the network research stop message instead of another tool-only state", () => {
    const runtime = reduceRunEvent([], {
      kind: "runtime",
      sequence: 11,
      timestamp: 12,
      runtime: {
        category: "guard",
        phase: "verify",
        message: "已取得足够的相关网页证据；停止继续搜索，正在整理为中文回答。",
      },
    });

    expect(runtime).toEqual([
      {
        id: "event-11",
        type: "progress",
        content: "结果检查 · 核验 — 已取得足够的相关网页证据；停止继续搜索，正在整理为中文回答。",
        timestamp: 12,
        status: "running",
        source: "runtime",
      },
    ]);
  });
});
