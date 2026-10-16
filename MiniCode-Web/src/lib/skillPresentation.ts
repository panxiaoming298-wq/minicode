import type { SkillInventoryItem } from "../types";

export type SkillAvailabilityTone = "ready" | "neutral" | "warning" | "danger";

const statusLabels: Record<string, string> = {
  ready: "在此范围可用", invalid: "配置错误", invalid_name: "名称不支持", unavailable: "暂不可用",
  shadowed: "被同名版本覆盖", disabled: "已停用", untrusted: "待信任所属扩展", permission_required: "待授权",
  dependency_cycle: "依赖循环", integrity_mismatch: "完整性异常", dependency_blocked: "依赖未就绪",
  dependency_missing: "缺少依赖",
};

const statusHints: Record<string, string> = {
  untrusted: "先查看技能内容，再到所属扩展确认信任。",
  permission_required: "所属扩展缺少所需权限，请在扩展管理中检查并授权。",
  disabled: "所属扩展已停用，可在扩展管理中重新启用。",
};

export function skillStatusLabel(status: string): string {
  return statusLabels[status] || status || "状态未知";
}

export function skillSourceLabel(skill: SkillInventoryItem): string {
  if (skill.extensionKey) return `${skill.scope === "project" ? "本项目" : "全局"}扩展 · ${skill.extensionId}`;
  return ({ project: "本项目目录", user: "全局目录", compat_project: "本项目 Claude 兼容目录", compat_user: "全局 Claude 兼容目录" } as Record<string, string>)[skill.source] || skill.source;
}

export function skillAvailability(skill: SkillInventoryItem, sameInvocationContext: boolean): {
  label: string;
  detail: string;
  tone: SkillAvailabilityTone;
} {
  // A version shadowed in the viewed project may still be the one used by the current conversation.
  if (skill.canInvokeInConversation) return {
    label: "当前对话可用", tone: "ready",
    detail: "添加后返回当前对话，保留已有草稿；发送消息时才会调用。",
  };
  if (skill.status !== "ready") return {
    label: skillStatusLabel(skill.status),
    detail: statusHints[skill.status] || skill.error || skill.reason || skill.invocationReason,
    tone: ["untrusted", "permission_required", "dependency_blocked", "dependency_missing"].includes(skill.status) ? "warning"
      : ["disabled", "shadowed", "unavailable"].includes(skill.status) ? "neutral" : "danger",
  };
  if (sameInvocationContext && !skill.userInvocable) return {
    label: skill.modelInvocable ? "仅自动匹配" : "已禁用调用", tone: "neutral",
    detail: skill.modelInvocable ? "此技能由模型按需匹配，不能手动添加到对话。" : "此技能禁止手动调用和模型自动匹配。",
  };
  return {
    label: "无法添加到当前对话", tone: "neutral",
    detail: skill.invocationReason || "请查看当前对话范围内的技能，或刷新目录核对调用状态。",
  };
}
