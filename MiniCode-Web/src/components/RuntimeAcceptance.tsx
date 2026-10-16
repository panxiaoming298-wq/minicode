import { Check, CircleAlert, Circle, ClipboardCheck, MessageSquare } from "lucide-react";

import type { RuntimeTraceView, VerificationView } from "../types";
import { criterionAssessment, recordedEvidence, verificationLabel } from "../lib/verification";
import { diagnoseRunFailure } from "../lib/runFailure";

function CheckEvidence({ records }: { records: VerificationView[] }) {
  if (!records.length) return null;
  return <details className="acceptance-evidence">
    <summary>查看依据 · {records.length} 条记录</summary>
    <ul>{records.map((record, index) => {
      const evidence = recordedEvidence(record);
      return <li key={`${record.verifier}-${record.createdAt}-${index}`}>
        <strong>{verificationLabel(record)} · {record.projectCheck?.execution === "not-started" ? "未开始" : record.passed ? "通过" : "未通过"}</strong>
        {record.deterministic && record.details && <p className="verification-record-detail">{record.details}</p>}
        {!record.deterministic && <p>模型判断，仅代表本次复核意见。</p>}
        {evidence.length > 0 ? <ul>{evidence.map((item, itemIndex) => <li key={itemIndex}>
          {item.file && <code>{item.file}{item.lineStart != null ? `:${item.lineStart}` : ""}</code>}
          {item.claim && <p>{item.claim}</p>}
          {item.baseRevision && <p>检查版本：<code title={item.baseRevision}>{item.baseRevision.slice(0, 16)}</code></p>}
          {item.contentHash && <p>证据指纹：<code title={item.contentHash}>{item.contentHash.slice(0, 16)}</code></p>}
        </li>)}</ul> : <p>这条记录未附实际检查证据。</p>}
      </li>;
    })}</ul>
  </details>;
}

export function RuntimeAcceptance({ trace }: { trace: RuntimeTraceView }) {
  const criteria = trace.acceptanceCriteria || [];
  const outputs = trace.expectedOutputs || [];
  const records = trace.delivery.verificationRecords;
  const assessments = criteria.map(criterion => ({ ...criterion, ...criterionAssessment(criterion.id, records) }));
  const checkedCount = assessments.filter(criterion => criterion.state === "passed").length;
  const reviewedCount = assessments.filter(criterion => criterion.state === "reviewed").length;
  const unverifiedCount = assessments.filter(criterion => criterion.state === "unverified").length;
  const failedCount = assessments.filter(criterion => criterion.state === "failed").length;
  const failure = trace.delivery.completed ? null : diagnoseRunFailure(trace);
  const reviewPolicy = trace.profile.reviewPolicy;
  const reviewUnavailable = trace.plan.nodes.some(node => node.reviewStatus === "unavailable");
  const verdict = trace.delivery.completed ? "passed" : failure?.kind === "verification" ? "failed" : "pending";
  const reasons = [...new Set([...trace.delivery.reasons, ...trace.scheduler.reasons])];
  const outputEvidence = records.filter((record) => record.verifier === "expected-outputs" && record.scope === "final")
    .flatMap((record) => record.evidence);

  return (
    <section className="inspector-section runtime-acceptance-section" aria-label="交付验收">
      <div className="section-heading"><h2><ClipboardCheck size={15} />执行与检查</h2><span>{trace.delivery.completed ? "执行已完成" : verdict === "failed" ? "发现未通过项" : "验收未完成"}</span></div>
      {trace.taskFile && <p className="summary-note">本轮显式契约 SHA-256：<code>{trace.taskFile.sha256}</code>。仅为契约来源，不代表验收通过。</p>}
      {reviewPolicy && <p className="summary-note">复核范围：{({ basic: "必要检查", focused: "聚焦复核", full: "完整复核" })[reviewPolicy.scope]}
        {reviewPolicy.scope !== "basic" && (reviewPolicy.required ? " · 必需独立复核" : " · 可选独立复核")}</p>}
      <div className={`delivery-verdict is-${verdict}`}>
        {trace.delivery.completed ? <Check size={16} /> : verdict === "failed" ? <CircleAlert size={16} /> : <Circle size={16} />}
        <div><strong>{trace.delivery.completed ? "本轮执行已完成" : "本轮尚未满足交付条件"}</strong>
          {reviewUnavailable && <p>可选独立复核未完成，本轮尚无独立复核结论；交付依据必要检查。</p>}
          {failure && <p>{failure.title}</p>}
          {failure?.reviewPending && <p>{failure.consequence}</p>}
          {failure?.action && <p>{failure.action}</p>}
          {reasons.length > 0 && <details className="acceptance-reasons"><summary>查看原始交付记录</summary><ul>{reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></details>}
        </div>
      </div>
      <h3>任务验收条件 <span>{criteria.length} 项</span></h3>
      {criteria.length > 0 && <p className="acceptance-counts">检查通过 {checkedCount} · AI 意见 {reviewedCount} · 未通过 {failedCount} · 未验证 {unverifiedCount}</p>}
      {criteria.length === 0 ? <p className="section-note">本轮未声明具体的任务验收条件，运行检查通过不代表业务要求已逐项核验。</p> : (
        <ul className="acceptance-list">
          {assessments.map((criterion) => {
            return <li key={criterion.id} className={`is-${criterion.state}`}>
              {criterion.state === "passed" ? <Check size={14} /> : criterion.state === "failed" ? <CircleAlert size={14} /> : criterion.state === "reviewed" ? <MessageSquare size={14} /> : <Circle size={14} />}
              <div><strong>{criterion.text}</strong><p className="criterion-status">{criterion.label}</p><p>{criterion.description}</p><CheckEvidence records={criterion.records} /></div>
            </li>;
          })}
        </ul>
      )}
      {outputs.length > 0 && <>
        <h3>预期产物 <span>{outputs.length} 个</span></h3>
        <ul className="acceptance-list">
          {outputs.map((path, index) => {
            const evidence = path ? outputEvidence.find((item) => item.file === path && item.contentHash) : undefined;
            return <li key={`${index}-${path}`} className={evidence ? "is-passed" : "is-unverified"}>
              {evidence ? <Check size={14} /> : <Circle size={14} />}
              <div><strong>{path || "无效或不在工作区内的产物路径"}</strong><p>{evidence ? "文件已生成并记录内容指纹" : "缺少有效的文件证据"}</p>
                {evidence && <code title={evidence.contentHash}>{evidence.contentHash.slice(0, 16)}</code>}
              </div>
            </li>;
          })}
        </ul>
      </>}
    </section>
  );
}
