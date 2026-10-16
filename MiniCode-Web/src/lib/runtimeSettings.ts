import type { RuntimeModelPricing, RuntimeSettings } from "../types";

export const pricingFields = [
  ["input", "输入单价"], ["output", "输出单价"],
  ["cacheRead", "缓存读取单价"], ["cacheWrite", "缓存写入单价"],
] as const;
export type PricingDraft = Record<keyof RuntimeModelPricing, string>;
export interface RuntimeSettingsDraft {
  maxTokens: string;
  maxCost: string;
  deadline: string;
  networkAccess: boolean;
  pricing: Record<string, PricingDraft>;
  editedPrices: string[];
}

export function emptyPricing(): PricingDraft {
  return { input: "", output: "", cacheRead: "", cacheWrite: "" };
}

export function runtimeSettingsDraft(settings: RuntimeSettings | null): RuntimeSettingsDraft {
  const date = settings?.runtimeDeadline == null ? null : new Date(settings.runtimeDeadline * 1000);
  return {
    maxTokens: settings?.runtimeMaxTokens?.toString() || "",
    maxCost: settings?.runtimeMaxCost?.toString() || "",
    // datetime-local displays local wall time; the API stores an absolute timestamp.
    deadline: date && Number.isFinite(date.getTime())
      ? new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 19) : "",
    networkAccess: settings?.runtimeNetworkAccess ?? true,
    pricing: Object.fromEntries(Object.entries(settings?.runtimeModelPricing || {}).map(([key, value]) => [key,
      value ? Object.fromEntries(pricingFields.map(([field]) => [field, String(value[field])])) as PricingDraft : emptyPricing(),
    ])),
    editedPrices: [],
  };
}

function positive(value: string, label: string, integer = false): number | null {
  if (!value.trim()) return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0 || (integer && !Number.isSafeInteger(number))) {
    throw new Error(`${label}必须是大于 0 的${integer ? "安全整数" : "有限数值"}，留空可取消上限。`);
  }
  return number;
}

function parsePricing(draft: PricingDraft): RuntimeModelPricing | null {
  if (pricingFields.every(([key]) => !draft[key].trim())) return null;
  if (pricingFields.some(([key]) => !draft[key].trim() || !Number.isFinite(Number(draft[key])) || Number(draft[key]) < 0)) {
    throw new Error("请填写全部四项非负单价；免费项请明确填写 0，清除计价则将四项全部留空。");
  }
  return Object.fromEntries(pricingFields.map(([key]) => [key, Number(draft[key])])) as unknown as RuntimeModelPricing;
}

export function runtimeSettingsPayload(draft: RuntimeSettingsDraft, provider: string, model: string): RuntimeSettings {
  const maxTokens = positive(draft.maxTokens, "Token 上限", true);
  const maxCost = positive(draft.maxCost, "费用上限");
  const deadline = draft.deadline ? new Date(draft.deadline).getTime() / 1000 : null;
  if (deadline != null && (!Number.isFinite(deadline) || deadline <= 0)) throw new Error("请选择有效的截止时间，或留空取消。");
  const current = parsePricing(draft.pricing[`${provider}:${model}`] || emptyPricing());
  if (maxCost != null && !current) throw new Error("设置费用上限时，请填写当前提供商和模型的全部四项单价。单价为 0 时请明确填写 0。");
  return {
    runtimeMaxTokens: maxTokens, runtimeMaxCost: maxCost, runtimeDeadline: deadline,
    runtimeNetworkAccess: draft.networkAccess,
    runtimeModelPricing: Object.fromEntries(draft.editedPrices.map((key) => [key, parsePricing(draft.pricing[key])])),
  };
}
