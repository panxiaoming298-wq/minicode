import type { ExecutionNode, ExecutionSnapshot, TimelineItem, TimelineOwner } from "../types";

export const agentRoleLabels: Record<string, string> = {
  primary: "统筹与执行", standard: "处理任务", deep: "处理任务", planner: "规划咨询",
  explorer: "探索", reviewer: "审查", writer: "汇总与修改", builder: "实现", shared_writer: "共享目录修改",
  synthesizer: "汇总", verifier: "独立核验", researcher: "调研",
};

export function agentWorkspaceLabel(mode?: string, role?: string): string {
  if (mode === "shared" && role === "shared_writer") return "共享主任务目录 · 按分配范围直接修改";
  return ({ shared: "共享主任务目录 · 实时读取", snapshot: "独立快照 · 固定当前文件",
    worktree: "Git worktree · 已提交版本" } as Record<string, string>)[mode || ""] || "";
}

export interface AgentActivityOwner {
  key: string;
  label: string;
  kind: "primary" | "child" | "system" | "unknown";
  role: string;
  nodes: ExecutionNode[];
}

const primaryRoles = new Set(["primary", "standard", "deep"]);

/** A persistent team member can own several generations of tasks. */
export function executionAgents(execution: ExecutionSnapshot): AgentActivityOwner[] {
  const groups = new Map<string, AgentActivityOwner>();
  const tasks = execution.agentTasks || [];
  const nodes = [...execution.nodes];
  for (const task of tasks) {
    if (!nodes.some(node => node.nodeId === task.taskId)) nodes.push({
      nodeId: task.taskId, objective: task.objective,
      role: execution.agentTeam?.members.find(member => member.session_id === task.sessionId)?.role || "",
      state: task.state, dependencies: [], attemptCount: 0, summary: "",
    });
  }
  for (const member of execution.agentTeam?.members || []) {
    if (member.current_task_id && !nodes.some(node => node.nodeId === member.current_task_id)) nodes.push({
      nodeId: member.current_task_id, objective: "任务目标未记录", role: member.role,
      state: member.state, dependencies: [], attemptCount: 0, summary: "",
    });
  }
  for (const node of nodes) {
    const primary = node.nodeId === "primary" || primaryRoles.has(node.role);
    const session = tasks.find(task => task.taskId === node.nodeId)?.sessionId
      || execution.agentTeam?.members.find(member => member.current_task_id === node.nodeId || member.task_ids?.includes(node.nodeId))?.session_id;
    const key = primary ? "primary" : session || node.nodeId;
    const previous = groups.get(key);
    if (previous) previous.nodes.push(node);
    else groups.set(key, { key, label: "", kind: primary ? "primary" : "child",
      role: agentRoleLabels[node.role] || node.role || "辅助任务", nodes: [node] });
  }
  let child = 0;
  return [...groups.values()].sort((a, b) => Number(b.kind === "primary") - Number(a.kind === "primary"))
    .map(owner => {
      if (owner.kind === "primary") return { ...owner, label: "主 Agent" };
      child += 1;
      const savedName = tasks.find(task => task.sessionId === owner.key && task.name?.trim())?.name;
      return { ...owner, label: savedName || `子 Agent ${child}` };
    });
}

export function agentOwner(source: TimelineOwner, execution?: ExecutionSnapshot | null): AgentActivityOwner {
  if (!execution && source.nodeId === "primary") return { key: "primary", label: "主 Agent", kind: "primary", role: "", nodes: [] };
  if (execution && (!source.executionId || source.executionId === execution.executionId)) {
    const owner = executionAgents(execution).find(agent => agent.nodes.some(node => node.nodeId === source.nodeId));
    if (owner) return owner;
  }
  return { key: "unattributed", label: "归属未记录", kind: "unknown", role: "", nodes: [] };
}

export function timelineOwner(item: TimelineItem, execution?: ExecutionSnapshot | null): AgentActivityOwner {
  if (!item.nodeId && item.type === "progress" && item.source === "runtime") {
    return { key: "system", label: "运行系统", kind: "system", role: "", nodes: [] };
  }
  return agentOwner(item, execution);
}

export type AgentProcessEntry =
  | { type: "activity"; id: string; items: TimelineItem[] }
  | { type: "agent-task"; id: string; owner: AgentActivityOwner; node: ExecutionNode; items: TimelineItem[] };

/** Keep ordinary activity in order; place each delegated task at its first recorded activity. */
export function agentProcessEntries(items: TimelineItem[], execution?: ExecutionSnapshot | null): AgentProcessEntry[] {
  return agentProcessSections([items], execution)[0];
}

/** Share task entries across a whole turn, including activity split by questions or images. */
export function agentProcessSections(sections: TimelineItem[][], execution?: ExecutionSnapshot | null): AgentProcessEntry[][] {
  const children = execution ? executionAgents(execution).filter(owner => owner.kind === "child") : [];
  const tasks = new Map(children.flatMap(owner => owner.nodes.map(node => [node.nodeId, {
    type: "agent-task" as const, id: `agent-task:${execution!.executionId}:${node.nodeId}`, owner, node, items: [] as TimelineItem[],
  }] as const)));
  const inserted = new Set<string>();
  const grouped = sections.map(items => {
    const entries: AgentProcessEntry[] = [];
    for (const item of items) {
      const task = (!item.executionId || item.executionId === execution?.executionId) && item.nodeId ? tasks.get(item.nodeId) : undefined;
      if (task) {
        task.items.push(item);
        if (!inserted.has(task.id)) { entries.push(task); inserted.add(task.id); }
      } else {
        const previous = entries.at(-1);
        if (previous?.type === "activity") previous.items.push(item);
        else entries.push({ type: "activity", id: item.id, items: [item] });
      }
    }
    return entries;
  });
  // Saved snapshots can retain a member's result even when its detailed events were not stored.
  for (const task of tasks.values()) if (!inserted.has(task.id)) grouped.at(-1)?.push(task);
  return grouped;
}

export function ownerLabel(owner: AgentActivityOwner): string {
  return owner.kind === "child" ? `${owner.label} · ${owner.role}` : owner.label;
}

export function collaborationSummary(execution: ExecutionSnapshot): string {
  const agents = executionAgents(execution);
  const children = agents.filter(agent => agent.kind === "child");
  if (children.length) return `${agents.some(agent => agent.kind === "primary") ? "主 Agent + " : ""}${children.length} 个子 Agent`;
  return execution.nodes.length ? "主 Agent 执行 · 本轮未派发子 Agent" : "等待执行分工";
}

export function agentMessageLabel(id: string, execution?: ExecutionSnapshot | null): string {
  if (id === "primary") return "主 Agent";
  const owner = execution && executionAgents(execution).find(agent => agent.key === id || agent.nodes.some(node => node.nodeId === id));
  return owner ? ownerLabel(owner) : id;
}

/** A terminal run does not prove an unfinished member succeeded. */
export function settledAgentState(state: string, active: boolean, stopped = false): string {
  return !active && ["PENDING", "READY", "CLAIMED", "RUNNING", "VERIFYING", "WAITING_CHILDREN", "FAILED_RETRYABLE"].includes(state)
    ? stopped ? "CANCELED" : "UNKNOWN" : state;
}
