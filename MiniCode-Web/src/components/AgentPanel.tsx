import { ArrowLeft, ChevronRight, GitFork, LoaderCircle } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import type { TimelineItem } from "../types";
import { ownerLabel, agentWorkspaceLabel } from "../lib/agentActivity";
import { agentContinuationLabel, agentConversationState, agentTaskObjective, agentTaskState, agentTaskTranscript, type AgentConversation, type AgentPanelTask } from "../lib/agentPanel";
import { advisoryFailureLabel, childRecoveryLabel, nodeStateLabels } from "../lib/execution";
import { reviewStatus } from "../lib/verification";
import { toolFailureCount, toolFailureLabel } from "../lib/conversation";
import "./AgentPanel.css";

interface Props {
  agents: AgentConversation[];
  selected: string | null;
  onSelect: (key: string | null) => void;
  onClose: () => void;
  onApproval: (id: string) => void;
  renderItems: (task: AgentPanelTask, items: TimelineItem[], process: boolean) => ReactNode;
  renderAnswer: (content: string) => ReactNode;
}

function AgentTaskTranscript({ task, renderItems, renderAnswer, onApproval, onClose }: Pick<Props, "renderItems" | "renderAnswer" | "onApproval" | "onClose"> & { task: AgentPanelTask }) {
  const view = agentTaskTranscript(task);
  const state = agentTaskState(task);
  const running = task.active && !["SUCCEEDED", "CANCELED", "SUPERSEDED", "FAILED_TERMINAL", "UNKNOWN"].includes(state);
  const objective = agentTaskObjective(task.node.objective);
  const review = reviewStatus(task.node, task.active, task.status === "stopped", task.execution.nodes);
  const failedCalls = toolFailureCount(view.process, view.recovered);
  const workspace = agentWorkspaceLabel(task.execution.agentTasks?.find(item => item.taskId === task.node.nodeId)?.workspaceMode, task.node.role);
  const metadata = task.execution.agentTasks?.find(item => item.taskId === task.node.nodeId);
  const activity = useRef<HTMLDetailsElement>(null);
  const hasProcess = view.process.length > 0;
  useEffect(() => { if (activity.current) activity.current.open = running; }, [running, hasProcess]);
  return <section className="agent-panel-task" data-node-id={task.node.nodeId}>
    {metadata && <div className="agent-round-meta"><strong>第 {metadata.generation} 轮任务</strong>
      {metadata.model?.model && <span>{metadata.model.model}</span>}</div>}
    {review && <aside className={`review-explanation is-${review.tone}`} aria-label="复核说明">
      <strong>{review.label}</strong><p>{review.description}</p>
      {review.state === "passed" && failedCalls > 0 && <p>过程中保留了 {failedCalls} 次调用失败记录。复核意见基于已取得的证据，请结合失败详情和报告中的检查范围判断。</p>}
      <p className="review-role-note">独立核验由另一个只读 Agent 完成，综合原始需求、代码改动和检查记录给出意见。</p>
      {!task.active && review.state !== "passed" && <button type="button" className="review-return" onClick={onClose}>返回主任务</button>}
    </aside>}
    <details className="agent-task-instructions">
      <summary>任务说明<ChevronRight size={13} /></summary>
      <p>{objective}</p>
      {workspace && <p>{workspace}</p>}
      {!!metadata?.writeScope?.length && <p>负责文件：{metadata.writeScope.join("、")}</p>}
      {!!metadata?.acceptanceCriteria?.length && <ul>{metadata.acceptanceCriteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul>}
      {task.node.attemptCount > 1 && <p>第 {task.node.attemptCount} 次尝试</p>}
    </details>
    {!review && <div className="agent-task-state">{agentContinuationLabel(task) || childRecoveryLabel(task.node, state) || advisoryFailureLabel(task.node) || nodeStateLabels[state] || "状态未知"}</div>}
    {!!metadata?.checks?.length && <details className="agent-work-process">
      <summary>局部检查记录<ChevronRight size={14} /></summary>
      <ul className="agent-check-results">{metadata.checks.map(check => <li key={check.operationId}>
        <span>{check.id}</span><span>{check.state === "running" ? "执行中或等待恢复确认" : check.passed ? "通过" : "未通过"}</span>
      </li>)}</ul><p className="agent-empty-note">整体构建与集成验收由主任务继续完成。</p>
    </details>}
    {metadata?.resultHandling && <p className="agent-task-state">{metadata.resultHandling === "handled" ? "主代理已记录结果处理" : "结果已送达主代理上下文"} · 最终验收另行记录</p>}
    {view.history.length > 0 && <details className="agent-work-process">
      <summary>此前尝试（任务已恢复）<ChevronRight size={14} /></summary>
      {renderItems(task, view.history, true)}
    </details>}
    {hasProcess && <details className="agent-work-process" ref={activity}>
      <summary>工作过程<ChevronRight size={14} />{failedCalls > 0 && <span>{toolFailureLabel(failedCalls, running)}</span>}</summary>
      {renderItems(task, view.process, true)}
    </details>}
    {view.approvals.map(item => <div className="agent-approval-note" key={item.id}>
      <span>这项任务正在等待你的确认。</span><button type="button" onClick={() => onApproval(item.id)}>前往处理</button>
    </div>)}
    {view.outcome.length > 0 && renderItems(task, view.outcome, false)}
    {view.summary && <div className="agent-panel-result">{renderAnswer(view.summary)}</div>}
    {review && task.node.failure?.message && task.node.failure.message !== view.summary && <details className="agent-work-process" open={review.state === "findings"}>
      <summary>{review.state === "findings" ? "复核指出的问题" : "查看复核停止原因"}<ChevronRight size={14} /></summary>
      <div className="agent-panel-result">{renderAnswer(task.node.failure.message)}</div>
    </details>}
    {!task.items.length && !view.summary && !["not-started", "waiting"].includes(review?.state || "") && <p className="agent-empty-note">{running ? "等待子代理的活动记录。" : "未保存该子任务的详细过程。"}</p>}
  </section>;
}

export function AgentPanel({ agents, selected, onSelect, onClose, onApproval, renderItems, renderAnswer }: Props) {
  const current = agents.find(agent => agent.key === selected);
  const heading = useRef<HTMLHeadingElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
    follow.current = true;
    if (scroll.current) scroll.current.scrollTop = 0;
  }, [selected]);
  useEffect(() => {
    if (current?.tasks.some(task => task.active) && follow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [current]);
  const groups = [
    { label: "进行中", match: (state: string) => ["RUNNING", "CLAIMED", "VERIFYING", "WAITING_APPROVAL", "WAITING_INPUT", "WAITING_CHILDREN", "READY", "PENDING", "FAILED_RETRYABLE"].includes(state) },
    { label: "已完成", match: (state: string) => state === "SUCCEEDED" },
    { label: "其他记录", match: (state: string) => !["SUCCEEDED", "RUNNING", "CLAIMED", "VERIFYING", "WAITING_APPROVAL", "WAITING_INPUT", "WAITING_CHILDREN", "READY", "PENDING", "FAILED_RETRYABLE"].includes(state) },
  ];
  return <section className="agent-panel conversation-surface" aria-label="子代理面板" onKeyDown={event => {
    if (event.key === "Escape") { event.stopPropagation(); onClose(); }
  }}>
    <header className="agent-panel-header">
      {current && <button className="icon-button" type="button" aria-label="返回子代理列表" onClick={() => onSelect(null)}><ArrowLeft size={18} /></button>}
      <GitFork size={18} className="delegated-icon" aria-hidden="true" />
      <h2 ref={heading} tabIndex={-1}>{current ? ownerLabel(current.owner) : "子代理"}</h2>
    </header>
    <div className="agent-panel-scroll" ref={scroll} onScroll={() => {
      const element = scroll.current;
      if (element) follow.current = element.scrollHeight - element.clientHeight - element.scrollTop < 48;
    }}>
      {!current && agents.length > 0 && <p className="agent-list-hint">选择子代理，查看它的工作过程、通信与结果。</p>}
      {current ? current.tasks.map(task => <AgentTaskTranscript key={task.id} task={task} renderItems={renderItems} renderAnswer={renderAnswer} onApproval={onApproval} onClose={onClose} />)
        : agents.length === 0 ? <p className="agent-empty-note">当前已加载的对话中没有子代理任务。</p>
          : groups.map(group => {
            const members = agents.filter(agent => group.match(agentConversationState(agent)));
            if (!members.length) return null;
            return <section className="agent-panel-group" key={group.label} aria-label={group.label}>
              <h3>{group.label}<span>{members.length}</span></h3>
              {members.map(agent => {
                const state = agentConversationState(agent);
                const latestTask = [...agent.tasks].reverse().find(task => agentTaskState(task) === state) || agent.tasks.at(-1);
                const latest = latestTask?.node;
                const review = latestTask && reviewStatus(latestTask.node, latestTask.active, latestTask.status === "stopped", latestTask.execution.nodes);
                return <button type="button" className="agent-member-link" key={agent.key} onClick={() => onSelect(agent.key)}>
                  <GitFork size={17} className="delegated-icon" aria-hidden="true" />
                  <span><strong>{ownerLabel(agent.owner)}</strong><small>{agentTaskObjective(agent.tasks.at(-1)?.node.objective || "")}</small></span>
                  <span className="agent-member-status">{["RUNNING", "VERIFYING"].includes(state) && <LoaderCircle size={12} className="spin" />}{review?.label || latestTask && agentContinuationLabel(latestTask) || latest && (childRecoveryLabel(latest, state) || advisoryFailureLabel(latest)) || nodeStateLabels[state] || "状态未知"}<ChevronRight size={14} aria-hidden="true" /></span>
                </button>;
              })}
            </section>;
          })}
    </div>
  </section>;
}
