import { ChevronDown, FileCode2, Files, LoaderCircle, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api/client";
import type { TurnChangedFile, TurnFileChanges } from "../types";
import { TurnUndo } from "./TurnUndo";
import "./RunChanges.css";

export interface ChangeReviewSource { sessionId: string; turnId: string; runId?: string; path?: string }

function LineCounts({ additions, deletions }: { additions: number | null; deletions: number | null }) {
  if (additions === null || deletions === null) return null;
  return <span className="change-line-counts" aria-label={`新增 ${additions} 行，删除 ${deletions} 行`}>
    <span className="change-additions">+{additions.toLocaleString()}</span><span className="change-deletions">−{deletions.toLocaleString()}</span>
  </span>;
}

function ChangeTotals({ changes }: { changes: TurnFileChanges }) {
  return <>{changes.unmeasuredFiles < changes.files.length && <LineCounts additions={changes.additions} deletions={changes.deletions} />}
    {changes.unmeasuredFiles > 0 && <span className="change-unmeasured">含 {changes.unmeasuredFiles} 项未计行数</span>}</>;
}

export function RunChangesBadge({ changes, onReview }: { changes: TurnFileChanges; onReview: () => void }) {
  if (!changes.files.length) return null;
  return <button className="run-changes-badge" onClick={onReview} aria-label="查看本轮文件改动" title="相对本轮开始时的工作区状态，点击审阅">
    <Files size={15} aria-hidden="true" /><span>{changes.files.length} 个文件已更改{changes.partial ? " · 部分" : ""}</span>
    <ChangeTotals changes={changes} />
  </button>;
}

const statusLabels = { A: "新增", M: "修改", D: "删除" };

export function RunChangesCard({ changes, onReview, undoSessionId }: { changes: TurnFileChanges; onReview: (path?: string) => void; undoSessionId?: string }) {
  const [expanded, setExpanded] = useState(false);
  const [undoNotice, setUndoNotice] = useState("");
  if (!changes.files.length && !changes.notice) return null;
  const files = expanded ? changes.files : changes.files.slice(0, 3);
  return <section className="run-changes-card" aria-label="本轮文件改动">
    <header><Files size={20} aria-hidden="true" /><div><strong>{changes.files.length ? `${changes.files.length} 个文件已更改` : "本轮改动统计不可用"}</strong>
      <div className="change-totals"><ChangeTotals changes={changes} /></div></div>
      {changes.files.length > 0 && <div className="change-card-actions">
        {undoSessionId && changes.complete && !changes.partial && <TurnUndo key={`${undoSessionId}:${changes.turnId}`} sessionId={undoSessionId} changes={changes} onNotice={setUndoNotice} />}
        <button className="change-review-button" onClick={() => onReview()}>审阅</button>
      </div>}
    </header>
    {changes.notice && <p className="change-notice">{changes.notice}</p>}
    {undoSessionId && undoNotice && <p className="change-notice">{undoNotice}</p>}
    {!changes.complete && <p className="change-notice">以下为执行中最近记录的快照，尚未完成最终核对。</p>}
    {files.length > 0 && <ul className="run-changes-files">{files.map(file => <li key={file.path}>
      <button onClick={() => onReview(file.path)} aria-label={`查看差异：${file.path}`}>
        <span className={`change-file-status status-${file.status}`} title={statusLabels[file.status]}>{file.status}</span>
        <span className="change-file-path" title={file.path}>{file.path}</span>
        {file.additions === null ? <span className="change-unmeasured">{file.binary ? "二进制" : "未计行数"}</span> : <LineCounts additions={file.additions} deletions={file.deletions} />}
      </button>
    </li>)}</ul>}
    {changes.files.length > 3 && <button className="change-expand" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>
      {expanded ? "收起文件" : `再显示 ${changes.files.length - 3} 个文件`}<ChevronDown size={15} className={expanded ? "is-expanded" : ""} />
    </button>}
  </section>;
}

function FileDiff({ file }: { file: TurnChangedFile }) {
  return <section className="turn-file-diff" aria-label={`文件差异：${file.path}`}>
    <header><FileCode2 size={16} /><code>{file.path}</code><span>{statusLabels[file.status]}</span><LineCounts additions={file.additions} deletions={file.deletions} /></header>
    {file.notice && <p className="change-notice">{file.notice}</p>}
    {file.additions === null && <p className="change-notice">{file.beforeSize.toLocaleString()} → {file.afterSize.toLocaleString()} 字节</p>}
    {file.diff && <pre tabIndex={0} aria-label="代码差异"><code>{file.diff.split("\n").map((line, index) => <span key={index}
      className={line.startsWith("@@") ? "diff-hunk" : line.startsWith("+++") || line.startsWith("---") ? "diff-meta" : line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-delete" : ""}>{line}{"\n"}</span>)}</code></pre>}
    {file.truncated && <p className="change-notice">差异预览已截断；行数统计包含完整的可统计文本。</p>}
  </section>;
}

export function RunChangesReview({ source, onClose }: { source: ChangeReviewSource; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();
  const [changes, setChanges] = useState<TurnFileChanges | null>(null);
  const [selected, setSelected] = useState(source.path || "");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);
  useEffect(() => {
    let canceled = false;
    setLoading(true); setError("");
    void api.getTurnChanges(source.sessionId, source.turnId, source.runId).then(({ changes: next }) => {
      if (canceled) return;
      if (!next || next.turnId !== source.turnId) throw new Error("此轮次没有保存可审阅的文件差异。");
      setChanges(next);
      setSelected(previous => next.files.some(file => file.path === previous) ? previous : next.files[0]?.path || "");
    }).catch(cause => { if (!canceled) setError(cause instanceof Error ? cause.message : "读取差异失败。"); })
      .finally(() => { if (!canceled) setLoading(false); });
    return () => { canceled = true; };
  }, [source.sessionId, source.turnId, source.runId, revision]);
  const file = changes?.files.find(item => item.path === selected);
  return <dialog ref={dialog} className="run-changes-review" aria-labelledby={heading}
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <header><div><h2 id={heading}>本轮文件改动</h2><p>与本轮开始时比较{changes && !changes.complete ? " · 运行中的快照" : " · 已保存的差异"}</p></div>
      <button className="icon-button" aria-label="关闭改动审阅" onClick={onClose}><X size={18} /></button></header>
    <div className="change-review-toolbar">{changes && <><span>{changes.files.length} 个文件</span><ChangeTotals changes={changes} /></>}
      {source.runId && <button disabled={loading} onClick={() => setRevision(value => value + 1)}>刷新快照</button>}
    </div>
    {loading ? <p className="change-review-message" role="status"><LoaderCircle size={16} className="spin" />正在读取差异…</p>
      : error ? <p className="change-review-message" role="alert">{error}<button onClick={() => setRevision(value => value + 1)}>重试</button></p>
        : <>{changes?.notice && <p className="change-notice">{changes.notice}</p>}
          {changes?.files.length ? <div className="change-review-layout"><nav aria-label="改动文件">
            {changes.files.map(item => <button key={item.path} aria-current={selected === item.path ? "true" : undefined} onClick={() => setSelected(item.path)}>
              <span className={`change-file-status status-${item.status}`}>{item.status}</span><span title={item.path}>{item.path}</span>
            </button>)}
          </nav>{file && <FileDiff file={file} />}</div> : <p className="change-review-message">未记录文件内容变化。</p>}
        </>}
  </dialog>;
}
