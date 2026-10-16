import type { TimelineItem } from "../types";
import type { ToolTimelineItem } from "./runSummary";

function boundary(item: ToolTimelineItem): { epoch: number; revision: number } | null {
  if (!item.input || typeof item.input !== "object" || Array.isArray(item.input)) return null;
  const input = item.input as Record<string, unknown>;
  const epoch = input.expected_epoch;
  const revision = input.expected_note_revision;
  if (typeof epoch !== "number" || !Number.isSafeInteger(epoch) || epoch < 0
    || typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 1) return null;
  return { epoch, revision };
}

interface RotationReceipt {
  epoch: number;
  note_revision: number;
  already_applied?: boolean;
  reconciled?: boolean;
  retained_tail_message_count?: number;
}

function committed(item: ToolTimelineItem): RotationReceipt | null {
  const expected = boundary(item);
  if (item.toolName !== "context_rotate" || item.status !== "success" || !expected) return null;
  try {
    const receipt = JSON.parse(item.output || "");
    const reconciled = receipt?.reconciled === true
      && receipt.requested_epoch === expected.epoch && receipt.requested_note_revision === expected.revision
      && Number.isSafeInteger(receipt.note_revision) && receipt.note_revision >= expected.revision;
    const matchingNote = receipt?.note_revision === expected.revision || reconciled;
    const matchingEpoch = receipt?.epoch === expected.epoch + 1
      || (reconciled && receipt.already_applied === true && receipt.epoch === expected.epoch);
    const valid = receipt?.status === "committed" && Number.isSafeInteger(receipt.epoch) && receipt.epoch > 0
      && Number.isSafeInteger(receipt.note_revision) && receipt.note_revision > 0
      && matchingEpoch && matchingNote && receipt.history_preserved === true
      && receipt.applies === "next_model_request" && Number.isSafeInteger(receipt.omitted_message_count)
      && receipt.omitted_message_count > 0 && typeof receipt.source_event_id === "string"
      && receipt.source_event_id.length > 0 && typeof receipt.content_hash === "string"
      && /^sha256:[a-f0-9]{64}$/.test(receipt.content_hash)
      && (receipt.retained_tail_message_count === undefined
        || Number.isSafeInteger(receipt.retained_tail_message_count) && receipt.retained_tail_message_count >= 0);
    return valid ? receipt as RotationReceipt : null;
  } catch { return null; }
}

export function hasContextRotationCommit(item: ToolTimelineItem): boolean {
  return committed(item) !== null;
}

export function contextRotationPresentation(item: ToolTimelineItem): { label: string; summary: string } | null {
  const receipt = committed(item);
  if (!receipt) return null;
  const retained = (receipt.retained_tail_message_count || 0) > 0;
  const tail = retained ? "新增内容已保留，将在后续请求中继续处理。" : "";
  if (receipt.already_applied === true) return { label: "已确认完成",
    summary: `此前已完成本次整理，已核对保存记录，没有重复提交。${tail}` };
  if (retained) return { label: "已整理，保留新增内容", summary: `已整理交接笔记覆盖的历史。${tail}` };
  if (receipt.reconciled === true) return { label: "已自动恢复", summary: "已核对最新交接状态并完成整理。" };
  return null;
}

/** A newer note can recover the same epoch, but only a committed native receipt proves it. */
export function recoveredContextRotationIds(items: TimelineItem[]): Set<string> {
  const pending = new Map<string, { item: ToolTimelineItem; revision: number }[]>();
  const recovered = new Set<string>();
  for (const item of items) {
    if (item.type === "user") { pending.clear(); continue; }
    if (item.type !== "tool" || item.toolName !== "context_rotate"
      || !item.executionId || !item.nodeId || !item.attemptId) continue;
    const expected = boundary(item);
    if (!expected) continue;
    const key = JSON.stringify([item.executionId, item.nodeId, item.attemptId, expected.epoch]);
    if (item.status === "error") {
      pending.set(key, [...(pending.get(key) || []), { item, revision: expected.revision }]);
    } else {
      const receipt = committed(item);
      if (!receipt) continue;
      // A confirmation of epoch N can only recover attempts to advance N-1.
      const committedKey = JSON.stringify([item.executionId, item.nodeId, item.attemptId, receipt.epoch - 1]);
      const remaining = (pending.get(committedKey) || []).filter(failed => {
        if (failed.revision <= receipt.note_revision && failed.item.resultOrder !== undefined
          && item.startOrder !== undefined && failed.item.resultOrder < item.startOrder) {
          recovered.add(failed.item.id);
          return false;
        }
        return true;
      });
      pending.set(committedKey, remaining);
    }
  }
  return recovered;
}
