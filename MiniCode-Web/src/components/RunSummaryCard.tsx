import { ArrowUpRight, Check, Circle, CircleAlert, ClipboardCheck, FileCode2, LoaderCircle } from "lucide-react";

import type { RunSummary } from "../lib/runSummary";
import { verificationSummary } from "../lib/runSummary";
import { explainStopReason } from "../lib/stopReason";
import { FailureNotice } from "./FailureDetails";
import { RecoveryButton, RunStopLine, type RunRecoveryAction } from "./RunStopLine";
import { ProjectCheckDetails } from "./ProjectCheckDetails";
import { ProjectCheckCard } from "./ProjectCheckCard";
import { latestProjectChecks } from "../lib/projectChecks";
import { isRuntimeStateCheck, verificationLabel } from "../lib/verification";

interface RunSummaryCardProps {
  sessionId?: string | null;
  showProjectCheck?: boolean;
  summary: RunSummary;
  loading: boolean;
  standalone: boolean;
  onOpenChanges: () => void;
  onOpenRuntime: () => void;
  recoveryAction?: RunRecoveryAction;
  onOpenSettings?: () => void;
}

const verifierLabels: Record<string, string> = {
  "integrated-runtime-state": "运行状态检查",
  "expected-outputs": "产物检查",
  "unit-tests": "测试检查",
  "file-check": "文件检查",
  "npm-build-and-browser": "项目构建与页面检查",
};

function SummaryFacts({ summary }: { summary: RunSummary }) {
  return <dl className="summary-facts">
    {summary.changes.length > 0 && <div><dt><FileCode2 size={15} />改动记录</dt><dd>{summary.changes.length} 项资源改动</dd></div>}
    <div><dt><ClipboardCheck size={15} />最终验证</dt><dd>{summary.failure?.verificationPending
      ? "未执行（任务已中断）" : summary.records.length ? verificationSummary(summary.records) : "未记录验证结果"}</dd></div>
  </dl>;
}

function SummaryRecords({ summary, sessionId }: { summary: RunSummary; sessionId?: string | null }) {
  return <>
    {summary.changes.length > 0 && <ul className="summary-changes" aria-label="本轮资源改动">
      {summary.changes.map(path => <li key={path}><FileCode2 size={14} /><code>{path}</code></li>)}
    </ul>}
    {summary.records.length > 0 && <ul className="summary-verifications" aria-label="本轮最终验证">
      {summary.records.map((record, index) => {
        const stoppedState = summary.status === "stopped" && isRuntimeStateCheck(record) && !record.passed;
        const pending = stoppedState || record.projectCheck?.execution === "not-started";
        const resultLabel = stoppedState ? "未完成（任务已停止）" : pending ? "未开始" : record.passed ? "通过" : "未通过";
        return <li key={`${record.verifier}-${index}`} className={pending ? "is-pending" : record.passed ? "is-passed" : "is-failed"}>
          {pending ? <Circle size={15} /> : record.passed ? <Check size={15} /> : <CircleAlert size={15} />}
          <div><strong>{verifierLabels[record.verifier] || verificationLabel(record)} · {resultLabel}</strong>
            {!record.deterministic && <p>AI 复核意见；实际检查范围与证据见交付验收。</p>}
            {record.projectCheck ? <ProjectCheckDetails record={record} sessionId={sessionId} runId={summary.runId} showScreenshots={false} /> : record.details && <p>{record.verifier === "integrated-runtime-state" && record.passed
              ? "执行流程已结束，无待处理任务。此项为运行状态确认，不代表代码测试或答复内容验证。" : record.details}</p>}
          </div>
        </li>;
      })}
    </ul>}
  </>;
}

export function RunSummaryCard({ summary, sessionId, showProjectCheck = true, loading, standalone, onOpenChanges, onOpenRuntime, recoveryAction, onOpenSettings }: RunSummaryCardProps) {
  const projectChecks = latestProjectChecks(summary.records);
  const failure = summary.failure;
  if (failure?.kind === "canceled" || summary.status === "stopped" && !failure) return <RunStopLine
    elapsedMs={summary.elapsedSeconds === null ? undefined : summary.elapsedSeconds * 1000} recoveryAction={recoveryAction} onOpenRuntime={onOpenRuntime} />;
  const actionLinks = <>
    {!standalone && summary.changes.length > 0 && <button onClick={onOpenChanges}>查看工作区变更<ArrowUpRight size={14} /></button>}
    <button onClick={onOpenRuntime}>查看运行详情<ArrowUpRight size={14} /></button>
  </>;
  if (failure) return <section className="run-summary-card is-failure needs-attention" aria-label="本次任务摘要">
    <FailureNotice failure={failure} actions={<>
      <RecoveryButton action={recoveryAction} />
      {failure.kind === "provider" && onOpenSettings && <button onClick={onOpenSettings}>模型设置<ArrowUpRight size={14} /></button>}
      {actionLinks}
    </>}>
      {loading ? <p className="summary-note" role="status">正在读取本轮改动与验证记录…</p> : <>
        <SummaryFacts summary={summary} />
        <SummaryRecords summary={summary} sessionId={sessionId} />
        {showProjectCheck && projectChecks.map(record => <ProjectCheckCard key={record.verifier} record={record} sessionId={sessionId} runId={summary.runId} />)}
      </>}
    </FailureNotice>
  </section>;
  const needsAttention = summary.status === "failed" || summary.delivery === "failed";
  const confirmed = summary.status === "completed" && summary.hasTrace && !needsAttention;
  const stateOnly = summary.changes.length === 0 && summary.records.every(isRuntimeStateCheck);
  // Ordinary answers need no separate success receipt. Explicit task checks,
  // changes and failures remain visible, including missing requested evidence.
  if (confirmed && !loading && stateOnly && !summary.hasResourceChanges && !summary.hasAcceptanceRequirements && !summary.reasons.length) return null;
  const compact = confirmed && !loading;
  const checkLabel = verificationSummary(summary.records);
  const title = needsAttention ? "本次任务尚未完成" : "本次执行已结束";
  const actions = <footer>{actionLinks}</footer>;
  return <section className={`run-summary-card ${compact ? "is-compact" : ""} ${needsAttention ? "needs-attention" : ""}`} aria-label="本次任务摘要">
    <header>
      {loading ? <LoaderCircle size={18} className="spin" /> : needsAttention ? <CircleAlert size={18} /> : <ClipboardCheck size={18} />}
      <strong>{compact ? summary.changes.length ? `${summary.changes.length} 项资源改动` : checkLabel : title}</strong>
      {compact && summary.changes.length > 0 && <span className="summary-check-count">{checkLabel}</span>}
    </header>
    {loading ? <p className="summary-note" role="status">正在读取本轮改动与验证记录…</p> : <>
      {!compact && <SummaryFacts summary={summary} />}
      {!summary.records.length && <p className="summary-note">执行结束不代表验证通过，可在运行详情中继续核对。</p>}
      {summary.reasons.length > 0 && <p className="summary-reason">{[...new Set(summary.reasons.map(explainStopReason))].join("；")}</p>}
      {showProjectCheck && projectChecks.map(record => <ProjectCheckCard key={record.verifier} record={record} sessionId={sessionId} runId={summary.runId} />)}
      {(summary.changes.length > 0 || summary.records.length > 0) && <details className="summary-details">
        <summary>{stateOnly ? "查看完成记录" : "查看本轮改动与验证明细"}</summary>
        <SummaryRecords summary={summary} sessionId={sessionId} />
        {compact && actions}
      </details>}
    </>}
    {!compact && actions}
  </section>;
}
