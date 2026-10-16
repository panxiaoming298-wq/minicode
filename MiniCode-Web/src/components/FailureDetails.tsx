import type { RunFailure } from "../lib/runFailure";
import type { ReactNode } from "react";
import { RunStopLine } from "./RunStopLine";

export function FailureNotice({ failure, children, actions }: { failure: RunFailure; children?: ReactNode; actions?: ReactNode }) {
  if (failure.kind === "canceled") return <RunStopLine />;
  const pending = failure.kind === "verification";
  return <section className="run-failure-notice" role={pending ? "status" : "alert"} aria-label={pending ? "本轮验收待处理" : "本轮执行错误"}>
    <header><strong>{failure.title}</strong></header>
    <FailureDetails failure={failure}>{children}</FailureDetails>
    {actions && <footer className="failure-actions">{actions}</footer>}
  </section>;
}

export function FailureDetails({ failure, children }: { failure: RunFailure; children?: ReactNode }) {
  return <div className="failure-details">
    <p className="summary-reason">{failure.message}</p>
    {failure.action && <p className="summary-reason">{failure.action}</p>}
    <details className="summary-details failure-evidence">
      <summary>{failure.kind === "verification" ? "查看待处理项与复核记录" : "查看错误详情"}</summary>
      {failure.consequence && <p className="summary-note">{failure.consequence}</p>}
      {failure.runId && <p className="summary-note">运行编号：<code>{failure.runId}</code></p>}
      {failure.rawReasons.length ? <ul className="failure-raw-reasons">{failure.rawReasons.map((reason, index) => <li key={index}><pre>{reason}</pre></li>)}</ul>
        : <p className="summary-note">未保存可用的原始停止原因。</p>}
      {children}
    </details>
  </div>;
}
