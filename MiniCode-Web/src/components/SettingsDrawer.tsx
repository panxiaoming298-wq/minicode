import {
  Check,
  CircleAlert,
  Eye,
  EyeOff,
  KeyRound,
  LoaderCircle,
  ShieldCheck,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { SettingsView } from "../types";

interface SettingsDrawerProps {
  open: boolean;
  settings: SettingsView | null;
  onClose: () => void;
  onSave: (payload: {
    provider: string;
    model: string;
    baseUrl: string;
    apiKey?: string;
    clearApiKey?: boolean;
    thinkingMode?: string;
    reasoningEffort?: string;
  }) => Promise<SettingsView>;
  onTest: () => Promise<{ ok: boolean; latencyMs: number; message: string }>;
}

type Notice = { kind: "success" | "error"; text: string } | null;

const CUSTOM_MODEL_VALUE = "__custom_model__";

export function SettingsDrawer({ open, settings, onClose, onSave, onTest }: SettingsDrawerProps) {
  const [provider, setProvider] = useState("deepseek");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [thinkingMode, setThinkingMode] = useState("enabled");
  const [reasoningEffort, setReasoningEffort] = useState("high");
  const [showKey, setShowKey] = useState(false);
  const [clearApiKey, setClearApiKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);

  useEffect(() => {
    if (!settings || !open) return;
    setProvider(settings.provider);
    setModel(settings.model);
    setBaseUrl(settings.baseUrl);
    setThinkingMode(settings.thinkingMode || "enabled");
    setReasoningEffort(settings.reasoningEffort || "high");
    setApiKey("");
    setClearApiKey(false);
    setShowKey(false);
    setNotice(null);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  const selectedProvider = useMemo(
    () => settings?.providers.find((option) => option.id === provider),
    [provider, settings],
  );
  const modelOptions = selectedProvider?.models ?? [];
  const usesCustomModel = !modelOptions.includes(model);

  function changeProvider(nextProvider: string) {
    const option = settings?.providers.find((item) => item.id === nextProvider);
    setProvider(nextProvider);
    setBaseUrl(option?.defaultBaseUrl || "");
    setModel(option?.models[0] || "");
    setNotice(null);
  }

  async function save(event?: React.FormEvent) {
    event?.preventDefault();
    setSaving(true);
    setNotice(null);
    try {
      await onSave({
        provider,
        model: model.trim(),
        baseUrl: baseUrl.trim(),
        ...(apiKey.trim() ? { apiKey: apiKey.trim() } : {}),
        clearApiKey,
        thinkingMode,
        reasoningEffort,
      });
      setApiKey("");
      setClearApiKey(false);
      setNotice({ kind: "success", text: "设置已保存到本机。" });
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "保存失败。" });
    } finally {
      setSaving(false);
    }
  }

  async function testConnection() {
    setTesting(true);
    setNotice(null);
    try {
      if (apiKey.trim() || clearApiKey || provider !== settings?.provider || model !== settings?.model || baseUrl !== settings?.baseUrl) {
        await onSave({
          provider,
          model: model.trim(),
          baseUrl: baseUrl.trim(),
          ...(apiKey.trim() ? { apiKey: apiKey.trim() } : {}),
          clearApiKey,
          thinkingMode,
          reasoningEffort,
        });
        setApiKey("");
        setClearApiKey(false);
      }
      const result = await onTest();
      setNotice({ kind: "success", text: `${result.message} 耗时 ${result.latencyMs} ms。` });
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "连接测试失败。" });
    } finally {
      setTesting(false);
    }
  }

  return (
    <>
      <button
        className={`drawer-backdrop ${open ? "is-visible" : ""}`}
        onClick={onClose}
        aria-label="关闭设置"
        tabIndex={open ? 0 : -1}
      />
      <aside
        className={`settings-drawer ${open ? "is-open" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        aria-hidden={!open}
        inert={!open}
      >
        <header>
          <div>
            <span className="drawer-icon"><KeyRound size={18} /></span>
            <div>
              <h1 id="settings-title">模型与 API</h1>
              <p>配置 MiniCode 使用的模型提供商</p>
            </div>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭设置"><X size={18} /></button>
        </header>

        <form onSubmit={(event) => void save(event)}>
          <section className="settings-section">
            <h2>连接</h2>
            <label>
              <span>模型提供商</span>
              <select value={provider} onChange={(event) => changeProvider(event.target.value)}>
                {settings?.providers.map((option) => (
                  <option key={option.id} value={option.id}>{option.label}</option>
                ))}
              </select>
            </label>
            <label>
              <span>模型 ID</span>
              <select
                value={usesCustomModel ? CUSTOM_MODEL_VALUE : model}
                onChange={(event) => {
                  setModel(event.target.value === CUSTOM_MODEL_VALUE ? "" : event.target.value);
                  setNotice(null);
                }}
              >
                {modelOptions.map((item) => <option key={item} value={item}>{item}</option>)}
                <option value={CUSTOM_MODEL_VALUE}>自定义模型 ID…</option>
              </select>
              <small>选择提供商支持的模型，或使用自定义 ID。</small>
            </label>
            {usesCustomModel && (
              <label>
                <span>自定义模型 ID</span>
                <input
                  value={model}
                  onChange={(event) => {
                    setModel(event.target.value);
                    setNotice(null);
                  }}
                  placeholder="输入模型 ID"
                  required
                  spellCheck={false}
                />
              </label>
            )}
            <label>
              <span>Base URL</span>
              <input
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                type="url"
                placeholder="https://api.example.com"
                required
                spellCheck={false}
              />
            </label>
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
                  placeholder={settings?.apiKeyConfigured ? `留空则保留 ${settings.apiKeyMask}` : "输入 API Key"}
                  autoComplete="new-password"
                  spellCheck={false}
                />
                <button type="button" onClick={() => setShowKey((value) => !value)} aria-label={showKey ? "隐藏密钥" : "显示密钥"}>
                  {showKey ? <EyeOff size={17} /> : <Eye size={17} />}
                </button>
              </div>
              {settings?.apiKeyConfigured && (
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
              <label>
                <span>推理强度</span>
                <select value={reasoningEffort} onChange={(event) => setReasoningEffort(event.target.value)}>
                  <option value="low">Low</option>
                  <option value="high">High</option>
                  <option value="max">Max</option>
                </select>
              </label>
            </section>
          )}

          <section className="security-note">
            <ShieldCheck size={18} />
            <div>
              <strong>密钥由本机服务保管</strong>
              <p>页面提交后会立即清空输入框。密钥不进入 localStorage、任务日志或浏览器运行事件。</p>
              <code>{settings?.settingsPath}</code>
            </div>
          </section>

          {notice && (
            <div className={`settings-notice notice-${notice.kind}`} role="status">
              {notice.kind === "success" ? <Check size={16} /> : <CircleAlert size={16} />}
              <span>{notice.text}</span>
            </div>
          )}

          <footer>
            <button className="button button-secondary" type="button" onClick={() => void testConnection()} disabled={testing || saving}>
              {testing && <LoaderCircle size={15} className="spin" />}
              {testing ? "正在测试" : "保存并测试连接"}
            </button>
            <button className="button button-primary" type="submit" disabled={saving || testing}>
              {saving && <LoaderCircle size={15} className="spin" />}
              {saving ? "正在保存" : "保存设置"}
            </button>
          </footer>
        </form>
      </aside>
    </>
  );
}
