import { CircleAlert, LoaderCircle, MessageSquare, RotateCcw, Search, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { TrashedSession } from "../types";
import { WorkspacePage, type WorkspacePageNavigation } from "./WorkspacePage";
import "./TrashPage.css";

interface TrashPageProps extends WorkspacePageNavigation {
  revision: number;
  onChanged: (session: TrashedSession, action: "restore" | "delete") => void;
}

function title(session: TrashedSession): string {
  return session.title || session.firstMessage || session.lastMessage || "新对话";
}

function context(session: TrashedSession): string {
  if (session.scope === "standalone") return "独立对话";
  const path = session.projectPath || session.workspace;
  return path.replace(/[\\/]+$/, "").split(/[\\/]/).at(-1) || "项目对话";
}

const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});

export function TrashPage({ onBack, onOpenRail, railCollapsed, revision, onChanged }: TrashPageProps) {
  const [sessions, setSessions] = useState<TrashedSession[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<{ id: string; action: "restore" | "delete" } | null>(null);
  const busyRef = useRef(false);
  const requestRef = useRef(0);
  const [confirmation, setConfirmation] = useState<TrashedSession | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);

  const refresh = useCallback(async () => {
    const request = ++requestRef.current;
    setLoading(true);
    setError("");
    try {
      const result = await api.listTrash();
      if (request === requestRef.current) setSessions(result.sessions);
    } catch (reason) {
      if (request === requestRef.current) setError(reason instanceof Error ? reason.message : "无法读取回收站，请重试。");
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    headingRef.current?.focus();
    void refresh();
    return () => { requestRef.current += 1; };
  }, [refresh, revision]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (confirmation && dialog && !dialog.open) {
      dialog.showModal();
      cancelRef.current?.focus();
    } else if (!confirmation && dialog?.open) dialog.close();
  }, [confirmation]);

  function dismissConfirmation() {
    if (busyRef.current) return;
    setConfirmation(null);
    setDeleteError("");
  }

  async function changeSession(session: TrashedSession, action: "restore" | "delete", worktree: "clean" | "keep" | "discard" = "clean") {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy({ id: session.sessionId, action });
    setError("");
    setDeleteError("");
    setNotice("");
    requestRef.current += 1;
    try {
      if (action === "restore") await api.restoreSession(session.sessionId);
      else await api.deleteSession(session.sessionId, worktree);
      setSessions((current) => current.filter((item) => item.sessionId !== session.sessionId));
      setConfirmation(null);
      setNotice(action === "restore" ? `“${title(session)}”已恢复到${session.scope === "project" ? "原项目" : "对话列表"}。` : `“${title(session)}”已永久删除。`);
      onChanged(session, action);
      headingRef.current?.focus();
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "操作未完成，请重试。";
      if (action === "delete") {
        setDeleteError(message);
        // The server owns whether deletion has started; never offer a restore
        // based on the row's old state after a partially completed deletion.
        void refresh();
      } else setError(message);
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  }

  const query = search.trim().toLocaleLowerCase();
  const visible = sessions.filter((session) => `${title(session)} ${session.workspace} ${context(session)}`.toLocaleLowerCase().includes(query));

  return (
    <WorkspacePage className="trash-page" title="回收站" icon={Trash2} headingId="trash-heading" headingRef={headingRef}
      description="移入的对话会保留在这里。你可以恢复，或确认永久删除。"
      onBack={onBack} onOpenRail={onOpenRail} railCollapsed={railCollapsed}>
        <div className="trash-toolbar">
          <label className="trash-search"><Search size={16} aria-hidden="true" /><span className="sr-only">搜索回收站</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索对话或项目…" />{search && <button onClick={() => setSearch("")} aria-label="清空搜索"><X size={14} /></button>}</label>
          <button className="trash-button" onClick={() => void refresh()} disabled={loading || busy !== null}><RotateCcw size={15} />刷新</button>
        </div>
        {notice && <div className="trash-notice" role="status">{notice}</div>}
        {error && <div className="trash-error" role="alert"><CircleAlert size={17} /><span>{error}</span><button onClick={() => void refresh()} disabled={loading || busy !== null}>重试</button></div>}
        {loading ? <div className="trash-empty" role="status"><LoaderCircle size={24} className="spin" /><p>正在读取回收站…</p></div> : !sessions.length && !error ? (
          <div className="trash-empty"><Trash2 size={30} aria-hidden="true" /><h2>回收站是空的</h2><p>移入回收站的对话会出现在这里。</p><button className="trash-button" onClick={onBack}>返回对话</button></div>
        ) : !visible.length && !error ? (
          <div className="trash-empty"><Search size={28} aria-hidden="true" /><h2>没有找到匹配的对话</h2><button className="trash-button" onClick={() => setSearch("")}>清空搜索</button></div>
        ) : (
          <>
            <p className="trash-count">{query ? `找到 ${visible.length} 条对话` : `${sessions.length} 条对话`}<span>按移入时间排序</span></p>
            <ul className="trash-list" aria-label="回收站对话">
              {visible.map((session) => <li className="trash-row" key={session.sessionId}>
                <MessageSquare size={18} className="trash-row-icon" aria-hidden="true" />
                <div className="trash-row-content"><h2 title={title(session)}>{title(session)}</h2><p><span title={session.workspace}>{context(session)}</span><span>{session.messageCount} 条消息</span></p><p><time dateTime={new Date(session.archivedAt).toISOString()}>{dateFormat.format(session.archivedAt)} 移入</time></p>{session.deletionPending && <p className="trash-pending">删除尚未完成，请重试删除</p>}</div>
                <div className="trash-row-actions">
                  <button className="trash-button" onClick={() => void changeSession(session, "restore")} disabled={busy !== null || session.deletionPending} aria-label={`恢复 ${title(session)}`}><RotateCcw size={15} />{busy?.id === session.sessionId && busy.action === "restore" ? "恢复中…" : "恢复"}</button>
                  <button className="trash-button trash-danger" onClick={() => { setDeleteError(""); setConfirmation(session); }} disabled={busy !== null} aria-label={`永久删除 ${title(session)}`}><Trash2 size={15} />{session.deletionPending ? "重试删除" : "永久删除"}</button>
                </div>
              </li>)}
            </ul>
          </>
        )}
      <dialog ref={dialogRef} className="trash-confirm" aria-labelledby="trash-confirm-heading" aria-describedby="trash-confirm-description" aria-modal="true" onCancel={(event) => { event.preventDefault(); dismissConfirmation(); }} onClose={() => { if (!busyRef.current) setConfirmation(null); }}>
        <div className="trash-confirm-icon"><Trash2 size={23} aria-hidden="true" /></div>
        <h2 id="trash-confirm-heading">永久删除这条对话？</h2>
        <p className="trash-confirm-title">{confirmation ? title(confirmation) : ""}</p>
        <p id="trash-confirm-description">对话记录、工具保存的图片副本、输入历史、会话内的回退检查点及仅归属此对话的自动摘要将被永久删除，无法恢复。项目文件、用户明确保存或纠正的记忆、多个来源的记录及无法关联的旧摘要仍会保留。</p>
        {deleteError && <p className="trash-error" role="alert">{deleteError}</p>}
        {confirmation?.worktreeId && <div className="worktree-notice"><p>工作树有未应用成果时，需要选择如何处理目录。其他会话仍在使用的目录会保留。</p><div className="worktree-actions"><button className="trash-button" disabled={busy !== null} onClick={() => void changeSession(confirmation, "delete", "keep")}>删除对话，保留目录</button><button className="trash-button trash-danger" disabled={busy !== null} onClick={() => void changeSession(confirmation, "delete", "discard")}>删除对话并丢弃工作树</button></div></div>}
        <div className="trash-confirm-actions"><button ref={cancelRef} className="trash-button" onClick={dismissConfirmation} disabled={busy !== null}>取消</button><button className="trash-button trash-delete-confirm" onClick={() => confirmation && void changeSession(confirmation, "delete")} disabled={busy !== null}>{busy?.action === "delete" ? <><LoaderCircle size={15} className="spin" />正在删除…</> : "确认永久删除"}</button></div>
      </dialog>
    </WorkspacePage>
  );
}
