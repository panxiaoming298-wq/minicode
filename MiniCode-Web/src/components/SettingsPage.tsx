import {
  Check,
  CircleAlert,
  Eye,
  EyeOff,
  Settings,
  LoaderCircle,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { SavedConnection, SettingsUpdate, SettingsView } from "../types";
import { runtimeSettingsDraft, runtimeSettingsPayload } from "../lib/runtimeSettings";
import { RuntimeSettingsFields } from "./RuntimeSettingsFields";
import { AgentSettingsFields } from "./AgentSettingsFields";
import { agentSettingsDraft } from "../lib/agentSettings";
import { connectionId, readConnectionHistory, rememberConnectionHistory } from "../lib/connectionHistory";
import { WorkspacePage, type WorkspacePageNavigation } from "./WorkspacePage";
import { DeepSeekBalance } from "./DeepSeekBalance";
import { ImageStorageSettings } from "./ImageStorageSettings";
import type { SettingsSection } from "./SettingsDialog";

interface SettingsPageProps extends WorkspacePageNavigation {
  embedded?: boolean;
  section?: Exclude<SettingsSection, "extensions" | "tools" | "search">;
  onSectionChange?: (section: SettingsSection) => void;
  settings: SettingsView | null;
  onSave: (payload: SettingsUpdate) => Promise<SettingsView>;
  onTest: () => Promise<{ ok: boolean; latencyMs: number; message: string }>;
}

type Notice = { kind: "success" | "error"; text: string } | null;

const CUSTOM_MODEL_VALUE = "__custom_model__";

type ConnectionDraft = { model: string; baseUrl: string; customModel: string; customMode: boolean };

function connectionDraft(provider: string, settings: SettingsView | null, history: SavedConnection[]): ConnectionDraft {
  const option = settings?.providers.find(item => item.id === provider);
  const saved = settings?.provider === provider ? settings : history.find(item => item.provider === provider);
  const model = saved?.model ?? option?.models[0] ?? "";
  const customMode = !option?.models.includes(model);
  const lastCustom = history.find(item => item.provider === provider && !option?.models.includes(item.model));
  return { model, baseUrl: saved?.baseUrl ?? option?.defaultBaseUrl ?? "",
    customMode, customModel: customMode ? model : lastCustom?.model ?? "" };
}

export function SettingsPage({ settings, onBack, onOpenRail, railCollapsed, onSave, onTest,
  embedded, section, onSectionChange }: SettingsPageProps) {
  const [provider, setProvider] = useState("deepseek");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [customModel, setCustomModel] = useState("");
  const [customMode, setCustomMode] = useState(false);
  const [connectionHistory, setConnectionHistory] = useState<SavedConnection[]>([]);
  const connectionDrafts = useRef<Record<string, ConnectionDraft>>({});
  const [imageInputMode, setImageInputMode] = useState<"auto" | "supported" | "unsupported">("auto");
  const [apiKey, setApiKey] = useState("");
  const [thinkingMode, setThinkingMode] = useState("enabled");
  const [maxOutputTokens, setMaxOutputTokens] = useState("");
  const [outputMode, setOutputMode] = useState<"auto" | "manual">("auto");
  const [showKey, setShowKey] = useState(false);
  const [clearApiKey, setClearApiKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [runtimeDraft, setRuntimeDraft] = useState(() => runtimeSettingsDraft(settings));
  const [agentDraft, setAgentDraft] = useState(() => agentSettingsDraft(settings?.webAgentSettings));
  const busy = saving || testing;

  useEffect(() => {
    if (!settings) return;
    setProvider(settings.provider);
    setModel(settings.model);
    setBaseUrl(settings.baseUrl);
    const history = readConnectionHistory(settings);
    const draft = connectionDraft(settings.provider, settings, history);
    setConnectionHistory(history);
    setCustomModel(draft.customModel);
    setCustomMode(draft.customMode);
    setImageInputMode(settings.imageInput?.mode || "auto");
    setThinkingMode(settings.thinkingMode || "enabled");
    setMaxOutputTokens(settings.maxOutputTokens?.toString() || "");
    setOutputMode(settings.maxOutputTokens == null ? "auto" : "manual");
    setRuntimeDraft(runtimeSettingsDraft(settings));
    setAgentDraft(agentSettingsDraft(settings.webAgentSettings));
    setApiKey("");
    setClearApiKey(false);
    setShowKey(false);
    setNotice(null);
  }, [Boolean(settings)]);

  const selectedProvider = useMemo(
    () => settings?.providers.find((option) => option.id === provider),
    [provider, settings],
  );
  const modelOptions = selectedProvider?.models ?? [];
  const usesCustomModel = customMode || !modelOptions.includes(model);
  const providerHistory = connectionHistory.filter(item => item.provider === provider);
  const savedModels = [...new Set(providerHistory.map(item => item.model))].filter(item => !modelOptions.includes(item));
  const savedBaseUrls = [...new Set([selectedProvider?.defaultBaseUrl, ...providerHistory.map(item => item.baseUrl)])].filter(Boolean) as string[];
  const selectedConnection = connectionId({ provider, model, baseUrl });
  const runtimeSettingsSupported = typeof settings?.runtimeNetworkAccess === "boolean";
  const isSavedProvider = settings?.provider === provider;
  const savedKeyConfigured = isSavedProvider && settings.apiKeyConfigured;
  const savedProviderLabel = settings?.providers.find(option => option.id === settings.provider)?.label || settings?.provider || "未配置模型";
  useEffect(() => {
    setImageInputMode(provider === settings?.provider && model === settings?.model && baseUrl === settings?.baseUrl
      ? settings.imageInput?.mode || "auto" : "auto");
  }, [provider, model, baseUrl]);

  function changeProvider(nextProvider: string) {
    if (nextProvider === provider) return;
    connectionDrafts.current[provider] = { model, baseUrl, customModel, customMode };
    const draft = connectionDrafts.current[nextProvider] ?? connectionDraft(nextProvider, settings, connectionHistory);
    setProvider(nextProvider);
    setBaseUrl(draft.baseUrl);
    setModel(draft.model);
    setCustomModel(draft.customModel);
    setCustomMode(draft.customMode);
    // A secret draft or deletion decision belongs only to the selected provider.
    setApiKey("");
    setClearApiKey(false);
    setShowKey(false);
    setNotice(null);
  }

  function selectConnection(id: string) {
    const saved = providerHistory.find(item => connectionId(item) === id);
    if (!saved) return;
    setBaseUrl(saved.baseUrl);
    setModel(saved.model);
    const custom = !modelOptions.includes(saved.model);
    setCustomMode(custom);
    if (custom) setCustomModel(saved.model);
    setNotice(null);
  }

  async function persistSettings() {
    if (section && (!model.trim() || !URL.canParse(baseUrl.trim()))) {
      onSectionChange?.("models");
      throw new Error(!model.trim() ? "请输入模型 ID。" : "请输入有效的 Base URL。");
    }
    const outputLimit = outputMode === "manual" ? Number(maxOutputTokens) : null;
    if (outputLimit !== null && (!Number.isInteger(outputLimit) || outputLimit < 1 || outputLimit > 1048576)) {
      throw new Error("请输入 1 到 1048576 之间的整数作为手动输出上限，或选择自动模式。");
    }
    const saved = await onSave({
      provider, model: model.trim(), baseUrl: baseUrl.trim(),
      ...(settings?.imageInput ? { imageInputMode } : {}),
      ...(apiKey.trim() ? { apiKey: apiKey.trim() } : {}),
      clearApiKey, thinkingMode,
      ...(runtimeSettingsSupported ? runtimeSettingsPayload(runtimeDraft, provider, model.trim()) : {}),
      maxOutputTokens: outputLimit,
      ...(settings?.webAgentSettingsAvailable ? { webAgentSettings: agentDraft } : {}),
    });
    setApiKey("");
    setClearApiKey(false);
    setShowKey(false);
    setRuntimeDraft(runtimeSettingsDraft(saved));
    setAgentDraft(agentSettingsDraft(saved.webAgentSettings));
    setMaxOutputTokens(saved.maxOutputTokens?.toString() || "");
    setOutputMode(saved.maxOutputTokens == null ? "auto" : "manual");
    const remembered = rememberConnectionHistory(saved, connectionHistory);
    setConnectionHistory(remembered.history);
    if (!remembered.persisted) {
      throw new Error("连接设置已保存，但浏览器未允许保存历史。请允许本机存储，或更新并重启后端后重试。");
    }
  }

  async function save(event?: React.FormEvent) {
    event?.preventDefault();
    if (busy) return;
    setSaving(true);
    setNotice(null);
    try {
      await persistSettings();
      setNotice({ kind: "success", text: settings?.webAgentSettingsAvailable
        ? "设置已保存。协作设置将在新启动的 Ultra 任务中生效。" : "设置已保存到本机。" });
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "保存失败。" });
    } finally {
      setSaving(false);
    }
  }

  async function testConnection() {
    if (busy) return;
    setTesting(true);
    setNotice(null);
    try {
      await persistSettings();
      const result = await onTest();
      setNotice({ kind: "success", text: `${result.message} 耗时 ${result.latencyMs} ms。` });
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "连接测试失败。" });
    } finally {
      setTesting(false);
    }
  }

  return (
    <WorkspacePage className="settings-page" title="设置" icon={Settings} headingId="settings-title"
      description="模型连接、Agent 协作与运行偏好"
      onBack={onBack} onOpenRail={onOpenRail} railCollapsed={railCollapsed} embedded={embedded}>
        <form className="settings-form" noValidate={Boolean(section)} onSubmit={(event) => void save(event)}>
          {section === "account" && <div className="settings-account">
            <section className="settings-account-card">
              <div className="settings-account-connection">
                <span className="settings-account-avatar"><UserRound size={23} strokeWidth={1.7} /></span>
                <div className="settings-account-identity"><h3>{savedProviderLabel}</h3><p>{settings?.apiKeyConfigured ? "API Key 已配置" : "尚未配置 API Key"}</p></div>
                <button className="button button-secondary" type="button" onClick={() => onSectionChange?.("models")}>管理连接</button>
              </div>
              <dl className="settings-account-details">
                <div><dt>当前模型</dt><dd>{settings?.model || "未选择"}</dd></div>
                <div><dt>API Key</dt><dd>{settings?.apiKeyMask || "未设置"}</dd></div>
                <div><dt>服务地址</dt><dd>{settings?.baseUrl || "未设置"}</dd></div>
              </dl>
            </section>
            {settings?.provider === "deepseek" ? <DeepSeekBalance settings={settings} connectionChanged={false} disabled={busy} />
              : <section className="settings-account-card settings-account-note"><h3>账户余额</h3>
                <p>DeepSeek 官方 API 支持在这里查询余额。其他提供商的余额可在对应服务商的账户中查看。</p>
                <button className="button button-secondary" type="button" onClick={() => onSectionChange?.("models")}>查看模型连接</button>
              </section>}
          </div>}
          {(!section || section === "agents") && <AgentSettingsFields draft={agentDraft} settings={settings} disabled={busy} onChange={(next) => { setAgentDraft(next); setNotice(null); }} />}
          {(!section || section === "models") && <>
          <section className="settings-section settings-connection">
            <h2>连接</h2>
            <label>
              <span>模型提供商</span>
              <select value={provider} disabled={busy} onChange={(event) => changeProvider(event.target.value)}>
                {settings?.providers.map((option) => (
                  <option key={option.id} value={option.id}>{option.label}</option>
                ))}
              </select>
            </label>
            <label>
              <span>连接历史</span>
              <select disabled={busy || providerHistory.length === 0}
                value={providerHistory.some(item => connectionId(item) === selectedConnection) ? selectedConnection : ""}
                onChange={event => selectConnection(event.target.value)}>
                <option value="" disabled>{providerHistory.length ? "选择记住的模型和地址…" : "保存设置后会记住此连接"}</option>
                {providerHistory.map(item => <option key={connectionId(item)} value={connectionId(item)}>
                  {item.model} · {item.baseUrl}
                </option>)}
              </select>
              <small>保存后记住最近 20 组模型 ID 与地址，刷新后仍可选择。API Key 沿用下方密钥设置。</small>
            </label>
            <label>
              <span>模型 ID</span>
              <select
                disabled={busy}
                value={usesCustomModel ? CUSTOM_MODEL_VALUE : model}
                onChange={(event) => {
                  const custom = event.target.value === CUSTOM_MODEL_VALUE;
                  setCustomMode(custom);
                  setModel(custom ? customModel : event.target.value);
                  if (!custom && !modelOptions.includes(event.target.value)) setCustomModel(event.target.value);
                  setNotice(null);
                }}
              >
                {modelOptions.map((item) => <option key={item} value={item}>{item}</option>)}
                {savedModels.length > 0 && <optgroup label="记住的自定义 ID">
                  {savedModels.map(item => <option key={item} value={item}>{item}</option>)}
                </optgroup>}
                <option value={CUSTOM_MODEL_VALUE}>自定义模型 ID…</option>
              </select>
              <small>选择提供商支持的模型，或使用自定义 ID。</small>
            </label>
            {usesCustomModel && (
              <label>
                <span>自定义模型 ID</span>
                <input
                  disabled={busy}
                  value={model}
                  onChange={(event) => {
                    setModel(event.target.value);
                    setCustomModel(event.target.value);
                    setNotice(null);
                  }}
                  placeholder="输入模型 ID"
                  required
                  spellCheck={false}
                />
              </label>
            )}
            <label className="settings-wide">
              <span>Base URL</span>
              <input
                disabled={busy}
                value={baseUrl}
                onChange={(event) => { setBaseUrl(event.target.value); setNotice(null); }}
                list="saved-provider-base-urls"
                type="url"
                placeholder="https://api.example.com"
                required
                spellCheck={false}
              />
              <datalist id="saved-provider-base-urls">
                {savedBaseUrls.map(url => <option key={url} value={url} />)}
              </datalist>
              <small>可输入新地址，或从输入框建议中选择此提供商记住的地址。</small>
            </label>
            {settings?.imageInput && <label className="settings-wide">
              <span>图片输入能力</span>
              <select aria-label="图片输入能力" value={imageInputMode} disabled={busy}
                onChange={event => setImageInputMode(event.target.value as typeof imageInputMode)}>
                <option value="auto">自动识别</option><option value="supported">确认此模型和接口支持图片</option><option value="unsupported">不支持图片输入</option>
              </select>
              <small>自定义接口请按服务商说明确认。此设置只用于当前提供商、地址和模型；连接测试不代表已验证视觉能力。</small>
            </label>}
            <div className="settings-field">
              <label htmlFor="provider-api-key">API Key</label>
              <div className="secret-input">
                <input
                  id="provider-api-key"
                  value={apiKey}
                  onChange={(event) => {
                    setApiKey(event.target.value);
                    if (event.target.value) setClearApiKey(false);
                  }}
                  type={showKey ? "text" : "password"}
                  placeholder={savedKeyConfigured ? `留空则保留 ${settings.apiKeyMask}`
                    : isSavedProvider ? "输入 API Key" : "留空则保留该提供商已保存的密钥（如有）"}
                  autoComplete="new-password"
                  spellCheck={false}
                />
                <button type="button" onClick={() => setShowKey((value) => !value)} aria-label={showKey ? "隐藏密钥" : "显示密钥"}>
                  {showKey ? <EyeOff size={17} /> : <Eye size={17} />}
                </button>
              </div>
              {savedKeyConfigured && (
                <label className="clear-key-check">
                  <input
                    type="checkbox"
                    checked={clearApiKey}
                    onChange={(event) => {
                      setClearApiKey(event.target.checked);
                      if (event.target.checked) setApiKey("");
                    }}
                  />
                  <span>删除已保存的密钥</span>
                </label>
              )}
            </div>
            {!section && provider === "deepseek" && <DeepSeekBalance settings={settings}
              connectionChanged={!isSavedProvider || baseUrl.trim().replace(/\/+$/, "") !== settings?.baseUrl.replace(/\/+$/, "") || Boolean(apiKey.trim()) || clearApiKey}
              disabled={busy} />}
          </section>

          <section className="settings-section compact-grid">
            <h2>单次模型输出</h2>
            <label><span>输出额度</span><select value={outputMode} disabled={busy}
              onChange={event => setOutputMode(event.target.value as "auto" | "manual")}>
              <option value="auto">自动（推荐）</option>
              <option value="manual">手动上限</option>
            </select></label>
            {outputMode === "manual" && <label><span>单次输出 Token 上限</span><input type="number" min="1" max="1048576" step="1" required disabled={busy}
              value={maxOutputTokens} placeholder="例如 32768"
              onChange={event => setMaxOutputTokens(event.target.value)} /></label>}
            <p className="settings-help">{outputMode === "auto"
              ? "根据已确认的模型能力与思考模式选择额度；发生截断时尝试调整额度并拆小步骤，保留已有进度。未知模型使用保守额度。"
              : "每次请求都遵守此上限，恢复时会尝试拆小步骤。上限过小可能导致无法完成；通常建议使用自动模式。"}
              部分模型的思考也占用输出额度。每次请求仍受模型能力、上下文容量和任务总预算限制。</p>
          </section>
          {provider === "deepseek" && (
            <section className="settings-section compact-grid">
              <h2>DeepSeek 推理</h2>
              <label>
                <span>思考模式</span>
                <select value={thinkingMode} onChange={(event) => setThinkingMode(event.target.value)}>
                  <option value="enabled">开启</option>
                  <option value="disabled">关闭</option>
                </select>
              </label>
              <p className="settings-help">思考强度在每个对话的输入栏中单独设置，适用于 Standard、Deep 和 Ultra。</p>
            </section>
          )}
          </>}


          {(!section || section === "general") && (runtimeSettingsSupported
            ? <RuntimeSettingsFields draft={runtimeDraft} onChange={setRuntimeDraft} provider={provider} model={model} disabled={busy} expanded={section === "general"} />
            : <p className="runtime-accounting-note settings-runtime-unavailable">运行预算设置需要新版后端。请重启 MiniCode 本机服务并刷新页面后使用。</p>)}

          {(!section || section === "storage") && <ImageStorageSettings />}

          {(!section || section === "models") && <section className="security-note">
            <ShieldCheck size={18} />
            <div>
              <strong>密钥由本机服务保管</strong>
              <p>页面提交后会立即清空输入框。密钥不进入 localStorage、任务日志或浏览器运行事件。</p>
              <code>{settings?.settingsPath}</code>
            </div>
          </section>}

          {(!section || section === "models" || section === "general" || section === "agents") && <footer>
          {notice && (
            <div className={`settings-notice notice-${notice.kind}`} role="status">
              {notice.kind === "success" ? <Check size={16} /> : <CircleAlert size={16} />}
              <span>{notice.text}</span>
            </div>
          )}

          <div className="settings-actions">
            <button className="button button-secondary" type="button" onClick={() => void testConnection()} disabled={busy || !settings}>
              {testing && <LoaderCircle size={15} className="spin" />}
              {testing ? "正在测试" : "保存并测试连接"}
            </button>
            <button className="button button-primary" type="submit" disabled={busy || !settings}>
              {saving && <LoaderCircle size={15} className="spin" />}
              {saving ? "正在保存" : "保存设置"}
            </button>
          </div>
          </footer>}
        </form>
    </WorkspacePage>
  );
}
