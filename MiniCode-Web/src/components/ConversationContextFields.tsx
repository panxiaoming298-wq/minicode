import { useEffect, useId, useState } from "react";
import { ChevronDown } from "lucide-react";
import { DEFAULT_CONTEXT_TOKENS, type ConversationContextSelection } from "../lib/useConversationContext";
import type { ReasoningEffort } from "../types";

const PRESETS = [128_000, 256_000, DEFAULT_CONTEXT_TOKENS, 512_000];

export function ConversationReasoningSelector({ selection, active, disabled }: {
  selection: ConversationContextSelection; active: boolean; disabled: boolean;
}) {
  const view = selection.view;
  if (!view?.reasoningEffortSupported) return null;
  return <label className="conversation-reasoning-selector"
    title={active ? "仅此对话生效；下次任务使用新强度，本轮及已排队任务保持原设置" : "仅此对话生效；Standard、Deep、Ultra 沿用此强度"}>
    <span>思考强度</span>
    <select aria-label="本对话思考强度" value={view.reasoningEffort || "high"}
      disabled={disabled || selection.pending} onChange={event => {
        void selection.save(view.maxTokens, event.target.value as ReasoningEffort);
      }}>
      <option value="low">Low</option>
      <option value="high">High</option>
      <option value="max">Max</option>
    </select>
  </label>;
}

export function ConversationContextFields({ selection, active, disabled, compact = false }: {
  selection: ConversationContextSelection; active: boolean; disabled: boolean; compact?: boolean;
}) {
  const id = useId();
  const view = selection.view;
  const [custom, setCustom] = useState(false);
  const [text, setText] = useState(String(view?.maxTokens ?? DEFAULT_CONTEXT_TOKENS));
  useEffect(() => {
    setCustom(view?.maxTokens != null && !PRESETS.includes(view.maxTokens));
    setText(String(view?.maxTokens ?? DEFAULT_CONTEXT_TOKENS));
  }, [view?.maxTokens]);
  if (!selection.supported || !view) return null;
  const isCustom = custom || (view.maxTokens !== null && !PRESETS.includes(view.maxTokens));
  const choice = isCustom ? "custom" : view.maxTokens === null ? "max" : String(view.maxTokens);
  const busy = disabled || selection.pending;
  const cap = view.maxTokens === null ? "模型上限" : view.maxTokens % 1000 === 0
    ? `${view.maxTokens / 1000}K` : view.maxTokens.toLocaleString();
  const controls = <div className="conversation-context-controls">
    <label htmlFor={id}>本对话上下文上限</label>
    <select id={id} value={choice} disabled={busy} onChange={event => {
      const value = event.target.value;
      if (value === "custom") { setCustom(true); return; }
      setCustom(false);
      void selection.save(value === "max" ? null : Number(value));
    }}>
      <option value="128000">128K</option>
      <option value="256000">256K</option>
      <option value={DEFAULT_CONTEXT_TOKENS}>400K（默认）</option>
      <option value="512000">512K</option>
      <option value="max">模型上限</option>
      <option value="custom">自定义</option>
    </select>
    {isCustom && <div className="conversation-context-custom">
      <input aria-label="自定义上下文 Token 数" type="number" min="8192" step="1" value={text}
        disabled={busy} onChange={event => setText(event.target.value)} />
      <button type="button" disabled={busy} onClick={() => void selection.save(Number(text))}>保存</button>
    </div>}
    {view.budgetPolicy ? <>
      <p>总窗口 {view.effectiveMaxTokens.toLocaleString()} tokens，包含输入、思考与回答。</p>
      <p>整理阈值 {view.budgetPolicy.compactAtTokens.toLocaleString()} · 保护阈值 {view.budgetPolicy.protectAtTokens.toLocaleString()} tokens</p>
      <p>{view.budgetPolicy.outputMode === "auto" ? "自动输出" : "指定输出"}上限 {view.budgetPolicy.outputLimit.toLocaleString()} tokens。
        {view.budgetPolicy.outputMode === "auto" ? "按请求剩余空间调整，不固定扣除整块输出额度。" : "保留指定输出额度；空间不足时提前整理上下文。"}</p>
      <p>输出工作余量 {view.outputReserveTokens?.toLocaleString()} · 安全余量 {view.safetyReserveTokens?.toLocaleString()} tokens</p>
    </> : <><p>按当前设置预估：窗口 {view.effectiveMaxTokens.toLocaleString()} tokens · 可用输入 {view.effectiveInputTokens.toLocaleString()}</p>
    {view.outputReserveTokens != null && view.safetyReserveTokens != null
      ? <p>输出预留 {view.outputReserveTokens.toLocaleString()} · 安全余量 {view.safetyReserveTokens.toLocaleString()} tokens</p>
      : <p>输出与安全余量合计 {view.reservedTokens.toLocaleString()} tokens（尚无分项）。</p>}</>}
    <p>{view.modelCapacityKnown === false ? "模型容量暂按" : "模型标称容量"} {view.modelMaxTokens.toLocaleString()}。1K = 1,000 tokens。</p>
    <p>{active ? "新任务使用此设置；正在运行的任务和已排队任务保持原上限。" : "仅此对话生效。调整上限不删除历史消息。"}</p>
  </div>;
  return <section className={`conversation-context-fields${compact ? " is-compact" : ""}`} aria-label="本对话上下文设置">
    {compact ? <details className="conversation-context-disclosure">
      <summary><span>本对话上限</span><span className="conversation-context-choice"><strong>{cap}</strong>
        <span>调整</span><ChevronDown size={13} aria-hidden="true" /></span></summary>
      {controls}
    </details> : controls}
    {compact && active && <p>调整后，下次任务生效；本轮保持原上限。</p>}
    {view.modelCapacityKnown === false
      ? <p>尚未识别此模型的上下文容量，暂按 {view.modelMaxTokens.toLocaleString()} tokens 计算；这不是服务商确认的上限。当前可用窗口为 {view.effectiveMaxTokens.toLocaleString()} tokens。</p>
      : view.maxTokens !== null && view.maxTokens > view.availableMaxTokens && <p>所选上限超过模型或服务容量，实际窗口为 {view.effectiveMaxTokens.toLocaleString()} tokens。</p>}
    {selection.pending && <p role="status">正在读取或保存设置…</p>}
    {selection.error && <p role="alert" className="conversation-context-error">{selection.error}<button type="button"
      disabled={busy} onClick={() => void selection.reload()}>重新读取</button></p>}
    {!selection.error && view.capacityError && <p role="alert" className="conversation-context-error">上限不足以容纳输出及安全预留，请调高上限。</p>}
  </section>;
}
