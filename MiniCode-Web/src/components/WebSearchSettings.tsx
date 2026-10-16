import { Check, CircleAlert, Eye, EyeOff, LoaderCircle, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { api, ApiError } from "../api/client";
import type { SearchKeyUpdate, SearchUsageView, WebSearchProvider, WebSearchSettingsView } from "../types";
import { SearchKeyUsage } from "./SearchKeyUsage";
import "./WebSearchSettings.css";

const services = [
  { id: "brave", label: "Brave Search", docs: "https://api-dashboard.search.brave.com/api-reference/web/search/get" },
  { id: "tavily", label: "Tavily", docs: "https://docs.tavily.com/documentation/api-reference/endpoint/search" },
] as const;
type Service = typeof services[number]["id"];
type KeyDraft = SearchKeyUpdate & { localId: string; apiKey: string; apiKeyMask: string; visible: boolean; usage?: SearchUsageView };
type KeyLists = Record<Service, KeyDraft[]>;

function drafts(view: WebSearchSettingsView): KeyLists {
  const rows = (service: Service) => view[service].keys.map(row => ({ ...row, localId: row.id, apiKey: "", visible: false }));
  return { brave: rows("brave"), tavily: rows("tavily") };
}

function errorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 404) return "当前后端尚未支持搜索设置，请重启 MiniCode 本机服务后重试。";
  if (error instanceof TypeError) return "无法连接本机服务，请确认服务正在运行后重试。";
  return error instanceof Error ? error.message : "无法读取或保存搜索设置，请重试。";
}

export function WebSearchSettings() {
  const [saved, setSaved] = useState<WebSearchSettingsView | null>(null);
  const [provider, setProvider] = useState<WebSearchProvider>("auto");
  const [keys, setKeys] = useState<KeyLists>({ brave: [], tavily: [] });
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  function accept(view: WebSearchSettingsView) {
    setSaved(view);
    setProvider(view.provider);
    setKeys(drafts(view));
    setConflict(false);
  }

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setNotice(null);
    api.getSearchSettings(controller.signal).then(view => {
      if (controller.signal.aborted) return;
      if (!view.brave.keys || !view.tavily.keys) throw new Error("当前后端尚未支持多密钥，请重启 MiniCode 本机服务后重新读取。");
      accept(view);
    }).catch(error => {
      if (!controller.signal.aborted) setNotice({ kind: "error", text: errorMessage(error) });
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [reload]);

  function edit(service: Service, localId: string, patch: Partial<KeyDraft>) {
    setKeys(current => ({ ...current, [service]: current[service].map(row => row.localId === localId ? { ...row, ...patch } : row) }));
    setNotice(null);
  }

  function add(service: Service) {
    if (!saved || keys[service].length >= saved.maxKeysPerService) return;
    const row: KeyDraft = { localId: crypto.randomUUID(), name: `密钥 ${keys[service].length + 1}`, enabled: true,
      apiKey: "", apiKeyMask: "", visible: false };
    setKeys(current => ({ ...current, [service]: [...current[service], row] }));
    setNotice(null);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!saved || saving || conflict) return;
    setSaving(true);
    setNotice(null);
    const payload = (service: Service): SearchKeyUpdate[] => keys[service].map(row => ({
      ...(row.id ? { id: row.id } : {}), name: row.name.trim(), enabled: row.enabled,
      ...(row.apiKey.trim() ? { apiKey: row.apiKey.trim() } : {}),
    }));
    try {
      const next = await api.saveSearchSettings({
        revision: saved.revision,
        ...(saved.providerSource !== "environment" ? { provider } : {}),
        ...(saved.brave.apiKeySource !== "environment" ? { braveKeys: payload("brave") } : {}),
        ...(saved.tavily.apiKeySource !== "environment" ? { tavilyKeys: payload("tavily") } : {}),
      });
      accept(next);
      setNotice({ kind: "success", text: "搜索设置已保存，新发起的任务将使用此配置。" });
    } catch (error) {
      setConflict(error instanceof ApiError && error.status === 409);
      setNotice({ kind: "error", text: errorMessage(error) });
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <div className="search-settings-loading" role="status"><LoaderCircle size={17} className="spin" />正在读取搜索设置…</div>;
  if (!saved) return <div className="search-settings-unavailable">
    <p className="settings-notice notice-error" role="alert"><CircleAlert size={17} />{notice?.text || "暂时无法读取搜索设置。"}</p>
    <button className="button button-secondary" type="button" onClick={() => setReload(value => value + 1)}>重新读取</button>
  </div>;

  const missingKey = (provider === "brave" || provider === "tavily")
    && !keys[provider].some(row => row.enabled && (row.id || row.apiKey.trim()));
  const dirty = provider !== saved.provider || services.some(service => {
    const original = saved[service.id].keys;
    return keys[service.id].length !== original.length || keys[service.id].some((row, index) => {
      const before = original[index];
      return !before || row.id !== before.id || row.name !== before.name || row.enabled !== before.enabled || row.apiKey !== "";
    });
  });

  return <form className="settings-form search-settings" onSubmit={event => void save(event)} aria-label="联网搜索设置" aria-busy={saving}>
    <section className="settings-section">
      <label htmlFor="web-search-provider">搜索来源</label>
      <select id="web-search-provider" value={provider} disabled={saving || saved.providerSource === "environment"}
        onChange={event => { setProvider(event.target.value as WebSearchProvider); setNotice(null); }}>
        <option value="auto">自动（推荐）</option>
        <option value="public">公开搜索（无需密钥）</option>
        <option value="brave">优先使用 Brave Search</option>
        <option value="tavily">优先使用 Tavily</option>
      </select>
      <p className="settings-help">{provider === "auto"
        ? "优先使用已配置的 Brave、Tavily；没有启用的密钥或服务不可用时，尝试公开搜索。"
        : provider === "public" ? "使用公开搜索入口，无需密钥；结果和可用性可能受网络、限流影响。"
        : "优先使用所选服务；服务不可用时尝试公开搜索。搜索服务密钥与模型密钥分别配置。"}</p>
      {saved.providerSource === "environment" && <p className="search-settings-source">搜索来源由启动环境变量指定，需在启动环境中修改。</p>}
      {saved.configurationError && <p className="settings-notice notice-error" role="alert">{saved.configurationError}</p>}
      {missingKey && <p className="search-settings-source" role="status">所选服务至少需要一把启用的密钥。请添加或启用密钥，或选择自动、公开搜索后保存。</p>}
    </section>

    {services.map(service => {
      const environment = saved[service.id].apiKeySource === "environment";
      const rows = keys[service.id];
      const enabled = rows.filter(row => row.enabled).length;
      return <section className="settings-section search-provider-card" key={service.id} aria-labelledby={`search-${service.id}-heading`}>
        <div className="search-provider-heading">
          <h3 id={`search-${service.id}-heading`}>{service.label}</h3>
          <span className={`search-key-status ${enabled ? "is-configured" : ""}`}>
            {rows.length ? `${rows.length} 把密钥 · ${enabled} 把启用` : "未配置"}
          </span>
        </div>
        <p className="search-settings-source">{environment
          ? "启动环境密钥优先于本机保存的列表。请先在启动环境中移除该变量，再管理多把密钥。"
          : `按列表顺序使用已启用的密钥，认证失败或限流时尝试下一把。每家最多保存 ${saved.maxKeysPerService} 把。`}</p>
        <div className="search-key-list">
          {rows.map((row, index) => {
            const inputId = `search-${service.id}-${row.localId}`;
            const title = `${service.label} 密钥 ${index + 1}`;
            return <div className={`search-key-row ${row.enabled ? "" : "is-disabled"}`} key={row.localId} role="group" aria-label={title}>
              <div className="search-key-row-heading">
                <span className="search-key-index">{index + 1}{!row.id && <span>待保存</span>}</span>
                <div className="search-key-controls">
                  <label className="search-key-enabled">
                    <input type="checkbox" checked={row.enabled} disabled={saving || environment} aria-label={`启用${title}`}
                      onChange={event => edit(service.id, row.localId, { enabled: event.target.checked })} />
                    <span>{row.enabled ? "已启用" : "已停用"}</span>
                  </label>
                  {!environment && <button className="search-key-remove" type="button" disabled={saving} aria-label={`移除${title}`}
                    onClick={() => { setKeys(current => ({ ...current, [service.id]: current[service.id].filter(item => item.localId !== row.localId) })); setNotice(null); }}>
                    <Trash2 size={14} /><span>移除</span>
                  </button>}
                </div>
              </div>
              <div className="search-key-fields">
                <div className="settings-field">
                  <label htmlFor={`${inputId}-name`}>名称</label>
                  <input id={`${inputId}-name`} aria-label={`${title}名称`} value={row.name} maxLength={64}
                    disabled={saving || environment} placeholder="例如：主用、备用"
                    onChange={event => edit(service.id, row.localId, { name: event.target.value })} />
                </div>
                <div className="settings-field">
                  <label htmlFor={inputId}>API Key</label>
                  <div className="secret-input">
                    <input id={inputId} aria-label={`${title} API Key`} type={row.visible ? "text" : "password"} value={row.apiKey}
                      onChange={event => edit(service.id, row.localId, { apiKey: event.target.value })} disabled={saving || environment}
                      placeholder={row.id ? `已保存 ${row.apiKeyMask}` : "输入 API Key"} required={!row.id}
                      autoComplete="new-password" spellCheck={false} maxLength={4096} aria-describedby={environment ? undefined : `search-${service.id}-help`} />
                    <button type="button" disabled={saving || environment} aria-label={`${row.visible ? "隐藏" : "显示"}${title}新输入的密钥`}
                      onClick={() => edit(service.id, row.localId, { visible: !row.visible })}>
                      {row.visible ? <EyeOff size={17} /> : <Eye size={17} />}
                    </button>
                  </div>
                </div>
              </div>
              <SearchKeyUsage service={service.id} keyId={row.id} title={title} revision={saved.revision} initial={row.usage}
                disabled={saving || conflict} unsavedSecret={!!row.apiKey.trim()} />
            </div>;
          })}
          {!rows.length && <p className="search-keys-empty">尚未添加密钥。可添加主用和备用密钥，暂时不用的可以停用。</p>}
        </div>
        {!environment && <div className="search-key-list-footer">
          <button className="button button-secondary" type="button" disabled={saving || rows.length >= saved.maxKeysPerService}
            onClick={() => add(service.id)}><Plus size={14} />添加 {service.label} 密钥</button>
          <small id={`search-${service.id}-help`}>已保存的密钥留空保留，输入新值可替换。移除和停用在保存后生效。</small>
        </div>}
        <p className="search-settings-source">{service.id === "brave"
          ? "正常搜索会自动更新额度记录。手动查询会发起一次搜索，成功时计入用量；打开设置不会自动发起查询。"
          : "Tavily 按积分计费，当前普通搜索每次消耗 1 积分。账户额度可能由多把密钥共享，各项额度不能直接相加。"}</p>
        <a className="search-service-docs" href={service.docs} target="_blank" rel="noreferrer">{service.label} 服务文档 ↗</a>
      </section>;
    })}

    <section className="security-note">
      <ShieldCheck size={18} />
      <div><strong>密钥由本机服务保管</strong>
        <p>保存后输入框会清空，页面只显示掩码。已保存不代表服务连接已验证，实际可用性由搜索结果确认。</p>
      </div>
    </section>
    <footer>
      {notice && <div className={`settings-notice notice-${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}>
        {notice.kind === "success" ? <Check size={16} /> : <CircleAlert size={16} />}<span>{notice.text}</span>
      </div>}
      <div className="settings-actions">
        {conflict ? <button className="button button-secondary" type="button" disabled={saving}
          onClick={() => setReload(value => value + 1)}>重新读取（放弃修改）</button>
          : dirty && <button className="button button-secondary" type="button" disabled={saving}
            onClick={() => { accept(saved); setNotice(null); }}>放弃修改</button>}
        <button className="button button-primary" type="submit" disabled={saving || missingKey || conflict}>
          {saving && <LoaderCircle size={15} className="spin" />}{saving ? "正在保存" : "保存搜索设置"}
        </button>
      </div>
    </footer>
  </form>;
}
