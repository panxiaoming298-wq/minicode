import { Gauge } from "lucide-react";

import type { RuntimeTraceView } from "../types";
import { budgetRemaining, closureReserveLabel, formatCacheRate, formatCost, formatCount, usageCost, usageTokens } from "../lib/runtime";

export function RuntimeUsage({ trace }: { trace: RuntimeTraceView }) {
  const usage = trace.scheduler.usage;
  const { maxTokens, maxCost, deadline } = trace.profile;
  const closure = trace.closureBudget;
  const cache = usage?.cache;
  const tokensLeft = usage?.tokensKnown ? budgetRemaining(maxTokens, usage.tokens, usage.reservedTokens) : null;
  const costLeft = usage?.costKnown ? budgetRemaining(maxCost, usage.cost, usage.reservedCost) : null;

  return (
    <section className="inspector-section runtime-usage-section" aria-label="预算与用量">
      <div className="section-heading"><h2 data-inspector-section="usage" tabIndex={-1}><Gauge size={15} />预算与用量</h2><span>本轮汇总</span></div>
      <p className="section-note">包含主 Agent、团队成员和重试的累计输入与输出，与单次上下文容量分别统计。费用单位为 USD。</p>
      {trace.continuation && <p className="section-note">从任务 {trace.continuation.sourceRunId} 的进度继续；此前{trace.continuation.retainedTokensKnown === false ? "已统计" : "累计"} {formatCount(trace.continuation.retainedTokens)} Token，本轮用量另列。
        {trace.continuation.retainedTokensKnown === false && "历史请求仍有用量未知，未按 0 计算。"}历史失败记录保留。</p>}
      {!usage && <p className="section-empty">这条历史记录没有保存用量。</p>}
      <dl className="compact-metrics usage-metrics">
        <div><dt>已用 Token</dt><dd>{usageTokens(usage)}</dd></div>
        <div><dt>累计费用</dt><dd>{usageCost(usage)}</dd></div>
        <div><dt>Token 上限</dt><dd>{maxTokens == null ? "未设置" : formatCount(maxTokens)}</dd></div>
        <div><dt>费用上限</dt><dd>{maxCost == null ? "未设置" : formatCost(maxCost)}</dd></div>
        <div><dt>请求占用 Token</dt><dd>{formatCount(usage?.reservedTokens)}</dd></div>
        <div><dt>请求占用费用</dt><dd>{formatCost(usage?.reservedCost)}</dd></div>
        <div><dt>剩余 Token</dt><dd>{maxTokens == null ? "未设置上限" : formatCount(tokensLeft)}</dd></div>
        <div><dt>剩余额度</dt><dd>{maxCost == null ? "未设置上限" : formatCost(costLeft)}</dd></div>
        <div><dt>模型请求</dt><dd>{formatCount(usage?.modelCalls)}</dd></div>
        <div><dt>未结算请求</dt><dd>{formatCount(usage?.unsettledCalls)}</dd></div>
      </dl>
      <div className="section-heading"><h3>提示词缓存</h3><span>{cache?.complete ? "提供商已报告" : "按已报告请求统计"}</span></div>
      <dl className="compact-metrics usage-metrics">
        <div><dt>输入缓存命中率</dt><dd>{formatCacheRate(cache?.hitRate)}</dd></div>
        <div><dt>命中 Token</dt><dd>{formatCount(cache?.reportedCalls ? cache.cacheReadTokens : null)}</dd></div>
        <div><dt>已报告请求</dt><dd>{cache ? `${formatCount(cache.reportedCalls)} / ${formatCount(cache.modelCalls)}` : "未知"}</dd></div>
        {cache?.groups.map((group) => <div key={group.key}>
          <dt>{{ primary: "主 Agent", agents: "子 Agent", memory_rerank: "记忆重排", other: "其他辅助请求" }[group.key]}</dt>
          <dd>{formatCacheRate(group.hitRate)}{!group.complete && group.hitRate != null ? "（部分）" : ""}</dd>
        </div>)}
      </dl>
      <p className="section-note">按命中 Token ÷ 已报告输入 Token 计算，包含重试；未报告和未结算请求不按零命中计算。</p>
      {tokensLeft != null && <p className="section-note">剩余额度不等于可以立即使用的额度：下一次完整请求和必要收尾必须能容纳在限制内。</p>}
      {closure && <>
        <dl className="compact-metrics usage-metrics">
          <div><dt>主 Agent 收尾保护</dt><dd>{closureReserveLabel(closure.primaryTokens, closure.primaryCost)}</dd></div>
          <div><dt>最终验证保护</dt><dd>{closureReserveLabel(closure.verificationTokens, closure.verificationCost)}</dd></div>
        </dl>
        <p className="section-note">以上是总预算内的咨询支出边界，不是额外费用、已用量或保证可用的余额。请求占用会另行扣减；上下文仍需逐次检查。</p>
      </>}
      {usage && (!usage.tokensKnown || !usage.costKnown) && (
        <p className="runtime-accounting-note">{usage.unsettledCalls
          ? `部分请求尚未返回最终用量，预留值不代表实际扣费。${maxTokens == null && maxCost == null ? "当前未设置用量上限，已结束请求的未知用量不会单独阻止继续或提交。" : "有限预算需要完整用量，未结算请求可能阻止继续。"}`
          : "用量或计价信息不完整，未知费用不会按 0 计算。"}
          {!usage.costKnown && usage.knownCost != null && ` 已知费用部分：${formatCost(usage.knownCost)}。`}
        </p>
      )}
      {deadline != null && <p className="section-note">截止时间：{new Date(deadline * 1000).toLocaleString("zh-CN")}</p>}
    </section>
  );
}
