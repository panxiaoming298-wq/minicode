import { useState } from "react";
import type { VerificationView } from "../types";
import { projectArtifactUrl } from "../lib/projectChecks";
import { ProjectScreenshots } from "./ProjectScreenshots";

const labels: Record<string, string> = { dependencies: "安装依赖", build: "构建项目", browser: "浏览器检查" };

function ReportPreview({ url }: { url: string }) {
  const [body, setBody] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const show = async () => {
    if (open && !error) { setOpen(false); return; }
    setOpen(true);
    if (body && !error) return;
    setLoading(true);
    setError("");
    try {
      const response = await fetch(url);
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "报告暂不可用");
      const text = JSON.stringify(data, null, 2);
      setBody(text.length > 64000 ? text.slice(0, 64000) + "\n…（已达到展示上限）" : text);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "读取报告失败，请重试");
    } finally { setLoading(false); }
  };
  return <div className="project-check-report">
    <button type="button" onClick={show} disabled={loading} aria-expanded={open}>
      {loading ? "正在读取报告…" : error ? "重新读取报告" : open ? "收起检查原始报告" : "查看检查原始报告"}
    </button>
    {open && (error ? <p role="alert">{error}</p> : body && <pre aria-label="检查原始报告">{body}</pre>)}
  </div>;
}

export function ProjectCheckDetails({ record, sessionId, runId, showScreenshots = true }: {
  record: VerificationView; sessionId?: string | null; runId?: string | null; showScreenshots?: boolean;
}) {
  const check = record.projectCheck;
  if (!check) return <p>{record.details || record.scope}</p>;
  return <div className="project-check-details">
    <p>{check.execution === "reused" ? "已复用本轮同版本的通过结果，未重复执行命令。"
      : check.execution === "not-started" ? "检查未开始。" : "已执行项目检查。"}
      {check.candidateRevision && <> 文件版本 <code title={check.candidateRevision}>{check.candidateRevision.slice(0, 12)}</code></>}</p>
    <p className="project-check-scope">{check.scope === "build-only" ? "范围：项目构建。" :
      check.scope === "browser-scenario" ? "范围：本次声明的交互验收项；其他功能未包含在此检查中。" :
      "范围：构建、桌面和手机页面加载；交互功能仍需单独验收。"}</p>
    {!!check.criteria?.length && <ul aria-label="交互验收项">{check.criteria.map(item => <li key={item.number}>
      {item.number}. {item.text} · {item.status === "passed" ? "通过" : item.status === "failed" ? "未通过" : "未检查"}
    </li>)}</ul>}
    {check.failure && <p className="project-check-error">{check.failure}</p>}
    {check.browserErrors.length > 0 && <ul>{check.browserErrors.map((error, i) => <li key={i}>{error}</li>)}</ul>}
    {check.stages.map((stage, i) => <details key={i}>
      <summary>{labels[stage.stage] || stage.stage} · {stage.exitCode === 0 ? "通过" : stage.exitCode === null ? "未记录结果" : `未通过（退出码 ${stage.exitCode}）`}</summary>
      <pre>{stage.output || "无命令输出"}</pre>
    </details>)}
    {showScreenshots && <ProjectScreenshots record={record} sessionId={sessionId} runId={runId} />}
    <div className="project-check-artifacts">{check.artifacts.flatMap(artifact => {
      const url = projectArtifactUrl(sessionId, runId, artifact.artifactId);
      if (!url) return [];
      if (artifact.name === "report.json") return [<ReportPreview key={url} url={url} />];
      return /^(desktop|mobile)-trace\.zip$/.test(artifact.name) ? [
        <a key={url} href={url} download={artifact.name}>下载{artifact.name.startsWith("desktop") ? "桌面" : "手机"}检查过程</a>,
      ] : [];
    })}</div>
  </div>;
}
