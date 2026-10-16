import { LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { ProviderBalance, SettingsView } from "../types";

interface DeepSeekBalanceProps {
  settings: SettingsView | null;
  connectionChanged: boolean;
  disabled: boolean;
}

export function DeepSeekBalance({ settings, connectionChanged, disabled }: DeepSeekBalanceProps) {
  const [balance, setBalance] = useState<ProviderBalance | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const unavailable = !settings ? "正在读取连接设置…"
    : connectionChanged ? "请先保存当前连接设置，再查询余额。"
    : !settings.apiKeyConfigured ? "请先保存 DeepSeek API Key，再查询余额。"
    : settings.balanceQuerySupported === undefined ? "余额查询需要新版后端，请重启本机服务后刷新页面。"
    : !settings.balanceQuerySupported ? "余额查询仅支持 DeepSeek 官方地址 https://api.deepseek.com。"
    : null;

  useEffect(() => {
    setBalance(null);
    setError(null);
    setLoading(false);
    if (unavailable || disabled) return;
    const controller = new AbortController();
    setLoading(true);
    void api.getProviderBalance(controller.signal).then((result) => {
      if (!controller.signal.aborted) setBalance(result);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "查询余额失败，请重试。");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [settings, unavailable, disabled, refresh]);

  // Hide a previous account's result immediately while a connection edit is pending.
  const current = unavailable || disabled ? null : balance;

  return (
    <section className="provider-balance" aria-labelledby="deepseek-balance-title" aria-busy={loading}>
      <div className="provider-balance-heading">
        <h3 id="deepseek-balance-title">DeepSeek 账户余额</h3>
        <button className="button button-secondary" type="button" disabled={Boolean(unavailable) || disabled || loading}
          onClick={() => setRefresh((value) => value + 1)}>
          {loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
          {loading ? "查询中" : "刷新余额"}
        </button>
      </div>
      <div role="status" aria-live="polite">
        {unavailable ? <p className="settings-help">{unavailable}</p>
          : loading ? <p className="settings-help">正在查询账户余额…</p>
          : error ? <p className="provider-balance-error">{error}</p>
          : current && <>
            {current.balances.map((entry, index) => (
              <dl className="provider-balance-values" key={`${entry.currency}-${index}`}>
                <div><dt>可用余额（{entry.currency === "CNY" ? "人民币" : "美元"}）</dt>
                  <dd className="provider-balance-total">{entry.currency === "CNY" ? "¥" : "$"}{entry.totalBalance}</dd></div>
                <div><dt>充值余额</dt><dd>{entry.currency === "CNY" ? "¥" : "$"}{entry.toppedUpBalance}</dd></div>
                <div><dt>赠送余额</dt><dd>{entry.currency === "CNY" ? "¥" : "$"}{entry.grantedBalance}</dd></div>
              </dl>
            ))}
            <p className={`provider-balance-status${current.isAvailable ? "" : " is-unavailable"}`}>
              {current.isAvailable ? "余额可供 API 调用" : "余额不足，请充值后继续使用"}
              <span>更新于 {new Date(current.checkedAt).toLocaleTimeString("zh-CN", { hour12: false })}</span>
            </p>
          </>}
      </div>
      <p className="settings-help">账户金额余额，包含充值与未过期赠金。任务 Token 预算在运行设置中配置。</p>
    </section>
  );
}
