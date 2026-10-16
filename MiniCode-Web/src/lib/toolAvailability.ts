/** Present native capability refusals without guessing permissions from names. */
export function toolAvailabilityFailure(output: string, toolName: string): { label: string; summary: string } | undefined {
  let value: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(output);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return;
    value = parsed as Record<string, unknown>;
  } catch { return; }

  const reasons = value.unavailable_reasons;
  if (reasons && typeof reasons === "object" && !Array.isArray(reasons)) {
    const entries = Object.entries(reasons);
    if (entries.length && entries.every(([, reason]) => reason === "effect_restricted")) {
      return { label: "权限受限", summary: "当前角色的权限范围不包含这项工具，本次未执行。需要运行验证时，应使用受控验证入口，或将缺少的检查交回主任务；重复查找不会获得权限。" };
    }
    if (entries.length && entries.every(([, reason]) => reason === "network_disabled")) {
      return { label: "网络访问受限", summary: "当前任务未开放网络访问，这项工具不可用；重复查找不会改变访问权限。" };
    }
    if (entries.length) return { label: "工具不可用", summary: "请求的工具未全部包含在当前可用范围内，本次未执行；请根据详情中的原因选择可用工具。" };
  }
  if (toolName === "request_verification") {
    if (["not_configured", "not_observed_custom_registry"].includes(String(value.status))) {
      return { label: "验证证据不足", summary: "当前没有可供此次核验执行的登记检查，需要将待验证项和所需证据交回主任务；尚未验证的项目不能视为通过。" };
    }
    if (value.status === "failed") return { label: "验证未通过", summary: "登记的验证检查未通过或未能完成，请查看检查结果并处理缺少的证据。" };
  }
}
