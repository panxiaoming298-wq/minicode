import {
  Braces,
  Check,
  CircleAlert,
  Cpu,
  Gauge,
  KeyRound,
  Network,
  PlugZap,
} from "lucide-react";

import type { CapabilitySnapshot, ContextStatsView, InstructionLayerView, SessionInspect, SettingsView } from "../types";
import { ContextBreakdown, ContextInputMeasurements, contextSourceNote } from "./ContextBreakdown";
import { ContextKnowledge } from "./ContextKnowledge";

interface ContextPanelProps {
  sessionId?: string;
  workspace: string;
  settings: SettingsView | null;
  capabilities: CapabilitySnapshot | null;
  inspect: SessionInspect | null;
  contextStats?: ContextStatsView | null;
  onOpenSettings: () => void;
  onOpenRule?: (rule: InstructionLayerView) => void;
}

function readinessLabel(status: string): string {
  return {
    ready: "模型配置就绪",
    warning: "模型配置需检查",
    "at-risk": "模型配置需检查",
    blocked: "模型配置未就绪",
  }[status] || "尚未检查模型配置";
}

function providerLabel(provider: string): string {
  return {
    deepseek: "DeepSeek",
    openai: "OpenAI",
    anthropic: "Anthropic",
    openrouter: "OpenRouter",
    custom: "自定义接口",
    unknown: "未识别提供商",
  }[provider] || provider || "未配置提供商";
}

export function ContextPanel({
  sessionId,
  workspace,
  settings,
  capabilities,
  inspect,
  contextStats,
  onOpenSettings,
  onOpenRule,
}: ContextPanelProps) {
  const context = contextStats ?? inspect?.contextStats;
  const capacityError = context?.capacityError || (context?.effectiveInput === 1 && context.reservedTokens == null
    ? "legacy_capacity_unavailable" : "");
  const readiness = inspect?.readiness || capabilities?.readiness;
  const readinessChecked = readiness && ["ready", "warning", "at-risk", "blocked"].includes(readiness.status);
  const readinessStatus = readinessChecked ? readiness.status : "unknown";
  const hooks = inspect?.hookStatus || capabilities?.hookStatus;
  const delegation = inspect?.delegationStatus || capabilities?.delegationStatus;
  const extensions = inspect?.extensions.length ? inspect.extensions : capabilities?.extensions || [];
  const utilization = Math.round((context?.utilization || 0) * 100);
  const pressureEstimated = !context?.accounting.isExact || context?.accounting.stale;

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
          <span>{capacityError ? "容量配置待核对" : context ? `${pressureEstimated ? "约 " : ""}${utilization}%` : "暂无记录"}</span>
        </div>
        {context ? (
          <>
            <div className={`context-meter level-${context.warningLevel}`}>
              <i style={{ width: `${Math.max(1, utilization)}%` }} />
            </div>
            <div className="context-token-line">
              <strong>{pressureEstimated ? "约 " : ""}{context.totalTokens.toLocaleString()}</strong>
              <span>{capacityError ? "tokens 输入 · 容量计算无效" : `/ ${(context.budgetPolicy ? context.contextWindow : context.effectiveInput).toLocaleString()} tokens`}</span>
            </div>
            <ContextBreakdown context={context} />
            <ContextInputMeasurements context={context} />
            <p className="section-note">
              总窗口 {context.contextWindow.toLocaleString()} tokens · {context.outputReserveTokens != null && context.safetyReserveTokens != null
                ? `${context.budgetPolicy ? context.request ? "本次输出上限（含思考）" : "输出工作余量" : "输出预留"} ${context.outputReserveTokens.toLocaleString()} · 安全余量 ${context.safetyReserveTokens.toLocaleString()}`
                : `输出与安全余量合计 ${capacityError === "legacy_capacity_unavailable" ? "旧记录未保存" : (context.reservedTokens ?? Math.max(0, context.contextWindow - context.effectiveInput)).toLocaleString()}（旧记录未分项）`}
            </p>
            {context.budgetPolicy && <p className="section-note">整理参考线（含保护余量）{context.budgetPolicy.compactAtTokens.toLocaleString()} tokens。
              输出与输入共用总窗口，输出上限不代表实际用量。</p>}
            <p className="section-note">
              {contextSourceNote(context)}
            </p>
          </>
        ) : <p className="section-empty">选择已有会话后显示 token 估算和上下文窗口。</p>}
      </section>

      <ContextKnowledge key={sessionId || workspace} sessionId={sessionId} requestId={context?.request?.requestId} onOpenRule={onOpenRule}
        initial={{ rules: inspect ? inspect.instructionLayers : capabilities?.instructionLayers || [],
          memory: (inspect ? inspect.memory : capabilities?.memory) || { schema: "", capturedAt: "", scopes: [] },
          sources: inspect?.contextSources, taskNotes: inspect?.taskNotes }} />

      <section className="inspector-section runtime-health-section">
        <div className="section-heading"><h2 data-inspector-section="health" tabIndex={-1}><Cpu size={15} />运行环境</h2></div>
        <div className={`readiness-line status-${readinessStatus}`}>
          {readinessStatus === "ready" ? <Check size={15} /> : <CircleAlert size={15} />}
          <div>
            <strong>{readinessLabel(readinessStatus)}</strong>
            {readinessChecked ? <>
              <span>{providerLabel(readiness.provider)} · 主模型配置{readiness.providerReady ? "通过" : "待完善"}</span>
              <span>{readiness.fallbackCandidates.length
                ? `备用模型配置：${readiness.viableFallbacks.length} / ${readiness.fallbackCandidates.length} 通过`
                : "暂无备用模型"}</span>
            </> : <span>尚未生成就绪度报告</span>}
          </div>
        </div>
        {readinessChecked && <p className="section-note">仅检查本机配置，实际可用性以模型请求结果为准。</p>}
        <dl className="compact-metrics health-metrics">
          <div>
            <dt><PlugZap size={13} /> 自动化钩子（Hooks）</dt>
            <dd>{hooks ? hooks.totalHooks ? `${hooks.enabledHooks} / ${hooks.totalHooks} 已启用` : "未配置" : "暂无数据"}</dd>
            <dd className="health-metric-note">在工具执行、会话保存等事件发生时自动运行脚本。</dd>
          </div>
          <div>
            <dt><Network size={13} /> 后台命令</dt>
            <dd>{delegation ? `${delegation.runningTasks} 运行 / ${delegation.maxSlots} 槽位` : "暂无数据"}</dd>
            <dd className="health-metric-note">后台命令共用这些槽位。子 Agent 数量请查看“子代理”。</dd>
          </div>
          <div>
            <dt><Cpu size={13} /> 扩展</dt>
            <dd>{inspect || capabilities ? `${extensions.filter((extension) => extension.enabled).length} 个已启用` : "暂无数据"}</dd>
          </div>
        </dl>
        {readiness?.issues.length ? <ul className="readiness-issues">{readiness.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul> : null}
        {readiness?.summary && <details className="readiness-diagnostics">
          <summary>诊断详情</summary>
          <code>{readiness.summary}</code>
        </details>}
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
