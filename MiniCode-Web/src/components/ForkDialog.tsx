import { useEffect, useRef, useState } from "react";
import { GitFork, LoaderCircle, X } from "lucide-react";
import { api } from "../api/client";
import { waitForWorktree, WorktreeOperationFailure } from "../lib/worktrees";
import type { SessionSummary, SessionTurn } from "../types";
import "./Worktrees.css";

export function ForkDialog({ session, turn, git, onClose, onCreated }: {
  session: SessionSummary; turn: SessionTurn; git: boolean; onClose: () => void;
  onCreated: (id: string) => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const requestId = useRef(crypto.randomUUID());
  const [mode, setMode] = useState<"local" | "worktree">(git ? "worktree" : "local");
  const [ignored, setIgnored] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<{ id: string; files: string[] } | null>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  async function fork() {
    setBusy(true); setError("");
    try {
      const { operation } = await api.forkSession(session.sessionId, turn.turnId, mode, requestId.current,
        ignored.split(/\r?\n/).map((s) => s.trim()).filter(Boolean));
      const done = await waitForWorktree(operation);
      const id = done.sessionId || done.result?.sessionId;
      if (!id) throw new Error("创建结果缺少会话标识。");
      setCreated({ id, files: done.result?.worktree?.includedFiles || [] });
      await onCreated(id);
    } catch (cause) {
      if (cause instanceof WorktreeOperationFailure) requestId.current = crypto.randomUUID();
      setError(cause instanceof Error ? cause.message : "创建失败。");
    }
    finally { setBusy(false); }
  }
  return <dialog className="fork-dialog" ref={dialog} aria-labelledby="fork-heading" onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
    <header><h2 id="fork-heading"><GitFork size={20} />从此处分叉</h2><button className="icon-button" onClick={onClose} disabled={busy} aria-label="关闭分叉窗口"><X size={18} /></button></header>
    <p>对话保留到所选轮次，之后的消息不会继承。</p>
    <p>分叉会继承这些历史的上下文。启用自动整理时，历史较长的任务会先整理上下文，再继续执行。</p>
    <blockquote>{turn.prompt}</blockquote>
    <p>代码从分叉时的当前工作区开始，包含未提交修改。</p>
    <code className="worktree-path">{session.workspace}</code>
    <label>执行环境<select value={mode} disabled={busy || Boolean(created)} onChange={(event) => { setMode(event.target.value as typeof mode); requestId.current = crypto.randomUUID(); }}>
      {git && <option value="worktree">Worktree · 独立工作树</option>}<option value="local">Local · 原目录</option>
    </select></label>
    {mode === "local" ? <p className="worktree-notice">子会话共享此目录；文件修改会直接影响这里的其他会话。</p> : <>
      <p>复制已跟踪文件的当前内容和未忽略的新文件。依赖、构建缓存和运行时数据库不复制。</p>
      <label>额外复制的忽略文件（可选）<textarea value={ignored} disabled={busy || Boolean(created)} onChange={(event) => { setIgnored(event.target.value); requestId.current = crypto.randomUUID(); }} placeholder="每行一个相对于仓库根的路径，例如 .env.local" /></label>
    </>}
    {created && <p role="status">已创建。额外复制：{created.files.join("、") || "无"}。</p>}
    {error && <p role="alert" className="worktree-error">{error}</p>}
    <footer><button className="trash-button" onClick={onClose} disabled={busy}>取消</button><button className="trash-button" disabled={busy} onClick={() => created ? void onCreated(created.id) : void fork()}>
      {busy ? <><LoaderCircle size={15} className="spin" />正在准备…</> : created ? "打开子会话" : "创建分叉"}</button></footer>
  </dialog>;
}
