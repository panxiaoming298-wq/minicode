import {
  Activity,
  Archive,
  Check,
  CircleAlert,
  Clock3,
  FileClock,
  FlaskConical,
  GitFork,
  LoaderCircle,
  PackageCheck,
  Route,
  ShieldCheck,
} from "lucide-react";
import { useMemo } from "react";

import type {
  ArtifactView,
  RunEvent,
  RunStatus,
  RuntimeNodeView,
  SessionInspect,
  VerificationView,
} from "../types";

interface RuntimePanelProps {
  runStatus: RunStatus;
  events: RunEvent[];
  inspect: SessionInspect | null;
}

function statusIcon(status: string) {
  if (["running", "stopping", "queued", "RUNNING", "CLAIMED", "VERIFYING"].includes(status)) {
    return <LoaderCircle size={15} className="spin" />;
  }
  if (["failed", "FAILED_TERMINAL", "FAILED_RETRYABLE"].includes(status)) return <CircleAlert size={15} />;
  return <Check size={15} />;
}

function stateLabel(status: string): string {
  return {
    idle: "等待任务",
    queued: "排队中",
    running: "运行中",
    stopping: "正在停止",
    completed: "已完成",
    failed: "失败",
    stopped: "已停止",
    PENDING: "待开始",
    READY: "已就绪",
    CLAIMED: "已领取",
    RUNNING: "执行中",
    WAITING_APPROVAL: "等待审批",
    WAITING_INPUT: "等待输入",
    VERIFYING: "验证中",
    FAILED_RETRYABLE: "可重试失败",
    FAILED_TERMINAL: "失败",
    SUCCEEDED: "已完成",
    CANCELED: "已取消",
    SUPERSEDED: "已替代",
  }[status] || status;
}

function eventTitle(event: RunEvent): string {
  if (event.kind === "tool_start") return `调用 ${event.toolName}`;
  if (event.kind === "tool_result") return `${event.toolName} ${event.isError ? "失败" : "完成"}`;
  if (event.kind === "approval_requested") return "请求审批";
  if (event.kind === "approval_resolved") return "审批已处理";
  if (event.kind === "error") return "执行错误";
  if (event.kind === "run_started") return "模型已连接";
  if (event.kind === "capability_snapshot") return "能力清单已同步";
  if (event.kind === "runtime") {
    const category = event.runtime?.category || "runtime";
    const phase = {
      explore: "分析",
      execute: "执行",
      verify: "核验",
    }[event.runtime?.phase || ""] || event.runtime?.phase || "";
    const label = {
      phase: "阶段切换",
      compaction: "上下文整理",
      guard: "结果检查",
      widening: "执行扩展",
      recovery: "恢复处理",
      stop: "停止条件",
    }[category] || "运行时状态";
    return phase ? `${label} · ${phase}` : label;
  }
  return event.content || event.status || "状态更新";
}

function eventDetail(event: RunEvent): string {
  if (event.kind === "run_started") return `${event.provider} / ${event.model}${event.mock ? "（演示）" : ""}`;
  if (event.kind === "capability_snapshot") {
    const capability = event.capabilities;
    return capability ? `${capability.skills.length} Skills · ${capability.mcpServers.length} MCP · ${capability.tools.length} 工具` : "";
  }
  if (event.kind === "runtime") {
    if (event.runtime?.category === "phase") {
      const verification = {
        light: "基础",
        normal: "常规",
        strict: "严格",
      }[event.runtime.verification_focus || ""] || event.runtime.verification_focus || "";
      return verification ? `验证重点：${verification}` : "进入新阶段";
    }
    if (event.runtime?.category === "stop") {
      return event.runtime.stop_reason || event.stopReason || "已满足停止条件";
    }
    return event.runtime?.widening_reason
      || event.runtime?.evidence_summary
      || event.runtime?.message
      || event.content
      || "";
  }
  if (event.kind === "error") return event.message || event.content || "";
  if (event.durationMs !== undefined && event.durationMs !== null) return `${event.durationMs} ms`;
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(event.timestamp);
}

function uniqueVerifications(nodes: RuntimeNodeView[], delivery: VerificationView[]): VerificationView[] {
  const result = [...delivery];
  const seen = new Set(result.map((record) => `${record.verifier}:${record.scope}:${record.details}`));
  nodes.forEach((node) => node.result?.verificationRecords.forEach((record) => {
    const key = `${record.verifier}:${record.scope}:${record.details}`;
    if (!seen.has(key)) {
      seen.add(key);
      result.push(record);
    }
  }));
  return result;
}

function uniqueArtifacts(nodes: RuntimeNodeView[], delivery: ArtifactView[]): ArtifactView[] {
  const result = [...delivery];
  const seen = new Set(result.map((artifact) => artifact.artifactId));
  nodes.forEach((node) => node.result?.artifacts.forEach((artifact) => {
    if (!seen.has(artifact.artifactId)) {
      seen.add(artifact.artifactId);
      result.push(artifact);
    }
  }));
  return result;
}

export function RuntimePanel({ runStatus, events, inspect }: RuntimePanelProps) {
  const trace = inspect?.runtimeRuns.at(-1) || null;
  const effectiveStatus: RunStatus = runStatus === "idle" && trace
    ? trace.delivery.completed
      ? "completed"
      : trace.scheduler.canceled
        ? "stopped"
        : "failed"
    : runStatus;
  const nodes = trace?.plan.nodes || [];
  const verifications = useMemo(
    () => uniqueVerifications(nodes, trace?.delivery.verificationRecords || []),
    [nodes, trace],
  );
  const artifacts = useMemo(
    () => uniqueArtifacts(nodes, trace?.delivery.artifacts || []),
    [nodes, trace],
  );
  const visibleEvents = events.filter((event) => [
    "run_started",
    "capability_snapshot",
    "runtime",
    "tool_start",
    "tool_result",
    "approval_requested",
    "approval_resolved",
    "error",
    "status",
  ].includes(event.kind));

  return (
    <div className="inspector-body run-panel runtime-inspection">
      <div className={`run-overview state-${effectiveStatus}`}>
        {statusIcon(effectiveStatus)}
        <div>
          <strong>{stateLabel(effectiveStatus)}</strong>
          <span>
            {trace
              ? `${trace.profile.selectedRoute || "single"} · ${nodes.length} 个计划节点`
              : `${visibleEvents.length} 条实时事件`}
          </span>
        </div>
      </div>

      {trace && (
        <section className="inspector-section runtime-summary-section">
          <div className="section-heading">
            <h2><Route size={15} />执行拓扑</h2>
            <span>Plan v{trace.plan.planVersion || 1}</span>
          </div>
          <dl className="compact-metrics">
            <div><dt>路线</dt><dd>{trace.profile.selectedRoute || "standard-single"}</dd></div>
            <div><dt>峰值 Worker</dt><dd>{trace.scheduler.peakActiveWorkers}</dd></div>
            <div><dt>领取尝试</dt><dd>{trace.scheduler.claimedAttempts}</dd></div>
            <div><dt>验证等级</dt><dd>{trace.profile.verificationLevel || "light"}</dd></div>
          </dl>
          {trace.topology.reasons[0] && <p className="runtime-rationale"><GitFork size={14} />{trace.topology.reasons[0]}</p>}
        </section>
      )}

      <section className="inspector-section plan-section">
        <div className="section-heading">
          <h2><Activity size={15} />运行计划</h2>
          <span>{nodes.filter((node) => node.state === "SUCCEEDED").length} / {nodes.length}</span>
        </div>
        {nodes.length === 0 ? (
          <p className="section-empty">本轮完成后会在这里保留 Runtime V2 计划与节点状态。</p>
        ) : (
          <ol className="plan-node-list">
            {nodes.map((node, index) => (
              <li key={node.nodeId} className={`node-state-${node.state.toLowerCase()}`}>
                <div className="plan-node-index">{index + 1}</div>
                <div className="plan-node-content">
                  <div><strong>{node.objective}</strong><span>{stateLabel(node.state)}</span></div>
                  <p>{node.role || "worker"} · {node.attemptCount}/{node.maxAttempts} 次尝试{node.dependencies.length ? ` · 依赖 ${node.dependencies.join(", ")}` : ""}</p>
                  {(node.result?.failure || node.rejectionReasons.length > 0) && (
                    <div className="node-failure">
                      {node.result?.failure?.message || node.rejectionReasons[0]}
                    </div>
                  )}
                  {(node.acceptanceCriteria.length > 0 || Boolean(node.result?.changedResources.length)) && (
                    <details>
                      <summary>节点证据</summary>
                      {node.acceptanceCriteria.length > 0 && (
                        <ul>{node.acceptanceCriteria.map((item) => <li key={item}>{item}</li>)}</ul>
                      )}
                      {node.result?.changedResources.length ? (
                        <p>变更：{node.result.changedResources.join("、")}</p>
                      ) : null}
                    </details>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      {(verifications.length > 0 || trace) && (
        <section className="inspector-section verification-section">
          <div className="section-heading">
            <h2><FlaskConical size={15} />验证</h2>
            <span>{verifications.filter((record) => record.passed).length} / {verifications.length} 通过</span>
          </div>
          {verifications.length === 0 ? (
            <p className="section-empty">本轮没有生成独立验证记录。</p>
          ) : (
            <ul className="verification-list">
              {verifications.map((record, index) => (
                <li key={`${record.verifier}-${record.scope}-${index}`} className={record.passed ? "is-passed" : "is-failed"}>
                  {record.passed ? <ShieldCheck size={15} /> : <CircleAlert size={15} />}
                  <div><strong>{record.verifier}</strong><p>{record.details || record.scope}</p></div>
                  <span>{record.passed ? "通过" : "未通过"}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {artifacts.length > 0 && (
        <section className="inspector-section artifact-section">
          <div className="section-heading"><h2><PackageCheck size={15} />工件</h2><span>{artifacts.length} 个</span></div>
          <ul className="artifact-list">
            {artifacts.map((artifact) => (
              <li key={artifact.artifactId}>
                <Archive size={14} />
                <div><strong>{artifact.artifactId}</strong><span>{artifact.mediaType || "application/octet-stream"} · {artifact.size.toLocaleString()} B</span></div>
                <code title={artifact.contentHash}>{artifact.contentHash.slice(0, 10)}</code>
              </li>
            ))}
          </ul>
        </section>
      )}

      {(inspect?.checkpoints.length || 0) > 0 && (
        <section className="inspector-section checkpoint-section">
          <div className="section-heading"><h2><FileClock size={15} />恢复检查点</h2><span>{inspect?.checkpoints.length} 个</span></div>
          <p className="section-note">只展示可恢复边界；旧文件内容不会发送到浏览器。</p>
          <ol className="checkpoint-list">
            {inspect?.checkpoints.slice().reverse().map((checkpoint) => (
              <li key={checkpoint.checkpointId}>
                <Clock3 size={14} />
                <div><strong>{checkpoint.file || "未知文件"}</strong><span>{checkpoint.kind} · {new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(checkpoint.createdAt)}</span></div>
              </li>
            ))}
          </ol>
        </section>
      )}

      <section className="inspector-section event-section">
        <div className="section-heading"><h2><Activity size={15} />实时事件</h2><span>{visibleEvents.length} 条</span></div>
        {visibleEvents.length === 0 ? (
          <p className="section-empty">任务启动后，阶段、工具和审批记录会出现在这里。</p>
        ) : (
          <ol className="event-list">
            {visibleEvents.map((event) => (
              <li key={event.sequence} className={`event-${event.kind}`}>
                <i />
                <div><strong>{eventTitle(event)}</strong><span>{eventDetail(event)}</span></div>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
