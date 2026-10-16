import type { Dispatch, SetStateAction } from "react";
import { emptyPricing, pricingFields, type RuntimeSettingsDraft } from "../lib/runtimeSettings";

export function RuntimeSettingsFields({ draft, onChange, provider, model, disabled, expanded }: {
  draft: RuntimeSettingsDraft;
  onChange: Dispatch<SetStateAction<RuntimeSettingsDraft>>;
  provider: string;
  model: string;
  disabled: boolean;
  expanded?: boolean;
}) {
  const pricingKey = `${provider}:${model.trim()}`;
  const rates = draft.pricing[pricingKey] || emptyPricing();
  return (
    <details className="runtime-settings-advanced" open={expanded || undefined}>
      <summary>运行预算与权限</summary>
      <fieldset disabled={disabled}>
        <section className="settings-section compact-grid">
          <h2>每轮执行上限</h2>
          <p className="settings-help">对新启动的任务生效，所有节点和重试共享额度。留空表示不保存覆盖值；Web 默认仍会补上预算。需要不限额时，请在发送前的任务预算中明确选择。</p>
          <label><span>Token 上限</span><input type="number" min="1" step="1" max={Number.MAX_SAFE_INTEGER} value={draft.maxTokens} placeholder="未设置"
            onChange={(event) => onChange((value) => ({ ...value, maxTokens: event.target.value }))} /></label>
          <label><span>费用上限（USD）</span><input type="number" min="0" step="any" value={draft.maxCost} placeholder="未设置"
            onChange={(event) => onChange((value) => ({ ...value, maxCost: event.target.value }))} /></label>
          <label className="settings-wide"><span id="runtime-deadline-label">截止时间（本地时间）</span><input type="datetime-local" step="1" value={draft.deadline} aria-labelledby="runtime-deadline-label" aria-describedby="runtime-deadline-help"
            onChange={(event) => onChange((value) => ({ ...value, deadline: event.target.value }))} />
            <small id="runtime-deadline-help">这是固定的截止时间，到期后新任务也会停止；留空可取消。</small></label>
          <label className="clear-key-check settings-wide"><input type="checkbox" checked={draft.networkAccess}
            onChange={(event) => onChange((value) => ({ ...value, networkAccess: event.target.checked }))} /><span>允许内置联网工具</span></label>
          <p className="settings-help">控制搜索、网页获取等声明联网能力的工具；模型 API 连接不受此项影响。</p>
        </section>
        <section className="settings-section compact-grid">
          <h2>当前模型计价</h2>
          <p className="settings-help">{provider} / {model || "未选择模型"} · USD / 百万 Token。请填写实际计价；四项全空时费用可能未知。</p>
          {pricingFields.map(([key, label]) => <label key={key}><span>{label}</span>
            <input type="number" min="0" step="any" placeholder="未知" value={rates[key]} onChange={(event) => {
              const next = event.target.value;
              onChange((value) => ({ ...value,
                pricing: { ...value.pricing, [pricingKey]: { ...(value.pricing[pricingKey] || emptyPricing()), [key]: next } },
                editedPrices: [...new Set([...value.editedPrices, pricingKey])],
              }));
            }} /></label>)}
          <p className="settings-help">费用上限需要完整单价才能在发起请求前预留额度。免费项填 0；清除计价请将四项全部留空。</p>
        </section>
      </fieldset>
    </details>
  );
}
