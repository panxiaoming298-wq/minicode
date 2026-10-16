import { ArrowDown, Check, CircleAlert, CircleStop, WifiOff, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { ExecutionSnapshot, RunInfo, RunStatus, TimelineItem } from "../types";
import { advisoryFailureLabel, nodeStateLabels, progressView } from "../lib/execution";
import { AgentBadge, AgentRunSummary } from "./AgentActivity";
import "./RunActivity.css";

interface Props {
  changesBadge?: ReactNode;
  run: RunInfo | null;
  status: RunStatus;
  execution: ExecutionSnapshot | null;
  items: TimelineItem[];
  reconnecting: boolean;
  awayFromBottom: boolean;
  hasNewProgress: boolean;
  onFollowLatest: () => void;
  onCompose: () => void;
  onJump: (id: string) => void;
}

function ActivitySpinner() {
  return <svg className="run-activity-spinner" viewBox="0 0 24 24" aria-hidden="true">
    {Array.from({ length: 12 }, (_, index) => <path key={index} d="M12 2.5v3.5"
      transform={`rotate(${index * 30} 12 12)`} opacity={0.2 + index * 0.07}
      stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />)}
  </svg>;
}

export function RunActivity({ run, status, execution, items, reconnecting, awayFromBottom, hasNewProgress, onFollowLatest, onCompose, onJump, changesBadge }: Props) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const view = progressView(status, execution, items, reconnecting, run?.waitingModels, run?.pendingQuestions);
  const expanded = open && view.active;
  const awaitingApproval = Boolean(view.approval || view.needsApproval);
  const needsAttention = Boolean(view.approval || view.needsApproval || view.needsInput);
  const nodes = execution?.nodes.filter(node => node.state !== "SUPERSEDED") || [];
  const hasPlan = nodes.length > 1;

  function close() {
    setOpen(false);
    trigger.current?.focus({ preventScroll: true });
  }

  useEffect(() => { if (!view.active) setOpen(false); }, [view.active]);
  useEffect(() => {
    if (!expanded) return;
    panel.current?.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (!panel.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); close(); }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [expanded]);

  const followLabel = hasNewProgress ? "有新进展 · 回到底部" : "回到底部";
  if (!view.active && !awayFromBottom && !hasNewProgress) return null;
  return <div className={`run-activity-layer ${changesBadge && view.active ? "has-file-changes" : ""} ${awaitingApproval ? "has-pending-approval" : ""} ${run?.pendingQuestions?.length ? "has-pending-question" : ""}`}>
    <div className="run-activity-dock">
      <div className="run-activity-status-row">
      {view.active && <button ref={trigger} type="button"
        className={`run-activity-toggle ${needsAttention || reconnecting ? "needs-attention" : ""}`}
        aria-label={`查看任务进度：${view.label}`} aria-expanded={expanded} aria-controls={expanded ? panelId : undefined}
        title={`${view.label} · 点击查看进度`} onClick={() => setOpen(value => !value)}>
        {status === "stopping" ? <CircleStop size={19} aria-hidden="true" />
          : needsAttention ? <CircleAlert size={19} aria-hidden="true" />
          : reconnecting ? <WifiOff size={19} aria-hidden="true" /> : <ActivitySpinner />}
        <span>{view.label}</span>
      </button>}
      {(awayFromBottom || hasNewProgress) && <button type="button" className="new-items-button" onClick={onFollowLatest}
        aria-label={followLabel} title={followLabel}>
        <ArrowDown size={17} aria-hidden="true" />
        {!view.active && <span>{followLabel}</span>}
      </button>}
      </div>
      {view.active && changesBadge}
    </div>
    {expanded && <div ref={panel} id={panelId} className="run-activity-panel" role="region" aria-label="任务进度详情" tabIndex={-1}>
      <header><strong>{view.label}</strong><button className="icon-button" aria-label="关闭任务进度" onClick={close}><X size={15} /></button></header>
      <p className="run-activity-detail">{view.detail}</p>
      {execution && <AgentRunSummary execution={execution} />}
      {hasPlan && <>
        <div className="run-activity-plan-heading"><span>执行计划</span><span>已完成 {nodes.filter(node => node.state === "SUCCEEDED").length} / {nodes.length} 项</span></div>
        <ol className="run-activity-plan" aria-label="子任务进度">
          {nodes.map(node => <li key={node.nodeId} className={`activity-node-${node.state.toLowerCase()}`}>
            <span className="run-activity-node-mark" aria-hidden="true">{node.state === "SUCCEEDED" ? <Check size={12} /> : null}</span>
            <div><AgentBadge source={{ nodeId: node.nodeId }} execution={execution} /><span className="run-activity-objective">{node.objective.replace(/^Independently verify the integrated result for this original request:\s*/, "核验最终结果：")}</span>
              <span className="run-activity-node-state">{advisoryFailureLabel(node) || nodeStateLabels[node.state] || "状态未知"}{node.attemptCount > 1 ? ` · 第 ${node.attemptCount} 次尝试` : ""}</span>
            </div>
          </li>)}
        </ol>
      </>}
      {view.approval?.type === "approval" ? <button className="run-activity-action" onClick={() => { setOpen(false); onJump(`approval-${view.approval!.id}`); }}>前往确认</button>
        : view.needsInput && <button className="run-activity-action" onClick={() => { setOpen(false); onCompose(); }}>补充信息</button>}
    </div>}
  </div>;
}
