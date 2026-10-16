import { CornerDownRight, ListEnd, LoaderCircle, MoreHorizontal, Pause, Pencil, Play, Trash2 } from "lucide-react";
import { useEffect, useRef } from "react";
import { parseMessage } from "../lib/attachments";
import { SkillText } from "./SkillText";
import { profileLabels } from "../lib/execution";
import type { RunInput, RunStatus } from "../types";

interface RunInputQueueProps {
  inputs: RunInput[];
  runStatus: RunStatus;
  busy: boolean;
  acceptsSteering: boolean;
  resumeBlocked: boolean;
  onSteer?: (item: RunInput) => void;
  onDelete: (item: RunInput) => void;
  onEdit: (item: RunInput) => void;
  onResume?: () => void;
}

export function RunInputQueue({ inputs, runStatus, busy, acceptsSteering, resumeBlocked,
  onSteer, onDelete, onEdit, onResume }: RunInputQueueProps) {
  const root = useRef<HTMLElement>(null);
  const active = ["running", "queued", "stopping"].includes(runStatus);
  const queued = inputs.filter(item => !["completed", "canceled"].includes(item.status));
  const paused = !active && queued.some(item => item.kind !== "steer" && ["held", "accepted"].includes(item.status));
  useEffect(() => {
    function dismiss(event: PointerEvent) {
      root.current?.querySelectorAll<HTMLDetailsElement>("details[open]").forEach(menu => {
        if (!menu.contains(event.target as Node)) menu.open = false;
      });
    }
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, []);
  if (!queued.length) return null;
  return <section className="run-input-queue" aria-label="消息队列" ref={root}>
    {paused && <div className="run-queue-paused" role="status">
      <Pause size={14} aria-hidden="true" />
      <span>{runStatus === "stopped" ? "当前任务已停止，队列已暂停"
        : runStatus === "failed" ? "当前任务未完成，队列已暂停" : "当前任务已结束，队列等待继续"}</span>
      {onResume && <button className="run-queue-resume" onClick={onResume} disabled={busy || resumeBlocked}
        title={resumeBlocked ? "请先确认任务设置和上下文" : "按当前任务设置和新的本轮预算继续队列"}>
        <Play size={13} fill="currentColor" aria-hidden="true" /><span>继续</span>
      </button>}
    </div>}
    <div className="run-input-list">
      {queued.map((item, index) => {
        const message = parseMessage(item.text);
        const editable = ["accepted", "held"].includes(item.status);
        const steering = item.kind === "steer";
        const status = steering ? item.status === "accepted" || item.status === "starting" ? "引导已发送，等待处理"
          : item.status === "applied" ? "正在处理引导" : item.status === "held" ? "引导已暂停，可取回编辑" : "引导未完成"
          : item.status === "starting" || item.status === "applied" ? "正在发送排队消息"
          : item.status === "failed" ? "这条任务未完成" : "";
        return <div className={`run-input-row ${steering ? "is-steering" : ""}`} key={item.inputId}
          role="group" aria-label={`队列消息 ${index + 1}`} data-input-id={item.inputId}>
          {steering && ["accepted", "starting", "applied"].includes(item.status)
            ? <LoaderCircle className="run-input-icon spin" size={16} aria-hidden="true" />
            : <ListEnd className="run-input-icon" size={16} aria-hidden="true" />}
          <div className="run-input-content">
            <p title={message.text || "附件消息"}><SkillText text={message.text || "附件消息"} /></p>
            {message.attachments.length > 0 && <small>{message.attachments.length} 个附件</small>}
            {status && <small role="status">{status}</small>}
          </div>
          <div className="run-input-actions">
            {!steering && editable && onSteer && <button className="run-input-steer" onClick={() => onSteer(item)}
              disabled={busy || !acceptsSteering || item.status !== "accepted"}
              title={acceptsSteering && item.status === "accepted" ? "将这条排队消息发送给正在执行的任务" : "任务运行时可以发送引导"}>
              <CornerDownRight size={16} aria-hidden="true" /><span>引导</span>
            </button>}
            {editable && <button className="run-input-delete" onClick={() => onDelete(item)} disabled={busy}
              aria-label="删除排队消息" title="删除消息"><Trash2 size={16} aria-hidden="true" /></button>}
            <details className="run-input-more" onKeyDown={event => {
              if (event.key === "Escape") {
                event.preventDefault();
                event.currentTarget.open = false;
                event.currentTarget.querySelector("summary")?.focus();
              }
            }}>
              <summary aria-label="消息更多操作" title="更多操作" role="button"><MoreHorizontal size={17} aria-hidden="true" /></summary>
              <div className="run-input-menu">
                {editable && <button onClick={event => {
                  event.currentTarget.closest("details")?.removeAttribute("open");
                  onEdit(item);
                }} disabled={busy}><Pencil size={14} aria-hidden="true" /><span>取回编辑</span></button>}
                <small>{profileLabels[item.executionProfile]} · {steering ? "当前任务引导" : item.queueMode === "manual" ? "等待手动继续" : "当前任务结束后发送"}</small>
                <p>{message.text || "附件消息"}</p>
                {message.attachments.map(file => <small key={file.id}>{file.name}</small>)}
                {item.reason && <small>{item.reason}</small>}
              </div>
            </details>
          </div>
        </div>;
      })}
    </div>
  </section>;
}
