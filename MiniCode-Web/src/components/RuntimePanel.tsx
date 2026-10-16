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
  Pause,
} from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import { RuntimeAcceptance } from "./RuntimeAcceptance";
import { isRuntimeStateCheck, verificationLabel } from "../lib/verification";
import { ProjectCheckDetails } from "./ProjectCheckDetails";
import { RuntimeUsage } from "./RuntimeUsage";
import { RuntimeMemory } from "./RuntimeMemory";
import { CandidateRecoveryPanel } from "./CandidateRecoveryPanel";
import { advisoryFailureLabel, executionForRun } from "../lib/execution";
import { AgentBadge, AgentRoster } from "./AgentActivity";
import { agentMessageLabel, agentOwner, collaborationSummary, ownerLabel } from "../lib/agentActivity";
import { formatCount, formatDuration, formatStopReason, runtimeStatus, traceForRun, usageCost, usageTokens } from "../lib/runtime";

import type {
  ArtifactView,
  ExecutionSnapshot,
  RunEvent,
  RunStatus,
  RuntimeNodeView,
  SessionInspect,
  VerificationView,
} from "../types";

interface RuntimePanelProps {
  sessionId?: string | null;
  runStatus: RunStatus;
  runId?: string | null;
  execution?: ExecutionSnapshot | null;
  events: RunEvent[];
  inspect: SessionInspect | null;
}

function statusIcon(status: string) {
  if (["running", "stopping", "queued", "RUNNING", "CLAIMED", "VERIFYING"].includes(status)) {
    return <LoaderCircle size={15} className="spin" />;
  }
  if (["failed", "FAILED_TERMINAL", "FAILED_RETRYABLE"].includes(status)) return <CircleAlert size={15} />;
  if (["completed", "SUCCEEDED"].includes(status)) return <Check size={15} />;
  if (["stopped", "CANCELED", "SUPERSEDED"].includes(status)) return <Pause size={15} />;
  return <Clock3 size={15} />;
}

function stateLabel(status: string): string {
  return {
    idle: "等待任务",
    active: "进行中",
    retired: "已退役",
    blocked: "受阻",
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
  if (event.kind === "execution_snapshot") return "执行安排已更新";
  if (event.kind === "model_status") return event.modelState === "waiting" ? "等待模型响应" : "模型请求结束";
  if (event.kind === "run_input") return event.runInput?.kind === "supplement" ? "补充要求状态更新" : "排队消息状态更新";
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
  if (event.kind === "execution_snapshot" && event.execution) return `${event.execution.profile} · ${event.execution.nodes.filter((node) => node.state === "SUCCEEDED").length} / ${event.execution.nodes.length} 项已完成`;
  if (event.kind === "run_input" && event.runInput) return ({ accepted: "已接收，等待当前执行结束", starting: "准备处理", applied: "已开始处理", completed: "已完成", held: "已暂停，可取回编辑", canceled: "已取回", failed: "未完成" } as Record<string, string>)[event.runInput.status] || "";
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
      return formatStopReason(event.runtime.stop_reason || event.stopReason);
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
  const keyOf = (record: VerificationView) => JSON.stringify([record.verifier, record.scope, record.details, record.passed, record.coveredCriteriaIds]);
  const seen = new Set(result.map(keyOf));
  nodes.forEach((node) => node.result?.verificationRecords.forEach((record) => {
    const key = keyOf(record);
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

export function RuntimePanel({ sessionId, runStatus, runId, execution: suppliedExecution, events, inspect }: RuntimePanelProps) {
  const trace = traceForRun(inspect?.runtimeRuns || [], runId);
  const effectiveStatus = runtimeStatus(runStatus, trace);
  const nodes = trace?.plan.nodes || [];
  const liveExecution = suppliedExecution || [...events].reverse().find((event) => event.kind === "execution_snapshot" && event.execution)?.execution;
  const liveCollaboration = ["queued", "running", "stopping"].includes(runStatus);
  const execution = liveCollaboration ? liveExecution || null : executionForRun(null, trace) || liveExecution || null;
  const team = execution?.agentTeam;
  const agentMessages = (liveCollaboration || !trace ? liveExecution?.agentMessages?.map((message) => ({
    messageId: message.message_id, senderId: message.sender_id, recipientId: message.recipient_id,
    kind: message.kind, state: message.state, sequence: message.sequence, content: "", reason: "",
  })) : undefined) || trace?.agentCollaboration?.messages || [];
  const [agentControlNotice, setAgentControlNotice] = useState("");
  const [stoppingAgent, setStoppingAgent] = useState<string | null>(null);
  const stopAgent = async (taskId: string) => {
    if (!runId || !liveExecution?.executionId) return;
    setStoppingAgent(taskId);
    try {
      const receipt = await api.stopAgent(runId, liveExecution.executionId, taskId, crypto.randomUUID());
      setAgentControlNotice(receipt.notice);
    } catch (error) {
      setAgentControlNotice(error instanceof Error ? error.message : "停止请求失败，请查看运行状态。");
    } finally {
      setStoppingAgent(null);
    }
  };
  const verifications = useMemo(
    () => uniqueVerifications(nodes, trace?.delivery.verificationRecords || []),
    [nodes, trace],
  );
  const actualVerifications = verifications.filter(record => !isRuntimeStateCheck(record));
  const artifacts = useMemo(
    () => uniqueArtifacts(nodes, trace?.delivery.artifacts || []),
    [nodes, trace],
  );
  const visibleEvents = events.filter((event) => [
    "run_started",
    "execution_snapshot",
    "run_input",
    "model_status",
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
            {execution
              ? collaborationSummary(execution)
              : `${visibleEvents.length} 条实时事件`}
          </span>
        </div>
      </div>

      {execution && <AgentRoster execution={execution} status={effectiveStatus} />}
      {trace && <RuntimeAcceptance trace={trace} />}
      {trace && <RuntimeMemory receipt={trace.taskMemory} />}
      {trace ? <RuntimeUsage trace={trace} /> : <section className="inspector-section">
        <div className="section-heading"><h2 data-inspector-section="usage" tabIndex={-1}>预算与用量</h2></div>
        <p className="section-empty">当前运行尚无用量汇总；任务结束后可查看已保存的 Token 与费用记录。</p>
      </section>}
      {(team || agentMessages.length > 0) && <section className="inspector-section" aria-label="父子 Agent 协作">
        <div className="section-heading"><h2><GitFork size={15} />协作控制与消息</h2><span>{stateLabel(team?.state || "") || "异步助手"}</span></div>
        {team && <p>主 Agent 统一派发 · {team.members.filter((member) => member.state !== "retired").length}/{team.member_limit} 名成员</p>}
        {team?.members.map((member) => <div key={member.session_id} className="runtime-summary-section">
          <p>{ownerLabel(agentOwner({ nodeId: member.current_task_id }, execution))} · 第 {member.generation} 次工作 · {stateLabel(member.state)}{member.worker_exited ? " · 执行已退出" : ""}</p>
          <code>{member.current_task_id}</code>
          {runStatus === "running" && ["READY", "PENDING", "CLAIMED", "RUNNING", "WAITING_CHILDREN"].includes(member.state) &&
            <button type="button" disabled={stoppingAgent !== null} onClick={() => void stopAgent(member.current_task_id)}>
              {stoppingAgent === member.current_task_id ? "正在发送停止请求…" : "停止此成员"}
            </button>}
        </div>)}
        {agentControlNotice && <p role="status">{agentControlNotice}</p>}
        {agentMessages.length > 0 && <details><summary>父子消息 · {agentMessages.length} 条</summary>
          <p>已排队表示等待接收；已进入上下文表示已保存检查点，任务结果另行验收。</p>
          <ol>{agentMessages.map((message) => <li key={`${message.senderId}:${message.messageId}`}>
            <p>{agentMessageLabel(message.senderId, execution)} → {agentMessageLabel(message.recipientId, execution)}
              {" · "}{{ queued: "已排队", consumed: "已进入上下文", rejected: "已拒绝" }[message.state] || message.state}</p>
            {message.content && <p>{message.content}</p>}{message.reason && <p>{message.reason}</p>}
          </li>)}</ol>
        </details>}
      </section>}
      {sessionId && <CandidateRecoveryPanel key={`${sessionId}:${trace?.runId || ""}:${runStatus}`}
        sessionId={sessionId} active={["queued", "running", "stopping"].includes(runStatus)} />}

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
          <h2 data-inspector-section="plan" tabIndex={-1}><Activity size={15} />运行计划</h2>
          <span>{nodes.filter((node) => node.state === "SUCCEEDED").length} / {nodes.length} 成功{nodes.some((node) => advisoryFailureLabel(node) && node.diagnosis?.acknowledged) ? " · 含已接管咨询" : ""}</span>
        </div>
        {nodes.length === 0 ? (
          <p className="section-empty">{events.some((event) => event.kind === "execution_snapshot")
            ? "执行安排与阶段在主视图实时展示；本轮结束后，这里显示完整用量与验收汇总。"
            : "本轮运行结束后显示计划、用量与验收汇总；执行过程见下方实时事件。"}</p>
        ) : (
          <ol className="plan-node-list">
            {nodes.map((node, index) => (
              <li key={node.nodeId} className={`node-state-${node.state.toLowerCase()}`}>
                <div className="plan-node-index">{index + 1}</div>
                <div className="plan-node-content">
                  <div><strong>{node.objective}</strong><span>{advisoryFailureLabel(node) || stateLabel(node.state)}</span></div>
                  <p><AgentBadge source={{ nodeId: node.nodeId }} execution={execution} /> · {node.attemptCount}/{node.maxAttempts} 次尝试{node.dependencies.length ? ` · 依赖 ${node.dependencies.join(", ")}` : ""}</p>
                  {node.advisoryReason && <p>主 Agent 接管说明：{node.advisoryReason}</p>}
                  {(node.result?.failure || node.rejectionReasons.length > 0) && (
                    <div className="node-failure">
                      {node.result?.failure?.message || node.rejectionReasons[0]}
                    </div>
                  )}
                  {node.result?.metrics && <p className="node-measurements">
                    末次尝试 · 总耗时 {formatDuration(node.result.metrics.elapsedSeconds)}
                    <br />模型 {formatCount(node.result.metrics.modelCalls)} 轮 / {formatDuration(node.result.metrics.modelSeconds)}
                    {" · "}工具 {formatCount(node.result.metrics.toolCalls)} 次 / {formatDuration(node.result.metrics.toolSeconds)}
                  </p>}
                  {node.result?.usage && <p>末次用量 · {usageTokens(node.result.usage)} Token · {usageCost(node.result.usage)}</p>}
                  {node.result?.warnings.map((warning) => <p className="runtime-accounting-note" key={warning}>{warning}</p>)}
                  {(node.acceptanceCriteria.length > 0 || node.result || trace?.contextManifests.some((item) => item.nodeId === node.nodeId && item.retryFeedback?.length)) && (
                    <details>
                      <summary>节点证据</summary>
                      {node.acceptanceCriteria.length > 0 && (
                        <ul>{node.acceptanceCriteria.map((item) => <li key={item}>{item}</li>)}</ul>
                      )}
                      {node.result?.changedResources.length ? (
                        <p>变更：{node.result.changedResources.join("、")}</p>
                      ) : null}
                      {[...new Set(trace?.contextManifests.filter((item) => item.nodeId === node.nodeId).flatMap((item) => item.retryFeedback || []) || [])].map((feedback) => (
                        <div key={feedback}><p>重试反馈</p><pre className="retry-feedback">{feedback}</pre></div>
                      ))}
                      {trace?.reasoningBudgetDecisions?.filter((decision) => decision.nodeId === node.nodeId).map((decision, decisionIndex) => (
                        <p key={decisionIndex}>推理强度：{decision.requested || "默认"} → {decision.applied || "未应用"}{decision.degraded ? "（已降级）" : ""}{decision.reason ? ` · ${decision.reason}` : ""}</p>
                      ))}
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
            <span>{actualVerifications.length > 0
              ? `${actualVerifications.filter(record => record.passed).length} / ${actualVerifications.length} 通过`
              : `${verifications.length} 条状态记录`}</span>
          </div>
          {verifications.length === 0 ? (
            <p className="section-empty">本轮没有生成独立验证记录。</p>
          ) : (
            <ul className="verification-list">
              {verifications.map((record, index) => {
                const stoppedState = !record.passed && isRuntimeStateCheck(record) && (runStatus === "stopped" || trace?.scheduler.canceled);
                return <li key={`${record.verifier}-${record.scope}-${index}`} className={stoppedState ? "is-pending" : record.passed ? "is-passed" : "is-failed"}>
                  {stoppedState ? <Pause size={15} /> : record.passed ? <ShieldCheck size={15} /> : <CircleAlert size={15} />}
                  <div><strong>{record.verifier === "integrated-runtime-state" ? "运行状态检查" : record.verifier === "npm-build-and-browser" ? "项目构建与页面检查" : verificationLabel(record)}</strong>
                    <p>{isRuntimeStateCheck(record) ? "运行收尾状态，不计入内容或代码检查。" : `${record.scope === "final" ? "最终验收" : "节点检查"} · ${record.deterministic ? "确定性验证" : "模型判断"}`}</p>
                    <ProjectCheckDetails record={record} sessionId={sessionId} runId={trace?.runId || runId} />
                    {record.evidence.filter((item) => item.file).map((item, evidenceIndex) => <p key={evidenceIndex}>{item.file} · <code title={item.contentHash}>{item.contentHash.slice(0, 12)}</code></p>)}
                  </div>
                  <span>{stoppedState ? "已停止" : record.passed ? "通过" : "未通过"}</span>
                </li>;
              })}
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

      <section className="inspector-section checkpoint-section">
        <div className="section-heading"><h2 data-inspector-section="checkpoints" tabIndex={-1}><FileClock size={15} />恢复检查点</h2><span>{inspect?.checkpoints.length || 0} 个</span></div>
        {!inspect?.checkpoints.length && <p className="section-empty">当前会话尚无恢复检查点。</p>}
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

      <section className="inspector-section event-section">
        <div className="section-heading"><h2><Activity size={15} />实时事件</h2><span>{visibleEvents.length} 条</span></div>
        {visibleEvents.length === 0 ? (
          <p className="section-empty">任务启动后，阶段、工具和审批记录会出现在这里。</p>
        ) : (
          <ol className="event-list">
            {visibleEvents.map((event) => (
              <li key={event.sequence} className={`event-${event.kind}`}>
                <i />
                <div><strong>{eventTitle(event)}</strong>{event.nodeId && <AgentBadge source={event} execution={execution} />}<span>{eventDetail(event)}</span></div>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
