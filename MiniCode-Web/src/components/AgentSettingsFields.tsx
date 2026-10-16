import type { SettingsView, WebAgentSettings } from "../types";
import { changeAgentMode } from "../lib/agentSettings";

const modes = [
  { value: "off", title: "基础协作", description: "保留 Ultra 原有的任务分派，关闭异步子 Agent 和团队协作。" },
  { value: "async", title: "异步子 Agent", description: "按需分派独立子任务，主 Agent 可同时继续工作，完成后统一汇总结果。" },
  { value: "teams", title: "团队协作（Agent Teams）", description: "在同一任务中保留团队成员，由主 Agent 持续分配、跟进和追加任务。" },
] as const;

export function AgentSettingsFields({ draft, onChange, settings, disabled }: {
  draft: WebAgentSettings; onChange: (draft: WebAgentSettings) => void; settings: SettingsView | null; disabled: boolean;
}) {
  const available = settings?.webAgentSettingsAvailable === true;
  const teams = draft.mode === "teams";
  return <section className="settings-section agent-settings" aria-labelledby="agent-settings-title">
    <div className="agent-settings-heading"><h2 id="agent-settings-title">多 Agent 协作</h2><span>Ultra 专属</span></div>
    <p className="settings-help" id="agent-settings-help">保存后，对新发起的 Ultra 任务生效。可在输入框旁选择 Ultra；正在运行的任务继续使用原设置。</p>
    {!available && <p className="settings-help" role="status">{!settings ? "正在加载设置…" : settings.webAgentSettingsAvailable === false
      ? "本机服务已禁用 Ultra 协作，当前无法修改。"
      : "请重启 MiniCode 本机服务并刷新页面，以加载协作设置。"}</p>}
    {settings?.webAgentSettingsError && <p className="settings-help" role="alert">{settings.webAgentSettingsError}</p>}
    <fieldset disabled={disabled || !available} aria-describedby="agent-settings-help">
      <legend className="sr-only">协作方式</legend>
      <div className="agent-mode-options">
        {modes.map((mode) => <label key={mode.value} className={`agent-mode-option ${draft.mode === mode.value ? "is-selected" : ""}`}>
          <input type="radio" name="agent-mode" value={mode.value} checked={draft.mode === mode.value}
            onChange={() => onChange(changeAgentMode(draft, mode.value))} />
          <span><strong>{mode.title}</strong><small>{mode.description}</small></span>
        </label>)}
      </div>
      <p className="settings-help agent-mode-help">是否分派由主 Agent 按任务需要决定，所有 Agent 共用本次任务预算。</p>
      <details className="agent-advanced">
        <summary>高级选项</summary>
        <label className="agent-check">
          <input type="checkbox" checked={draft.teamWrites} disabled={!teams}
            onChange={(event) => onChange({ ...draft, teamWrites: event.target.checked })} />
          <span>允许团队并行修改文件<small>{teams ? "当前目录按文件范围共享写入；隔离副本使用独立候选合入。最终仍需统一验证。" : "需要先选择团队协作（Agent Teams）。"}</small></span>
        </label>
        <label><span>主 Agent 的职责</span>
          <select value={draft.primaryRole} disabled={!teams}
            onChange={(event) => onChange({ ...draft, primaryRole: event.target.value as WebAgentSettings["primaryRole"],
              fork: event.target.value === "coordinator" ? false : draft.fork })}>
            <option value="worker">参与执行与调度</option>
            <option value="coordinator">仅负责调度（Coordinator）</option>
          </select>
          <small>{teams ? "仅调度时，主 Agent 可只读分析和统筹，文件修改交给成员；只读问答可能不派发子任务。自动关闭 Fork。" : "仅调度需要先选择团队协作（Agent Teams）。"}</small>
        </label>
        <label className="agent-check">
          <input type="checkbox" checked={draft.fork} disabled={draft.mode === "off" || draft.primaryRole === "coordinator"}
            onChange={(event) => onChange({ ...draft, fork: event.target.checked })} />
          <span>复用父任务上下文（Fork）<small>{draft.primaryRole === "coordinator" ? "仅调度模式不支持 Fork。" : "选择异步子 Agent 或团队协作后可用，仅用于兼容的只读子任务。"}</small></span>
        </label>
        <label><span>提示词缓存</span>
          <select value={draft.promptCache} onChange={(event) => onChange({ ...draft, promptCache: event.target.value as WebAgentSettings["promptCache"] })}>
            <option value="off">关闭</option><option value="auto">自动（提供商支持时）</option><option value="stable-prefix">稳定前缀</option>
          </select>
          <small>可单独启用，实际缓存命中与费用以提供商返回为准。</small>
        </label>
      </details>
    </fieldset>
  </section>;
}
