import { LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { api, ApiError } from "../api/client";
import type { SearchUsageView } from "../types";

interface Props {
  service: "brave" | "tavily";
  keyId?: string;
  title: string;
  revision: string;
  initial?: SearchUsageView;
  disabled: boolean;
  unsavedSecret: boolean;
}

const date = (value: number) => new Date(value).toLocaleString("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});
const number = (value: number) => value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });

export function SearchKeyUsage({ service, keyId, title, revision, initial, disabled, unsavedSecret }: Props) {
  const [usage, setUsage] = useState(initial);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now);

  useEffect(() => { setUsage(initial); setError(""); setNow(Date.now()); }, [initial, keyId]);
  useEffect(() => {
    const deadlines = [usage?.nextRefreshAt, usage?.checkedAt ? usage.checkedAt + 300_000 : null,
      ...(usage?.metrics.map(metric => metric.resetAt) || [])].filter((at): at is number => at != null && at > now);
    if (!deadlines.length) return;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(2_147_483_647, Math.min(...deadlines) - now + 50));
    return () => window.clearTimeout(timer);
  }, [usage, now]);

  async function refresh() {
    if (!keyId || loading || disabled || unsavedSecret) return;
    setLoading(true);
    setError("");
    try {
      setUsage(await api.refreshSearchUsage(service, keyId, revision, service === "brave"));
      setNow(Date.now());
    } catch (failure) {
      setError(failure instanceof TypeError ? "无法连接本机服务，请重试。"
        : failure instanceof ApiError && failure.status === 404 ? "请先重启后端并重新读取设置。"
        : failure instanceof Error ? failure.message : "暂时无法查询额度。");
    } finally { setLoading(false); }
  }

  if (!keyId || unsavedSecret) return <div className="search-key-usage"><p>保存密钥后可查询剩余额度。</p></div>;
  const cooling = (usage?.nextRefreshAt || 0) > now;
  const stale = usage?.stale || !!error || !!(usage?.checkedAt && now - usage.checkedAt >= 300_000)
    || usage?.metrics.some(metric => metric.resetAt != null && metric.resetAt <= now);

  return <div className="search-key-usage" aria-label={`${title}额度`} aria-busy={loading}>
    <div className="search-usage-heading">
      <span>剩余额度{stale && <span className="search-usage-stale">历史记录</span>}</span>
      <button type="button" disabled={disabled || loading || cooling} className="search-usage-refresh"
        aria-label={`查询${title}额度${service === "brave" ? "（会使用一次搜索）" : ""}`} onClick={() => void refresh()}>
        {loading ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}
        {loading ? "查询中…" : cooling ? "稍后查询" : service === "brave" ? "查询（1 次搜索）" : "查询额度"}
      </button>
    </div>
    {!!usage?.metrics.length && <dl className="search-usage-metrics">
      {usage.metrics.map(metric => <div key={metric.id}>
        <dt>{metric.label}</dt>
        <dd>{metric.unlimited ? "该窗口不限次数" : metric.remaining == null ? "剩余未知"
          : <strong>{number(metric.remaining)} <span>{metric.unit === "credits" ? "积分" : "次"}</span></strong>}
          {metric.limit != null && !metric.unlimited && <span className="search-usage-limit"> / {number(metric.limit)}</span>}
          {metric.used != null && <small>已用 {number(metric.used)}{metric.limit == null ? " · 上限未返回" : ""}</small>}
          {metric.resetAt != null && <small>{metric.resetAt <= now ? "此窗口已到重置时间，请重新查询" : `重置于 ${date(metric.resetAt)}`}</small>}
        </dd>
      </div>)}
    </dl>}
    {(error || usage?.message || !usage) && <p className={error || usage?.status === "error" ? "search-usage-error" : ""} role={error ? "alert" : undefined}>
      {error || usage?.message || "尚未获取额度。"}
    </p>}
    {usage?.checkedAt != null && <p className="search-usage-time">更新于 {date(usage.checkedAt)}{stale ? " · 请刷新后确认" : ""}</p>}
    {cooling && usage?.nextRefreshAt && <p className="search-usage-time">可在 {date(usage.nextRefreshAt)} 再次查询</p>}
  </div>;
}
