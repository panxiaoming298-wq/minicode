import { CircleCheck, CircleAlert, Minus } from "lucide-react";
import type { VerificationView } from "../types";
import { projectCoverage } from "../lib/projectChecks";
import { ProjectCheckDetails } from "./ProjectCheckDetails";
import { ProjectScreenshots } from "./ProjectScreenshots";
import "./ProjectCheck.css";

export function ProjectCheckCard({ record, sessionId, runId, active = false }: {
  record: VerificationView; sessionId?: string | null; runId?: string | null; active?: boolean;
}) {
  if (!record.projectCheck) return null;
  const check = record.projectCheck;
  const time = record.createdAt > 0 && Number.isFinite(record.createdAt) ? new Date(record.createdAt) : null;
  return <section className="project-check-card" aria-label="项目检查与截图" data-run-id={runId}>
    <header><strong>项目检查与截图{record.verifier.startsWith("browser-scenario-") && <> · {record.verifier.slice("browser-scenario-".length)}</>}</strong>{time && <time dateTime={time.toISOString()}>{time.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</time>}</header>
    <ul className="project-check-coverage" aria-label="检查范围">
      {projectCoverage(record).map(item => <li key={item.label} className={item.state === "通过" ? "is-passed" : item.state === "未通过" ? "is-failed" : "is-unchecked"}>
        {item.state === "通过" ? <CircleCheck size={15} /> : item.state === "未通过" ? <CircleAlert size={15} /> : <Minus size={15} />}
        <span>{item.label}</span><strong>{item.state}</strong>
      </li>)}
    </ul>
    {!record.passed && <p className="project-check-error">项目检查未通过{check.failure ? `：${check.failure}` : "，请展开详情核对。"}</p>}
    {check.execution === "reused" && <p className="project-check-note">已复用本轮同版本的检查结果，未重复构建或截图。</p>}
    <ProjectScreenshots key={`${runId}:${record.createdAt}`} record={record} sessionId={sessionId} runId={runId} />
    <p className="project-check-note">{active ? "任务仍在进行。这是本轮检查时的快照，后续修改需重新检查。" :
      check.scope === "build-only" ? "此记录只包含构建检查。" :
      check.scope === "browser-scenario" ? "交互结果对应本次声明的验收项，详情中列出实际覆盖范围。" :
      "截图对应本轮检查时的文件版本。页面能加载不代表交互功能已通过。"}</p>
    <details className="project-check-diagnostics"><summary>检查详情与原始报告</summary>
      <ProjectCheckDetails record={record} sessionId={sessionId} runId={runId} showScreenshots={false} />
    </details>
  </section>;
}
