import { Cable, CircleAlert, FolderOpen, LoaderCircle, Monitor, RefreshCw, Search, Wrench } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { api } from "../api/client";
import type { CapabilitySnapshot, ConversationScope, ExtensionCatalog, RecentWorkspace } from "../types";
import { capabilityStatusLabel, replayPolicyLabel, toolEffectLabel, toolText } from "../lib/capabilityText";
import { buildToolCatalog, matchesCatalogQuery, type ToolCatalogSource } from "../lib/toolCatalog";
import "./ToolCatalog.css";

interface ToolCatalogProps {
  workspace: string;
  scope: ConversationScope;
  projects: RecentWorkspace[];
}

const sources: Array<{ id: "all" | ToolCatalogSource; label: string }> = [
  { id: "all", label: "全部" }, { id: "builtin", label: "内置工具" },
  { id: "extension", label: "扩展工具" }, { id: "mcp", label: "MCP 工具" },
];

export function ToolCatalog({ workspace, scope, projects }: ToolCatalogProps) {
  const [selectedWorkspace, setSelectedWorkspace] = useState(scope === "project" ? workspace : "");
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<"all" | ToolCatalogSource>("all");
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot | null>(null);
  const [extensions, setExtensions] = useState<ExtensionCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const availableProjects = [...projects];
  if (workspace && !availableProjects.some(project => project.path === workspace)) {
    availableProjects.unshift({ path: workspace, name: workspace.replace(/[\\/]+$/, "").split(/[\\/]/).at(-1) || workspace });
  }

  useEffect(() => {
    let cancelled = false;
    const selectedScope = selectedWorkspace ? "project" : "standalone";
    setLoading(true); setError(""); setCapabilities(null); setExtensions(null);
    void Promise.allSettled([
      api.workspaceCapabilities(selectedWorkspace, selectedScope),
      api.getExtensions(selectedWorkspace, selectedScope),
    ]).then(([snapshot, catalog]) => {
      if (cancelled) return;
      if (snapshot.status === "fulfilled") setCapabilities(snapshot.value.capabilities);
      if (catalog.status === "fulfilled") setExtensions(catalog.value);
      const failures = [snapshot, catalog].flatMap(result => result.status === "rejected"
        ? [result.reason instanceof Error ? result.reason.message : "读取工具目录失败"] : []);
      setError(failures.join("；"));
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [selectedWorkspace, revision]);

  const tools = useMemo(() => buildToolCatalog(capabilities, extensions), [capabilities, extensions]);
  const filtered = useMemo(() => tools.filter(tool => (source === "all" || tool.kind === source) && matchesCatalogQuery(tool, query)), [tools, source, query]);
  const servers = capabilities?.mcpServers || [];
  const registeredCount = tools.filter(tool => tool.status === "ready").length;
  const conditionalCount = tools.filter(tool => tool.status === "conditional").length;

  return (
    <div className="settings-tool-catalog">
      <div className="tool-catalog-scope">
        <label htmlFor="tool-catalog-workspace">查看范围</label>
        <span className="tool-catalog-scope-picker">
          {selectedWorkspace ? <FolderOpen size={16} /> : <Monitor size={16} />}
          <select id="tool-catalog-workspace" value={selectedWorkspace} onChange={event => setSelectedWorkspace(event.target.value)}>
            <option value="">本机全局</option>
            {availableProjects.map(project => <option key={project.path} value={project.path}>项目：{project.name}</option>)}
          </select>
        </span>
        <button type="button" className="icon-button" aria-label="刷新工具目录" title="刷新工具目录" disabled={loading} onClick={() => setRevision(value => value + 1)}>
          <RefreshCw size={17} className={loading ? "spin" : ""} />
        </button>
      </div>
      <p className="tool-catalog-help">{selectedWorkspace ? "包含本项目与继承的全局工具。" : "展示本机全局范围内的工具目录。"}包含基础工具、按模式加载的内置工具，以及已发现的扩展与 MCP 工具。实际调用遵守任务权限、扩展授权和网络状态。</p>
      <div className="tool-catalog-toolbar">
        <label className="tool-catalog-search"><Search size={16} /><input aria-label="搜索工具目录" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索中文名称、工具名或来源" /></label>
        <span className="tool-catalog-count" aria-live="polite">{loading ? "读取中…" : `${filtered.length} / ${tools.length} 个工具`}</span>
      </div>
      {!loading && capabilities && <p className="tool-catalog-help tool-catalog-availability">已注册 {registeredCount} 项 · 按需加载 {conditionalCount} 项。目录数量不代表当前对话可调用的数量。</p>}
      <div className="tool-catalog-filters" role="group" aria-label="工具来源筛选">
        {sources.map(({ id, label }) => <button key={id} type="button" aria-pressed={source === id} onClick={() => setSource(id)}>
          {label}<span>{id === "all" ? tools.length : tools.filter(tool => tool.kind === id).length}</span>
        </button>)}
        {tools.some(tool => tool.kind === "other") && <button type="button" aria-pressed={source === "other"} onClick={() => setSource("other")}>其他<span>{tools.filter(tool => tool.kind === "other").length}</span></button>}
      </div>
      {error && <div className="tool-catalog-error" role="alert"><CircleAlert size={17} /><p>部分工具信息未能读取：{error}</p><button className="button button-secondary" type="button" onClick={() => setRevision(value => value + 1)} disabled={loading}>重试</button></div>}
      {loading ? <div className="tool-catalog-empty" role="status"><LoaderCircle size={22} className="spin" /><p>正在读取工具目录…</p></div>
        : filtered.length === 0 ? <div className="tool-catalog-empty"><Wrench size={24} /><p>{tools.length ? "没有符合筛选条件的工具。" : error ? "工具目录暂时无法完整展示，请重试。" : "这个范围暂未发现工具。"}</p></div>
          : <ul className="tool-catalog-list" aria-label="完整工具目录">
            {filtered.map(tool => {
              const text = toolText(tool);
              const title = text.name === tool.name ? tool.declaredName || text.name : text.name;
              return <li key={tool.id} className="tool-catalog-card">
                <header><div><strong>{title}</strong><code>{tool.name}</code></div><span className={`tool-catalog-status ${tool.status === "ready" ? "is-ready" : "is-pending"}`}>{tool.status === "ready" ? "已注册" : capabilityStatusLabel(tool.status)}</span></header>
                <p>{text.description}</p>
                {tool.activation && <p className="tool-catalog-help tool-catalog-activation"><span>可用条件：</span>{tool.activation}</p>}
                <div className="tool-catalog-meta"><span className={`effect-badge effect-${tool.effect}`}>{toolEffectLabel(tool.effect)}</span><span>{tool.origin}</span>
                  {tool.scope && <span>{tool.scope === "project" ? "本项目" : "全局"}</span>}<span>{replayPolicyLabel(tool.replayPolicy)}</span>{tool.destructive && <span className="tool-catalog-destructive">可能删除或覆盖数据</span>}</div>
                {Boolean(tool.inputFields?.length) && <details className="tool-catalog-parameters"><summary>查看调用参数</summary><dl>{tool.inputFields!.map(field => <div key={field}><dt><code>{field}</code></dt><dd>{tool.requiredInputs?.includes(field) ? "必填" : "可选"}</dd></div>)}</dl></details>}
              </li>;
            })}
          </ul>}
      {(source === "all" || source === "mcp") && <section className="tool-catalog-mcp" aria-labelledby="tool-catalog-mcp-title">
        <h3 id="tool-catalog-mcp-title"><Cable size={17} />MCP 连接<span>{servers.length} 个服务</span></h3>
        {servers.length ? <ul>{servers.map(server => <li key={server.name}><div><strong>{server.name}</strong><span>{capabilityStatusLabel(server.status)}</span></div>
          <p>{server.toolCount ? `${server.toolCount} 个工具` : "此目录尚未获取该服务的工具名称"}{server.protocol ? ` · ${server.protocol}` : ""}</p>{server.error && <p className="inline-error">{server.error}</p>}</li>)}</ul>
          : <p className="tool-catalog-help">此范围没有配置 MCP 服务。</p>}
        {servers.length > 0 && <p className="tool-catalog-help">未连接的服务先展示连接状态；工具列表仅包含已发现的名称。查看目录不会自动启动 MCP 服务。</p>}
      </section>}
    </div>
  );
}
