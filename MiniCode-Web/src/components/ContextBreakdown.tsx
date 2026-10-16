import type { ContextStatsView } from "../types";
import "./ContextBreakdown.css";

const LABELS = {
  system: "系统与规则", tools: "工具定义", conversation: "对话与工具结果",
  memory: "记忆与补充上下文", other: "其他请求内容",
};

export function ContextInputMeasurements({ context }: { context: ContextStatsView }) {
  if (!context.request || !context.budgetPolicy) return null;
  const estimate = context.contextGuardTokens ?? context.admissionInputTokens ?? context.estimatedTokens;
  return <div className="context-usage-capacity">
    <dl aria-label="本次请求输入统计">
      <div><dt>容量保护估算</dt><dd>约 {estimate.toLocaleString()} tokens</dd></div>
      <div><dt>提供商实际输入</dt><dd>{context.accounting.isExact
        ? `${context.totalTokens.toLocaleString()} tokens` : "尚未返回"}</dd></div>
    </dl>
    <p>保护估算包含计量误差余量，用于发送前的容量检查，不表示实际已用空间。提供商实际输入包含缓存输入。</p>
  </div>;
}

export function ContextBreakdown({ context }: { context: ContextStatsView }) {
  const parts = context.request ? context.breakdown || [] : [];
  if (!parts.length) return <p className="context-breakdown-note">历史会话尚无请求分项，下次模型请求后显示。</p>;
  return <div className="context-breakdown">
    <div className="context-breakdown-heading"><span>最近主 Agent 请求</span><span>分项估算 · tokens</span></div>
    <dl aria-label="上下文分项估算">{parts.map(part => <div key={part.key}>
      <dt><i className={`context-part-${part.key}`} aria-hidden="true" />{LABELS[part.key]}</dt>
      <dd>{part.estimatedTokens ? "约 " : ""}{part.estimatedTokens.toLocaleString()}</dd>
    </div>)}</dl>
    {(context.accounting.isExact || context.request?.method === "provider_prefix_delta") && <p className="context-breakdown-note">分项合计约 {context.estimatedTokens.toLocaleString()}，与提供商计量可能不同。</p>}
    {context.request?.hasUnmeasuredMedia && <p className="context-breakdown-note">图片、音频等内容未计入分项估算。</p>}
  </div>;
}

export function contextSourceNote(context: ContextStatsView): string {
  if (context.request || context.budgetPolicy) return (context.accounting.isExact
    ? `总量来自提供商本次请求的输入用量 ${context.totalTokens.toLocaleString()} tokens，包含缓存输入。`
    : context.request?.method === "provider_prefix_delta"
      ? "按提供商已计量的相同请求前缀，加上新增内容估算；实际用量返回后更新。"
      : "按最近主 Agent 请求内容估算，包含消息结构开销；实际用量返回后更新。")
    + (context.budgetPolicy ? "占用比例按输入占总窗口计算，输出含思考并与输入共用该窗口。工具结果增长可能提前触发上下文整理。" : "");
  return context.accounting.isExact ? "来自提供商用量记录。" : "历史会话内容估算，不代表完整请求。";
}
