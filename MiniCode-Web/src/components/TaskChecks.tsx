import type { StepCheck } from "../lib/taskChecks";
import "./TaskChecks.css";

function checkLabel(check: StepCheck): string {
  const category = check.category === "functional" ? "功能检查" : "视觉检查";
  return `${category} · ${{ passed: "通过", failed: "未通过", not_checked: "未检查", not_applicable: "不适用" }[check.status]}`;
}

export function TaskCheckSummary({ checks }: { checks: StepCheck[] }) {
  const relevant = checks.filter(check => check.status !== "not_applicable");
  if (!relevant.length) return null;
  return <span className="tool-checks-inline">{relevant.map(check =>
    <span key={check.category} className={`tool-check-inline is-${check.status}`} data-check-category={check.category}>
      <span className="tool-check-status">{checkLabel(check)}</span>
      <span className="tool-check-excerpt" title={check.summary}>{check.summary}</span>
    </span>)}</span>;
}

export function TaskCheckDetails({ checks }: { checks: StepCheck[] }) {
  if (!checks.length) return null;
  return <div className="tool-checks-details">{checks.map(check =>
    <section key={check.category} aria-label={`${check.category === "functional" ? "功能" : "视觉"}检查详情`}>
      <h4>{checkLabel(check)}</h4>
      <p>{check.summary}</p>
      <time dateTime={new Date(check.recordedAt).toISOString()}>记录于 {new Date(check.recordedAt).toLocaleString("zh-CN")}</time>
      {check.evidence.map(evidence => <div className="tool-check-source" key={evidence.toolCallId}>
        <span>{evidence.image?.caption || evidence.image?.name || evidence.toolName}</span>
        {evidence.image?.sourceCreatedAt && <p>引用此前图片；此结论只说明原图中的状态。</p>}
        <code title={evidence.outputSha256}>调用：{evidence.toolCallId}</code>
        {evidence.outputPreview && <pre>{evidence.outputPreview}</pre>}
      </div>)}
    </section>)}</div>;
}
