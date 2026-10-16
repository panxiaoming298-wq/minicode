import {
  ArrowUpRight,
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
import {
  capabilitySourceLabel,
  capabilityStatusLabel,
  matchesToolQuery,
  replayPolicyLabel,
  toolEffectLabel,
  toolText,
  toolSource,
} from "../lib/capabilityText";

interface CapabilityPanelProps {
  capabilities: CapabilitySnapshot | null;
  inspect: SessionInspect | null;
  loading: boolean;
  error: string;
  onOpenTools?: () => void;
}

function statusTone(status: string): string {
  const value = status.toLowerCase();
  if (["connected", "ready", "active"].includes(value)) return "is-ready";
  if (["error", "failed", "blocked"].includes(value)) return "is-error";
  if (value === "disabled") return "is-muted";
  return "is-pending";
}

export function CapabilityPanel({ capabilities, inspect, loading, error, onOpenTools }: CapabilityPanelProps) {
  const [query, setQuery] = useState("");
  const skills = inspect?.skills.length ? inspect.skills : capabilities?.skills || [];
  const mcpServers = inspect?.mcpServers.length ? inspect.mcpServers : capabilities?.mcpServers || [];
  const tools = capabilities?.tools || [];
  const filteredTools = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return tools;
    return tools.filter((tool) => matchesToolQuery(tool, needle));
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
        <span><strong>{skills.length}</strong> 技能</span>
        <span><strong>{mcpServers.length}</strong> MCP</span>
        <span><strong>{tools.length}</strong> 工具</span>
      </div>

      <section className="inspector-section capability-section">
        <div className="section-heading">
          <h2><Sparkles size={15} />技能</h2>
          <span>{skills.length} 个已发现</span>
        </div>
        {skills.length === 0 ? (
          <p className="section-empty">当前作用域没有发现技能。</p>
        ) : (
          <ul className="capability-list skill-list">
            {skills.map((skill) => (
              <li key={`${skill.source}-${skill.name}`}>
                <div><strong>{skill.name}</strong><span className="compact-badge">{capabilitySourceLabel(skill.source)}</span></div>
                <p>{skill.description || "没有描述"}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="inspector-section capability-section">
        <div className="section-heading">
          <h2 data-inspector-section="mcp" tabIndex={-1}><Cable size={15} />MCP 服务器</h2>
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
                  <span>{capabilityStatusLabel(server.status)}</span>
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
          <h2 data-inspector-section="tools" tabIndex={-1}><Wrench size={15} />当前范围的工具</h2>
          <span>{filteredTools.length} / {tools.length}</span>
        </div>
        <p className="capability-availability-note">
          这里展示当前范围内发现的工具。实际调用遵守任务权限；网页搜索、网页读取等外部能力还受网络、目标站点及人机验证影响。
        </p>
        {onOpenTools && <button type="button" className="capability-catalog-link" onClick={onOpenTools}>查看全部工具<ArrowUpRight size={13} /></button>}
        {tools.length > 8 && (
          <label className="capability-search">
            <Search size={14} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="筛选工具" placeholder="按中文名称或工具名筛选" />
          </label>
        )}
        {filteredTools.length === 0 ? (
          <p className="section-empty">没有符合筛选条件的工具。</p>
        ) : (
          <ul className="tool-surface-list">
            {filteredTools.map((tool) => {
              const text = toolText(tool);
              return (
                <li key={tool.name}>
                  <div>
                    <span className="tool-surface-name">
                      {text.name !== tool.name && <strong>{text.name}</strong>}
                      <code title={tool.name}>{tool.name}</code>
                    </span>
                    <span className={`effect-badge effect-${tool.effect}`}>{toolEffectLabel(tool.effect)}</span>
                  </div>
                  <p>{text.description}</p>
                  <small>{capabilitySourceLabel(toolSource(tool))} · {replayPolicyLabel(tool.replayPolicy)}</small>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
