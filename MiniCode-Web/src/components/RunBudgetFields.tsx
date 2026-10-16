import type { ExecutionLocation, ExecutionProfile, RunBudget, SettingsView } from "../types";
import { runBudgetSelection, type useRunBudget } from "../lib/useRunBudget";
import "./RunBudgetFields.css";

export function RunBudgetFields({ budget, settings, previous, disabled, active, executionProfile, environment, executionLocation }: {
  budget: ReturnType<typeof useRunBudget>; settings: SettingsView | null;
  previous: RunBudget | null; disabled: boolean; active: boolean;
  executionProfile: ExecutionProfile;
  environment?: "local" | "worktree";
  executionLocation?: ExecutionLocation;
}) {
  const supported = settings?.runBudgetSupported === true;
  const policy = settings?.webRuntimePolicy;
  const trial = policy?.enabled && policy.modes.includes(executionProfile) ? policy : null;
  const defaultTokens = settings?.runtimeMaxTokens ?? trial?.defaultMaxTokens;
  const draft = budget.draft;
  let error = "";
  try { runBudgetSelection(draft, supported); } catch (cause) { error = cause instanceof Error ? cause.message : "请检查任务预算。"; }
  const unlimited = active ? previous?.unlimited === true : draft.unlimited === true;
  const tokens = active ? previous?.maxTokens?.toString() || "" : draft.maxTokens;
  const duration = active ? previous?.durationSeconds?.toString() || "" : draft.durationSeconds;
  const hasDraft = Boolean(unlimited || tokens.trim() || duration.trim());
  const expired = settings?.runtimeDeadline != null && settings.runtimeDeadline * 1000 <= Date.now() && !duration.trim();
  const location = executionLocation === "isolated" ? "隔离副本中修改" : executionLocation === "project"
    ? environment === "worktree" ? "直接修改当前 Worktree" : "直接修改当前目录" : "执行位置以运行记录为准";
  return <>
    {policy?.enabled && <p className="web-runtime-status" role="status">{trial
      ? `${active ? "本轮执行" : "下次发送"}：${executionProfile === "ultra" ? "Ultra 主 Agent 主导 · 按需子 Agent" : executionProfile === "deep" ? "Deep" : "Standard"} · ${location}`
      : `${executionProfile === "ultra" ? "Ultra" : executionProfile === "deep" ? "Deep" : "Standard"} 沿用原配置，尚未纳入本次 Web 切换`}</p>}
    <details className="run-budget-fields">
    <summary>任务预算 · 高级设置 <span>{active ? unlimited ? "不限额 · 本轮" : previous ? "本轮已指定" : "本轮自动管理" : error ? "待修正" : unlimited ? "不限额 · 本对话" : hasDraft ? "已指定 · 本对话" : `自动管理${defaultTokens ? ` · 上限 ${defaultTokens.toLocaleString()} Token` : ""}`}</span></summary>
    <p>新对话默认不限额。所选预算按对话保存，发送后、切换对话或刷新页面都会沿用，直到手动修改。</p>
    {!supported && <p role="status">当前后端尚未声明支持任务预算；升级并重启本机服务后可用。</p>}
    <fieldset disabled={disabled || !supported}>
      <div className="run-budget-actions" aria-label="预算快捷选择">
        <button type="button" aria-pressed={!hasDraft} onClick={budget.clear}>自动 · 沿用系统配置</button>
        <button type="button" aria-pressed={!unlimited && tokens === "6480000"} onClick={() => budget.change("maxTokens", "6480000")}>648 万 Token · 大型任务</button>
        {settings?.runUnlimitedBudgetSupported && <button type="button" aria-pressed={unlimited} onClick={budget.unlimit}>不限额、不限时（默认）</button>}
      </div>
      {unlimited && <p role="status">不设累计 Token、费用、时长和执行步数上限，持续记录用量，可随时停止；单次模型输出上限仍由模型设置决定。</p>}
      {!unlimited && <div className="run-budget-grid">
        <label>任务累计 Token 上限<input type="number" min="1" step="1" max={Number.MAX_SAFE_INTEGER} value={tokens}
          placeholder={active ? "未指定本轮覆盖，生效值见运行详情" : defaultTokens ? `默认 ${defaultTokens}` : "默认未设上限"}
          onChange={event => budget.change("maxTokens", event.target.value)} /></label>
        <label>最长运行时间（分钟）<input type="number" min="0" step="any" max="1440" value={duration ? String(Number(duration) / 60) : ""}
          placeholder={active ? "未指定本轮覆盖，生效值见运行详情" : settings?.runtimeDeadline ? "默认固定截止时间" : trial ? `默认每轮 ${trial.defaultDurationSeconds / 60} 分钟` : "默认未设截止时间"}
          onChange={event => budget.change("durationSeconds", event.target.value ? String(Number(event.target.value) * 60) : "")} /></label>
      </div>}
      <div className="run-budget-actions">
        {previous && <button type="button" onClick={() => budget.reuse(previous)}>使用上次预算</button>}
        {settings?.runUnlimitedBudgetSupported && <button type="button" disabled={unlimited} onClick={budget.unlimit}>恢复默认预算</button>}
      </div>
    </fieldset>
    {!unlimited && <p>648 万 Token 是整项任务多次请求的累计上限，包含输入和输出；不代表单次上下文容量，也不保证完成。本对话每轮任务均沿用所选预算，运行时长从每轮启动重新计时。主 Agent、团队成员和重试共享本轮总额。</p>}
    {active ? <p>本轮设置已锁定；实际生效的累计额度、截止时间与用量请查看运行详情。</p>
      : trial && !unlimited && <p>下次发送未指定覆盖时：累计上限 {defaultTokens?.toLocaleString()} Token，{settings?.runtimeDeadline ? "沿用设置中的截止时间" : `最长 ${trial.defaultDurationSeconds / 60} 分钟`}。累计上限包含多次请求的输入与输出，与模型单次输出上限不同。</p>}
    {expired && !active && !unlimited && <p role="alert">默认截止时间已过期；请为本次设置时长，或到设置中调整。</p>}
    {error && !active && <p role="alert">{error}</p>}
  </details></>;
}
