import type { RuntimeTraceView, TimelineItem, VerificationView } from "../types";
import { traceForRun } from "./runtime";

export interface RoundProjectCheck {
  record: VerificationView;
  records: VerificationView[];
  runId: string;
  webRunId: string;
}

/** A newer failed/incomplete check must never borrow an older successful screenshot. */
export function latestProjectCheck(records: VerificationView[]): VerificationView | null {
  const latest = records.filter(record => record.verifier === "npm-build-and-browser" || record.verifier.startsWith("browser-scenario-"))
    .reduce<VerificationView | null>((previous, record) => !previous || record.createdAt >= previous.createdAt ? record : previous, null);
  return latest?.projectCheck ? latest : null;
}

/** Each declared scenario has its own receipt; only combine the same source revision. */
export function latestProjectChecks(records: VerificationView[]): VerificationView[] {
  const newest = latestProjectCheck(records);
  if (!newest) return [];
  const revision = newest.projectCheck?.candidateRevision;
  if (!revision) return [newest];
  const grouped = new Map<string, VerificationView[]>();
  for (const record of records) {
    const group = grouped.get(record.verifier) || [];
    group.push(record);
    grouped.set(record.verifier, group);
  }
  return [...grouped.values()].flatMap(group => {
    const record = latestProjectCheck(group);
    return record?.projectCheck?.candidateRevision === revision ? [record] : [];
  }).sort((a, b) => Number(a.passed) - Number(b.passed) || b.createdAt - a.createdAt);
}

/** Resolve each round from recorded ownership, never from the session's latest trace. */
export function projectChecksForRounds(rounds: Array<{ key: string; items: TimelineItem[]; runId?: string }>, traces: RuntimeTraceView[]) {
  const results = new Map<string, RoundProjectCheck>();
  const displayed = new Set<string>();
  for (const round of [...rounds].reverse()) {
    const ids = [...new Set([...round.items.map(item => item.executionId), round.runId].filter((id): id is string => Boolean(id)))];
    if (!ids.length) continue;
    const matches = ids.map(id => traceForRun(traces, id));
    const trace = matches[0];
    if (!trace || matches.some(match => match?.runId !== trace.runId) || displayed.has(trace.runId)) continue;
    const observations = [
      ...trace.plan.nodes.filter(node => node.state !== "SUPERSEDED").flatMap(node => node.result?.verificationRecords || []),
      ...trace.delivery.verificationRecords,
    ];
    const record = latestProjectCheck(observations);
    if (!record) continue;
    results.set(round.key, { record, records: latestProjectChecks(observations), runId: trace.runId, webRunId: trace.webRunId });
    displayed.add(trace.runId);
  }
  return results;
}

export function projectCoverage(record: VerificationView) {
  const check = record.projectCheck;
  const stageState = (name: string) => {
    if (!check || check.execution === "not-started") return "未检查";
    const stage = check.stages.filter(item => item.stage === name).at(-1);
    if (!stage) return "未检查";
    if (name === "browser" && check.browserErrors.length) return "未通过";
    return stage.exitCode === 0 ? "通过" : stage.exitCode === null ? "结果未确认" : "未通过";
  };
  return [
    { label: "项目构建", state: check?.scope === "browser-scenario" ? "不适用" : stageState("build") },
    { label: check?.scope === "browser-scenario" ? "浏览器场景" : "桌面与手机页面加载",
      state: check?.scope === "build-only" ? "不适用" : stageState("browser") },
    { label: "交互功能", state: check?.scope === "browser-scenario" && check.criteria?.length
      ? check.criteria.some(item => item.status === "failed") ? "未通过"
        : check.criteria.every(item => item.status === "passed") ? record.passed ? "通过" : "结果未确认" : "未检查" : "未检查" },
  ];
}

export function projectArtifactUrl(sessionId?: string | null, runId?: string | null, artifactId?: string) {
  return sessionId && runId && artifactId && /^artifact-[a-f0-9]{32}$/.test(artifactId)
    ? `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/project-artifacts/${encodeURIComponent(artifactId)}` : null;
}
