import type { RunImage, TimelineItem } from "../types";
import { validRunImages } from "./runImages";

export type CheckState = "passed" | "failed" | "not_checked" | "not_applicable";
export interface CheckEvidence {
  toolCallId: string;
  toolName: string;
  ok: boolean;
  createdAt: number;
  outputSha256: string;
  outputPreview?: string;
  image?: RunImage;
}
export interface TaskCheck {
  status: CheckState;
  summary: string;
  evidence: CheckEvidence[];
}
export interface TaskCheckReport {
  schema: "minicode.task-checks.v1" | "minicode.task-checks.v2";
  id: string;
  createdAt: number;
  runId: string;
  nodeId: string;
  attemptId?: string;
  assessment: "model_with_native_evidence";
  functional?: TaskCheck;
  visual?: TaskCheck;
}

export function parseTaskCheckReport(value: string): TaskCheckReport | null {
  try {
    const report = JSON.parse(value);
    const legacy = report.schema === "minicode.task-checks.v1";
    if ((!legacy && report.schema !== "minicode.task-checks.v2") || report.assessment !== "model_with_native_evidence"
      || typeof report.id !== "string" || typeof report.runId !== "string" || typeof report.nodeId !== "string"
      || !Number.isFinite(report.createdAt) || report.createdAt <= 0) return null;
    if (!report.functional && !report.visual) return null;
    const states = legacy ? ["passed", "failed", "not_checked"] : ["passed", "failed", "not_checked", "not_applicable"];
    for (const category of ["functional", "visual"]) {
      const check = report[category];
      if (check === undefined && !legacy) continue;
      if (!check || !states.includes(check.status)
        || typeof check.summary !== "string" || !check.summary.trim() || !Array.isArray(check.evidence)) return null;
      const verdict = check.status === "passed" || check.status === "failed";
      if (verdict && !check.evidence.length) return null;
      if (!verdict && check.evidence.length) return null;
      for (const item of check.evidence) {
        if (typeof item.toolCallId !== "string" || typeof item.toolName !== "string" || typeof item.ok !== "boolean"
          || !Number.isFinite(item.createdAt) || typeof item.outputSha256 !== "string") return null;
        if (category === "visual" && (item.toolName !== "view_image" || !item.ok || !validRunImages([item.image]).length)) return null;
        if (category === "functional" && check.status === "passed" && !item.ok) return null;
      }
    }
    return report;
  } catch { return null; }
}

/** Only structured native receipts count; never interpret prose or a saved image as PASS. */
function latestTaskCheckRecord(items: TimelineItem[]) {
  for (let index = items.length - 1; index >= 0; index--) {
    const item = items[index];
    if (item.type !== "tool" || item.toolName !== "record_task_checks" || item.status !== "success"
      || item.nodeId && !["primary", "root"].includes(item.nodeId)) continue;
    const report = parseTaskCheckReport(item.output);
    if (report) return { item, report };
  }
  return null;
}

export function latestTaskCheckReport(items: TimelineItem[]): TaskCheckReport | null {
  return latestTaskCheckRecord(items)?.report || null;
}

export interface StepCheck extends TaskCheck {
  category: "functional" | "visual";
  recordedAt: number;
}

/** Attach each finding once, to its source step; unloaded or absent sources stay at the recording step. */
export function taskChecksForSteps(items: TimelineItem[]): Map<string, StepCheck[]> {
  const placements = new Map<string, StepCheck[]>();
  const record = latestTaskCheckRecord(items);
  if (!record) return placements;
  const { report, item: recorder } = record;
  placements.set(recorder.id, []);
  for (const category of ["functional", "visual"] as const) {
    const check = report[category];
    if (!check) continue;
    const candidates = items.filter(item => item.type === "tool"
      && item.executionId === (report.runId || recorder.executionId)
      && item.nodeId === report.nodeId
      && (item.attemptId || "") === (report.attemptId || recorder.attemptId || "")
      && check.evidence.some(evidence => evidence.toolCallId === item.toolCallId && evidence.toolName === item.toolName));
    // Reused/ambiguous call IDs must not put a finding beside the wrong operation.
    const last = candidates.at(-1);
    const anchor = last && last.type === "tool"
      && candidates.filter(item => item.type === "tool" && item.toolCallId === last.toolCallId).length === 1 ? last : recorder;
    placements.set(anchor.id, [...(placements.get(anchor.id) || []), { ...check, category, recordedAt: report.createdAt }]);
  }
  return placements;
}
