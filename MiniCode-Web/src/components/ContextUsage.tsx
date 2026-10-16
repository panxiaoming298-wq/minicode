import { ArrowUpRight, RefreshCw, X } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ContextStatsView } from "../types";
import { ContextBreakdown, ContextInputMeasurements, contextSourceNote } from "./ContextBreakdown";
import { ConversationContextFields } from "./ConversationContextFields";
import type { ConversationContextSelection } from "../lib/useConversationContext";
import "./ContextUsage.css";

interface ContextUsageProps {
  selection?: ConversationContextSelection;
  settingsDisabled?: boolean;
  context: ContextStatsView | null;
  hasSession: boolean;
  loading: boolean;
  active: boolean;
  refreshing?: boolean;
  refreshError?: boolean;
  onRefresh: () => Promise<void>;
  onOpenDetails: () => void;
}

export function ContextUsage({ context, hasSession, loading, active, refreshing = false, refreshError = false,
  onRefresh, onOpenDetails, selection, settingsDisabled = false }: ContextUsageProps) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0, maxHeight: 400 });
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const stats = context && Number.isFinite(context.totalTokens) && context.totalTokens >= 0
    && Number.isFinite(context.contextWindow) && context.contextWindow > 0
    && Number.isFinite(context.effectiveInput) && context.effectiveInput >= 0 && (context.effectiveInput > 0 || Boolean(context.capacityError))
    && context.effectiveInput <= context.contextWindow ? context : null;
  const capacityError = stats?.capacityError || (stats?.effectiveInput === 1 && stats.reservedTokens == null
    ? "legacy_capacity_unavailable" : "");
  const pressureTokens = stats?.totalTokens ?? 0;
  const budget = stats?.budgetPolicy;
  const denominator = stats ? budget ? stats.contextWindow : stats.effectiveInput : 0;
  const ratio = stats && !capacityError ? pressureTokens / denominator : 0;
  const estimated = Boolean(stats && (!stats.accounting.isExact || stats.accounting.stale || (active && !stats.request)));
  const percent = ratio > 0 && ratio < 0.01 ? "<1%" : `${Math.round(ratio * 100)}%`;
  const amount = capacityError ? capacityError === "legacy_capacity_unavailable" ? "容量需刷新" : "容量配置冲突" : `${estimated ? "约 " : ""}${percent}`;
  const needsCompaction = budget ? pressureTokens >= budget.compactAtTokens : ratio >= 0.85;
  const busy = loading || refreshing;
  const label = `上下文占用：${stats ? amount : busy ? "正在读取" : "暂无统计"}`;
  const capturedAt = stats?.request?.capturedAt;
  const requestTime = capturedAt != null && Number.isFinite(capturedAt) ? new Date(capturedAt) : null;

  useEffect(() => { if (open && hasSession) void onRefresh(); }, [open, hasSession, onRefresh]);

  useLayoutEffect(() => {
    if (!open) return;
    function placePanel() {
      if (!trigger.current || !panel.current) return;
      const anchor = trigger.current.getBoundingClientRect();
      const popup = panel.current.getBoundingClientRect();
      const viewport = window.visualViewport;
      const leftEdge = (viewport?.offsetLeft || 0) + 12;
      const topEdge = (viewport?.offsetTop || 0) + 12;
      const rightEdge = leftEdge + (viewport?.width || window.innerWidth) - 24;
      const bottomEdge = topEdge + (viewport?.height || window.innerHeight) - 24;
      const above = anchor.top - topEdge - 10;
      const below = bottomEdge - anchor.bottom - 10;
      const useAbove = above >= popup.height || above >= below;
      const maxHeight = Math.max(0, useAbove ? above : below);
      setPosition({
        left: Math.max(leftEdge, Math.min(anchor.right - popup.width, rightEdge - popup.width)),
        top: useAbove ? Math.max(topEdge, anchor.top - Math.min(popup.height, maxHeight) - 10) : anchor.bottom + 10,
        maxHeight,
      });
    }
    placePanel();
    const observer = new ResizeObserver(placePanel);
    if (panel.current) observer.observe(panel.current);
    window.addEventListener("resize", placePanel);
    window.addEventListener("scroll", placePanel, true);
    window.visualViewport?.addEventListener("resize", placePanel);
    window.visualViewport?.addEventListener("scroll", placePanel);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", placePanel);
      window.removeEventListener("scroll", placePanel, true);
      window.visualViewport?.removeEventListener("resize", placePanel);
      window.visualViewport?.removeEventListener("scroll", placePanel);
    };
  }, [open, context, busy, refreshError, active]);

  useEffect(() => {
    if (!open) return;
    panel.current?.focus();
    const outside = (event: Event) => {
      if (!trigger.current?.contains(event.target as Node) && !panel.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("focusin", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("focusin", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  function close() { setOpen(false); trigger.current?.focus(); }

  return <div className="context-usage">
    <button ref={trigger} type="button" className="context-usage-trigger" aria-label={label}
      title={`${label}${refreshError ? " · 更新失败，显示上次记录" : stats?.accounting.stale ? " · 记录待更新" : ""}`}
      aria-busy={busy}
      aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? panelId : undefined}
      onClick={() => setOpen(value => !value)}>
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle className="context-usage-track" cx="12" cy="12" r="8.5" strokeWidth="2.5" />
        {stats && <circle className="context-usage-fill" cx="12" cy="12" r="8.5" strokeWidth="2.5"
          pathLength="100" strokeDasharray="100" strokeDashoffset={100 - Math.min(100, ratio * 100)}
          transform="rotate(-90 12 12)" />}
      </svg>
    </button>
    {open && createPortal(<div ref={panel} id={panelId} role="dialog" aria-label="上下文占用" tabIndex={-1}
      className="context-usage-popover" style={position}>
      <header><strong>上下文占用</strong><button type="button" onClick={close} aria-label="关闭上下文占用"><X size={16} /></button></header>
      {stats ? <>
        <div className="context-usage-summary-heading"><span>{stats.request ? budget ? "最近请求输入占用" : "最近请求占用" : "历史会话估算"}</span>
          <span className="context-usage-badge">{stats.accounting.stale ? "待更新" : estimated ? "估算" : "已计量"}</span></div>
        <div className="context-usage-total"><strong>{amount}</strong><span>{estimated ? "约 " : ""}{pressureTokens.toLocaleString()}{capacityError ? " tokens 输入" : ` / ${denominator.toLocaleString()} tokens`}</span></div>
        <div className="context-usage-meter" aria-hidden="true"><i style={{ width: `${Math.min(100, ratio * 100)}%` }} /></div>
        <p className="context-usage-timestamp">{requestTime ? <>请求于 <time dateTime={requestTime.toISOString()}
          title={requestTime.toLocaleString()}>{requestTime.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time></> : "尚无完整请求记录"}
          {active && " · 运行中自动更新"}</p>
        {capacityError && <p className="context-usage-notice" role="alert">{capacityError === "legacy_capacity_unavailable"
          ? "旧记录中的容量计算无效；下次模型请求后更新。输入用量记录仍然保留。"
          : "输出及安全预留已达到或超过上下文窗口，请调整上限。"}</p>}
        {needsCompaction && <p className="context-usage-notice">上下文空间偏紧。后续请求可能需要整理上下文。</p>}
      </> : <p className="context-usage-empty">{busy ? "正在读取上下文统计…" : hasSession ? "暂无上下文统计，可以刷新重试。" : "发送任务后显示上下文占用。"}</p>}
      {selection && <ConversationContextFields selection={selection} active={active} disabled={settingsDisabled} compact />}
      {stats && <details className="context-usage-details"><summary>占用明细与说明</summary>
        <ContextBreakdown context={stats} />
        <ContextInputMeasurements context={stats} />
        <div className="context-usage-capacity"><h3>{stats.request ? "本次请求容量" : "容量说明"}</h3><dl>
            <div><dt>总窗口</dt><dd>{stats.contextWindow.toLocaleString()}</dd></div>
            {stats.outputReserveTokens != null && stats.safetyReserveTokens != null ? <>
              <div><dt>{budget ? stats.request ? "本次输出上限（含思考）" : budget.outputMode === "auto" ? "输出工作余量" : "指定输出预留" : "输出预留"}</dt><dd>{stats.outputReserveTokens.toLocaleString()}</dd></div>
              <div><dt>安全余量</dt><dd>{stats.safetyReserveTokens.toLocaleString()}</dd></div>
            </> : <div><dt>输出与安全余量合计（旧记录未分项）</dt><dd>{capacityError === "legacy_capacity_unavailable" ? "旧记录未保存" : (stats.reservedTokens ?? (stats.contextWindow - stats.effectiveInput)).toLocaleString()}</dd></div>}
            {budget && <div><dt>整理参考线（含保护余量）</dt><dd>{budget.compactAtTokens.toLocaleString()}</dd></div>}
            <div><dt>{budget && stats.request ? "按本次输出额度可容纳输入" : "可用输入"}</dt><dd>{capacityError ? "无法计算有效容量" : stats.effectiveInput.toLocaleString()}</dd></div>
          </dl><p>{budget ? "占用比例按输入占总窗口计算。本次输出上限不代表实际输出用量。" : "占用比例按该请求的可用输入空间计算。"}调整对话上限不会改变这条记录。</p>
        </div>
        <p>{contextSourceNote(stats)}{stats.accounting.stale ? "记录可能尚未更新。" : ""}</p>
        {stats.request && <p>请求模型：{stats.request.model}</p>}
      </details>}
      <p className="context-usage-status" role="status">{refreshError ? stats ? "更新失败，显示上次记录。" : "暂时无法读取上下文统计。" : ""}</p>
      {hasSession && <footer>
        <button type="button" onClick={() => void onRefresh()} disabled={busy}><RefreshCw size={13} className={refreshing ? "spin" : undefined} />刷新统计</button>
        <button type="button" onClick={() => { setOpen(false); onOpenDetails(); }}>查看详情<ArrowUpRight size={13} /></button>
      </footer>}
    </div>, document.body)}
  </div>;
}
