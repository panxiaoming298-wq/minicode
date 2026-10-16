import { useEffect, useState } from "react";
import { api } from "../api/client";
import { waitForWorktree } from "../lib/worktrees";
import type { WorktreeChanges as Changes } from "../types";
import "./Worktrees.css";

export function WorktreeChanges({ id, runStatus }: { id: string; runStatus: string }) {
  const [changes, setChanges] = useState<Changes | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const active = ["running", "queued", "stopping"].includes(runStatus);
  useEffect(() => { let disposed = false;
    setChanges(null); setError(""); setNotice("");
    void api.worktreeChanges(id).then((value) => { if (!disposed) setChanges(value); })
      .catch((cause) => { if (!disposed) setError(String(cause)); });
    return () => { disposed = true; };
  }, [id, runStatus]);
  async function preview() {
    setBusy(true); setError(""); setNotice("");
    try { setChanges(await api.previewWorktree(id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }
  async function apply() {
    if (!changes?.previewId) return;
    setBusy(true); setError("");
    try {
      await waitForWorktree((await api.applyWorktree(id, changes.previewId, crypto.randomUUID())).operation);
      setChanges(await api.worktreeChanges(id));
      setNotice("已应用回原项目，保留为未提交改动。");
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }
  return <section className="worktree-changes" aria-label="工作树变更">
    <h3>工作树变更</h3><p>相对于{changes?.previewId ? "本次应用预览" : "创建或上次应用时"}的基线。</p>
    {changes?.worktree && <details><summary>创建时的复制范围</summary><p>已跟踪文件的当前内容，以及未忽略的新文件；依赖、缓存和未跟踪的数据库文件除外。</p><p>额外复制的忽略文件：{changes.worktree.includedFiles.join("、") || "无"}。</p></details>}
    <div className="worktree-actions"><button className="trash-button" disabled={busy || active} onClick={() => void preview()}>应用回原项目 · 预览</button>
      {changes?.previewId && <button className="trash-button" disabled={busy || active || Boolean(changes.conflicts.length || changes.outsideProject.length)} onClick={() => void apply()}>确认应用</button>}</div>
    {busy && <p role="status">正在处理工作树…</p>}{notice && <p role="status">{notice}</p>}
    {error && <p className="worktree-error" role="alert">{error}</p>}
    {changes && Boolean(changes.conflicts.length || changes.outsideProject.length) && <p className="worktree-error" role="alert">整次应用已阻止。冲突或范围外文件：{[...changes.conflicts, ...changes.outsideProject].join("、")}</p>}
    {changes?.files.length === 0 && <p>没有尚未应用的修改。</p>}
    {changes?.files.map((file) => <details key={file.path} className="worktree-file"><summary>{file.status} · {file.path}{file.conflict ? " · 冲突" : ""}{file.outsideProject ? " · 项目范围外" : ""}</summary>
      {file.binary ? <p>二进制文件 · {file.beforeSize} B → {file.afterSize} B</p> : <pre>{file.diff || "文件内容相同，权限或状态发生变化。"}</pre>}
      {file.truncated && <p>差异过长，显示内容已截断。</p>}
    </details>)}
  </section>;
}
