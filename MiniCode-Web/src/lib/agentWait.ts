import type { ExecutionSnapshot } from "../types";
import type { ToolTimelineItem } from "./runSummary";

export function agentWaitPresentation(item: ToolTimelineItem, execution?: ExecutionSnapshot | null, active = false) {
  if (!["wait_agents", "wait_dependencies"].includes(item.toolName) || item.status !== "success") return null;
  let receipt;
  try { receipt = JSON.parse(item.output); } catch { receipt = null; }
  const waitId = receipt?.wait?.wait_id || receipt?.decision?.wait_id;
  const saved = execution && item.executionId === execution.executionId
    ? execution.agentWaits?.find(wait => wait.waitId === waitId && wait.nodeId === item.nodeId) : undefined;
  const status = saved?.status || receipt?.decision?.status;
  const labels: Record<string, [string, string]> = {
    timed_out: ["等待超时", "本次等待已返回，子任务仍可能在运行。"],
    satisfied: ["等待条件已满足", "子任务结果已返回，仍需检查实际成果和验收记录。"],
    returned: ["等待已返回", "旧记录没有保存返回原因，请查看子任务结果。"],
    waiting: active ? ["等待子任务", "等待已登记，子任务尚未返回结果。"] : ["等待结果未确认", "本轮已结束，未保存本次等待的返回原因。"],
    generation_changed: ["子任务已更新", "子任务轮次发生变化，需要核对新一轮状态。"],
    interrupted: ["等待已中断", "依赖任务发生变化，请查看最新子任务记录。"],
    blocked: ["等待受阻", "请查看未满足的依赖条件。"],
    failed: ["等待失败", "依赖结果未满足要求，请查看子任务记录。"],
  };
  const [label, summary] = labels[status] || ["等待结果待核对", "已收到等待回执，尚不能确认子任务成功。"];
  return { label, summary, waiting: status === "waiting" && active, satisfied: status === "satisfied" };
}
