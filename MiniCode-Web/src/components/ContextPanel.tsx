import {
  Braces,
  Check,
  CircleAlert,
  Cpu,
  Database,
  Gauge,
  KeyRound,
  Layers3,
  Network,
  PlugZap,
} from "lucide-react";

import type { CapabilitySnapshot, SessionInspect, SettingsView } from "../types";

interface ContextPanelProps {
  workspace: string;
  settings: SettingsView | null;
  capabilities: CapabilitySnapshot | null;
  inspect: SessionInspect | null;
  onOpenSettings: () => void;
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
  return `${(value / 1_048_576).toFixed(1)} MB`;
}

function readinessLabel(status: string): string {
  return {
    ready: "就绪",
    "at-risk": "有风险",
    blocked: "阻塞",
    unknown: "未知",
  }[status] || status || "未知";
}

export function ContextPanel({
  workspace,
  settings,
  capabilities,
  inspect,
  onOpenSettings,
}: ContextPanelProps) {
  const context = inspect?.contextStats;
  const instructions = inspect?.instructionLayers.length
    ? inspect.instructionLayers
    : capabilities?.instructionLayers || [];
  const memory = inspect?.memory.scopes.length ? inspect.memory : capabilities?.memory;
  const readiness = inspect?.readiness || capabilities?.readiness;
  const hooks = inspect?.hookStatus || capabilities?.hookStatus;
  const delegation = inspect?.delegationStatus || capabilities?.delegationStatus;
  const extensions = inspect?.extensions.length ? inspect.extensions : capabilities?.extensions || [];
  const utilization = Math.round((context?.utilization || 0) * 100);

  return (
    <div className="inspector-body context-panel rich-context-panel">
      <section className="inspector-section">
        <div className="section-heading"><h2><Braces size={15} />运行上下文</h2></div>
        <dl className="context-details">
          <div><dt>工作区</dt><dd title={workspace}>{workspace || "未绑定项目（独立对话）"}</dd></div>
          <div><dt>提供商</dt><dd>{settings?.provider || "未配置"}</dd></div>
          <div><dt>模型</dt><dd>{settings?.model || "未配置"}</dd></div>
          <div><dt>Base URL</dt><dd title={settings?.baseUrl}>{settings?.baseUrl || "未配置"}</dd></div>
        </dl>
      </section>

      <section className="inspector-section context-budget-section">
        <div className="section-heading">
          <h2><Gauge size={15} />上下文占用</h2>
          <span>{context ? `${utilization}%` : "暂无记录"}</span>
        </div>
        {context ? (
          <>
            <div className={`context-meter level-${context.warningLevel}`}>
              <i style={{ width: `${Math.max(1, utilization)}%` }} />
            </div>
            <div className="context-token-line">
              <strong>{context.totalTokens.toLocaleString()}</strong>
              <span>/ {context.effectiveInput.toLocaleString()} tokens</span>
            </div>
            <p className="section-note">
              {context.accounting.isExact ? "来自提供商用量记录" : "基于当前会话内容估算"}
              {context.accounting.reason ? ` · ${context.accounting.reason}` : ""}
            </p>
          </>
        ) : <p className="section-empty">选择已有会话后显示 token 估算和上下文窗口。</p>}
      </section>

      <section className="inspector-section">
        <div className="section-heading"><h2><Layers3 size={15} />指令层</h2><span>{instructions.filter((layer) => layer.exists).length} 个生效</span></div>
        <ul className="instruction-layer-list">
          {instructions.map((layer) => (
            <li key={layer.name} className={layer.exists ? "is-active" : "is-inactive"}>
              {layer.exists ? <Check size={14} /> : <span className="empty-dot" />}
              <div><strong>{layer.name}</strong><span>{layer.scope} · {layer.kind}</span></div>
            </li>
          ))}
        </ul>
        {instructions.length === 0 && <p className="section-empty">没有可展示的指令层摘要。</p>}
      </section>

      <section className="inspector-section">
        <div className="section-heading"><h2><Database size={15} />记忆</h2><span>{memory?.scopes.reduce((total, scope) => total + scope.entries, 0) || 0} 条</span></div>
        {memory?.scopes.length ? (
          <ul className="memory-scope-list">
            {memory.scopes.map((scope) => (
              <li key={scope.name}>
                <div><strong>{scope.name}</strong><span>{scope.entries} 条 · {formatBytes(scope.sizeBytes)}</span></div>
                {scope.categories.length > 0 && <p>{scope.categories.join("、")}</p>}
              </li>
            ))}
          </ul>
        ) : <p className="section-empty">当前作用域还没有记忆条目。</p>}
      </section>

      <section className="inspector-section runtime-health-section">
        <div className="section-heading"><h2><Cpu size={15} />运行环境</h2></div>
        <div className={`readiness-line status-${readiness?.status || "unknown"}`}>
          {readiness?.status === "ready" ? <Check size={15} /> : <CircleAlert size={15} />}
          <div><strong>{readinessLabel(readiness?.status || "unknown")}</strong><span>{readiness?.summary || "尚未生成就绪度报告"}</span></div>
        </div>
        <dl className="compact-metrics health-metrics">
          <div><dt><PlugZap size={13} /> Hooks</dt><dd>{hooks?.enabledHooks || 0}/{hooks?.totalHooks || 0}</dd></div>
          <div><dt><Network size={13} /> 委派</dt><dd>{delegation?.runningTasks || 0}/{delegation?.maxSlots || 0}</dd></div>
          <div><dt><Cpu size={13} /> 扩展</dt><dd>{extensions.filter((extension) => extension.enabled).length}</dd></div>
        </dl>
        {readiness?.issues.length ? <ul className="readiness-issues">{readiness.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul> : null}
      </section>

      <section className="security-card inspector-section">
        <h2><KeyRound size={15} />API Key</h2>
        <strong>{settings?.apiKeyConfigured ? settings.apiKeyMask : "未配置"}</strong>
        <p>密钥只保存在本机 MiniCode 设置中，不会写入浏览器存储。</p>
        <button className="button button-secondary" onClick={onOpenSettings}>管理模型与密钥</button>
      </section>
    </div>
  );
}

