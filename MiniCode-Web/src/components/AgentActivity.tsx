import { GitFork, UserRound, Users } from "lucide-react";
import type { ExecutionSnapshot, RunStatus, TimelineOwner } from "../types";
import { agentOwner, collaborationSummary, executionAgents, ownerLabel, settledAgentState, agentWorkspaceLabel } from "../lib/agentActivity";
import { advisoryFailureLabel, childRecoveryLabel, nodeStateLabels } from "../lib/execution";
import { reviewStatus } from "../lib/verification";
import "./AgentActivity.css";

export function AgentBadge({ source, execution }: { source: TimelineOwner; execution?: ExecutionSnapshot | null }) {
  const owner = agentOwner(source, execution);
  return <span className={`agent-badge agent-${owner.kind}`} title={source.nodeId ? `任务：${source.nodeId}` : "这条记录没有保存执行者信息"}>
    {owner.kind === "child" ? <GitFork size={11} aria-hidden="true" /> : owner.kind === "primary" ? <UserRound size={11} aria-hidden="true" /> : null}
    {ownerLabel(owner)}
  </span>;
}

export function AgentRunSummary({ execution }: { execution: ExecutionSnapshot }) {
  return <div className="agent-run-summary" aria-label="本轮 Agent 分工">
    <Users size={14} aria-hidden="true" /><span>{collaborationSummary(execution)}</span>
  </div>;
}

export function AgentRoster({ execution, status }: { execution: ExecutionSnapshot; status: RunStatus }) {
  const agents = executionAgents(execution);
  const active = ["queued", "running", "stopping"].includes(status);
  return <section className="inspector-section agent-roster" aria-label="Agent 分工">
    <div className="section-heading"><h2><Users size={15} />Agent 分工</h2><span>{execution.collaboration
      ? { off: "基础执行", async: "异步子 Agent", teams: "Agent Teams" }[execution.collaboration.mode]
      : execution.agentTeam ? "Agent Teams" : "本轮实际执行"}</span></div>
    <AgentRunSummary execution={execution} />
    {execution.collaboration && <p className="agent-empty-note">本轮主 Agent：{execution.collaboration.primaryRole === "coordinator"
      ? "仅调度，可只读分析；文件修改交给成员。" : "参与执行与调度。"}</p>}
    {execution.agentTeam && !agents.some(agent => agent.kind === "child") && <p className="agent-empty-note">
      {active ? "团队已启用，目前尚未派发子任务。" : "团队已启用，但本轮没有子 Agent 执行记录。"}
    </p>}
    <div className="agent-roster-list">{agents.map(agent => <div className={`agent-roster-card agent-${agent.kind}`} key={agent.key}>
      <div className="agent-roster-heading"><strong>{ownerLabel(agent)}</strong><span>{agent.nodes.length > 1 ? `${agent.nodes.length} 项任务` : ""}</span></div>
      {agent.nodes.map(node => <details className="agent-roster-task" key={node.nodeId}>
        <summary><span title={node.objective}>{node.objective}</span><small>{reviewStatus(node, active, status === "stopped", execution.nodes)?.label || childRecoveryLabel(node, settledAgentState(node.state, active, status === "stopped")) || advisoryFailureLabel(node) || nodeStateLabels[settledAgentState(node.state, active, status === "stopped")] || node.state}</small></summary>
        <p>{node.objective}</p>
        {node.role === "verifier" && <p>{reviewStatus(node, active, status === "stopped", execution.nodes)?.description}</p>}
        <p>任务标识：<code>{node.nodeId}</code></p>
        {execution.agentTasks?.find(task => task.taskId === node.nodeId)?.workspaceMode && <p>
          {agentWorkspaceLabel(execution.agentTasks.find(task => task.taskId === node.nodeId)?.workspaceMode, node.role)}
        </p>}
        {node.attemptCount > 0 && <p>执行尝试：{node.attemptCount} 次</p>}
        {node.summary ? <div className="agent-task-result"><strong>执行结果</strong><p>{node.summary}</p></div> : <p>尚无结果摘要。</p>}
      </details>)}
    </div>)}</div>
    <p className="agent-empty-note">点击对话中的子 Agent 条目，在右侧查看各自的操作与结果。</p>
  </section>;
}
