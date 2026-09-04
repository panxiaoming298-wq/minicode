import {
  Boxes,
  Cable,
  CircleAlert,
  LoaderCircle,
  Search,
  Sparkles,
  Wrench,
} from "lucide-react";
import { useMemo, useState } from "react";

import type { CapabilitySnapshot, SessionInspect } from "../types";

interface CapabilityPanelProps {
  capabilities: CapabilitySnapshot | null;
  inspect: SessionInspect | null;
  loading: boolean;
  error: string;
}

function statusTone(status: string): string {
  const value = status.toLowerCase();
  if (["connected", "ready", "active"].includes(value)) return "is-ready";
  if (["error", "failed", "blocked"].includes(value)) return "is-error";
  if (value === "disabled") return "is-muted";
  return "is-pending";
}

function effectLabel(effect: string): string {
  return {
    read_only: "只读",
    workspace_write: "写入工作区",
    external_side_effect: "依赖外部服务",
    privileged: "需授权",
  }[effect] || effect || "未分类";
}

export function CapabilityPanel({ capabilities, inspect, loading, error }: CapabilityPanelProps) {
  const [query, setQuery] = useState("");
  const skills = inspect?.skills.length ? inspect.skills : capabilities?.skills || [];
  const mcpServers = inspect?.mcpServers.length ? inspect.mcpServers : capabilities?.mcpServers || [];
  const tools = capabilities?.tools || [];
  const filteredTools = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return tools;
    return tools.filter((tool) =>
      `${tool.name} ${tool.description} ${tool.effect} ${tool.source}`.toLowerCase().includes(needle),
    );
  }, [query, tools]);

  if (loading && !capabilities) {
    return <div className="inspector-empty"><LoaderCircle size={19} className="spin" /><p>正在读取本地能力清单…</p></div>;
  }
  if (error && !capabilities) {
    return <div className="inspector-empty is-error"><CircleAlert size={19} /><p>{error}</p></div>;
  }
  if (!capabilities && !inspect) {
    return <div className="inspector-empty"><Boxes size={19} /><p>绑定项目或运行一次任务后，这里会显示实际可用能力。</p></div>;
  }

  return (
    <div className="inspector-body capability-panel">
      <div className="capability-summary" aria-label="能力摘要">
        <span><strong>{skills.length}</strong> Skills</span>
        <span><strong>{mcpServers.length}</strong> MCP</span>
        <span><strong>{tools.length}</strong> 工具</span>
      </div>

      <section className="inspector-section capability-section">
        <div className="section-heading">
          <h2><Sparkles size={15} />Skills</h2>
          <span>{skills.length} 个已发现</span>
        </div>
        {skills.length === 0 ? (
          <p className="section-empty">当前作用域没有发现 Skill。</p>
        ) : (
          <ul className="capability-list skill-list">
            {skills.map((skill) => (
              <li key={`${skill.source}-${skill.name}`}>
                <div><strong>{skill.name}</strong><span className="compact-badge">{skill.source}</span></div>
                <p>{skill.description || "没有描述"}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="inspector-section capability-section">
        <div className="section-heading">
          <h2><Cable size={15} />MCP 服务器</h2>
          <span>{mcpServers.filter((server) => server.status === "connected").length} 个已连接</span>
        </div>
        {mcpServers.length === 0 ? (
          <p className="section-empty">当前没有配置 MCP 服务器。</p>
        ) : (
          <ul className="capability-list mcp-list">
            {mcpServers.map((server) => (
              <li key={server.name}>
                <div className="mcp-line">
                  <i className={`status-dot ${statusTone(server.status)}`} />
                  <strong>{server.name}</strong>
                  <span>{server.status}</span>
                </div>
                <p>
                  {server.toolCount} 工具 · {server.resourceCount} 资源 · {server.promptCount} 提示
                  {server.protocol ? ` · ${server.protocol}` : ""}
                </p>
                {server.error && <p className="inline-error">{server.error}</p>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="inspector-section capability-section tool-surface-section">
        <div className="section-heading">
          <h2><Wrench size={15} />工具面</h2>
          <span>{filteredTools.length} / {tools.length}</span>
        </div>
        <p className="capability-availability-note">
          这里显示模型可以尝试调用的工具，不代表外部服务正处于可用状态。web_search 和 web_fetch 仍会受网络、目标站点及人机验证影响。
        </p>
        {tools.length > 8 && (
          <label className="capability-search">
            <Search size={14} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选工具" />
          </label>
        )}
        {filteredTools.length === 0 ? (
          <p className="section-empty">没有符合筛选条件的工具。</p>
        ) : (
          <ul className="tool-surface-list">
            {filteredTools.map((tool) => (
              <li key={tool.name}>
                <div>
                  <code>{tool.name}</code>
                  <span className={`effect-badge effect-${tool.effect}`}>{effectLabel(tool.effect)}</span>
                </div>
                <p>{tool.description}</p>
                <small>{tool.source} · {tool.replayPolicy || "无自动重放"}</small>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
