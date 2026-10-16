import { ChevronRight, CircleAlert, LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";
import type { ExecutionProfile, ExecutionSnapshot, RunInfo, RunStatus, TimelineItem } from "../types";
import { advisoryFailureLabel, phaseSteps, profileLabels, progressView, readableElapsed, routeExplanation } from "../lib/execution";
import { toolActivity } from "../lib/conversation";
import { agentOwner, ownerLabel } from "../lib/agentActivity";

interface Props {
  run: RunInfo | null;
  status: RunStatus;
  execution: ExecutionSnapshot | null;
  profile?: ExecutionProfile;
  items: TimelineItem[];
  reconnecting: boolean;
  onJump: (id: string) => void;
  onCompose: () => void;
}

export function RunProgress({ run, status, execution, profile, items, reconnecting, onJump, onCompose }: Props) {
  const [now, setNow] = useState(Date.now());
  const view = progressView(status, execution, items, reconnecting, run?.waitingModels, run?.pendingQuestions);
  useEffect(() => {
    if (!view.active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [view.active, run?.runId]);
  const steps = execution ? phaseSteps(execution) : [];
  const currentPhase = execution?.phase === "synthesizing" || execution?.phase === "retrying" ? "executing" : execution?.phase;
  const quietMs = run ? now - run.updatedAt : 0;
  const attention = Boolean(view.approval || view.needsApproval || view.needsInput || (view.active && reconnecting));
  const runningTool = [...items].reverse().find((item) => item.type === "tool" && item.status === "running");
  const activity = runningTool?.type === "tool" ? toolActivity(runningTool) : null;
  const owner = runningTool ? agentOwner(runningTool, execution) : null;
  const detail = view.active && !attention && activity ? `${owner?.kind === "child" ? `${ownerLabel(owner)} · ` : ""}正在${activity.label}${activity.target ? ` · ${activity.target}` : ""}` : view.detail;
  if (!view.active && !view.needsInput) return null;
  return <section className={`execution-progress ${attention ? "needs-attention" : ""}`} aria-label="本轮执行进度">
    <div className="execution-progress-line">
      {profile && <span className="execution-mode">本轮 {profileLabels[profile]}</span>}
      <span className="execution-current" role="status">
        {attention ? <CircleAlert size={14} /> : view.active ? <LoaderCircle size={14} className="spin" /> : null}
        <strong>{view.label}</strong>
      </span>
      {run && view.active && <span className="execution-elapsed">已用 {readableElapsed(now - run.createdAt)}</span>}
    </div>
    {(view.active || view.needsInput) && <div className="execution-detail">
      <span title={detail}>{detail}{view.active && !attention && quietMs >= 15000 ? ` · ${readableElapsed(quietMs)}无新进展` : ""}</span>
      {view.approval?.type === "approval" && <button onClick={() => onJump(`approval-${view.approval!.id}`)}>前往处理</button>}
      {!view.approval && view.needsInput && <button onClick={onCompose}>补充信息</button>}
    </div>}
    {execution && (view.active || routeExplanation(execution)) && <details className="execution-explanation">
      <summary>{view.active ? "查看执行阶段" : "查看执行方式"}<ChevronRight size={12} /></summary>
      {view.active && steps.length > 0 && <ol className="execution-steps" aria-label="执行阶段">
        {steps.map((step, index) => <li key={step.id} aria-current={step.id === currentPhase ? "step" : undefined}>
          {index > 0 && <ChevronRight size={12} aria-hidden="true" />}<span>{step.label}</span>
        </li>)}
      </ol>}
      {routeExplanation(execution) && <p className="execution-route-note">{routeExplanation(execution)}</p>}
    </details>}
    {execution?.nodes.some((node) => advisoryFailureLabel(node) && node.diagnosis?.acknowledged) && <p className="execution-route-note">参考咨询失败，主 Agent 已接管，可展开工作过程查看记录。</p>}
    {!view.active && status !== "completed" && execution && <p className="execution-route-note">已完成 {execution.nodes.filter((node) => node.state === "SUCCEEDED").length} / {execution.nodes.length} 项；{view.needsInput ? "补充信息后可继续。" : "查看未完成项和结果说明，再决定如何继续。"}</p>}
  </section>;
}
