import { LoaderCircle, RotateCcw, RotateCw } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api/client";
import type { TurnFileChanges, TurnUndoState } from "../types";

export function TurnUndo({ sessionId, changes, onNotice }: { sessionId: string; changes: TurnFileChanges; onNotice: (notice: string) => void }) {
  const [state, setState] = useState<TurnUndoState | null>(null);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const busyRef = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const heading = useId();
  const description = useId();
  useEffect(() => { onNotice(state?.reason || ""); }, [onNotice, state?.reason]);
  useEffect(() => {
    let canceled = false;
    setState(null); setError("");
    void api.getTurnUndo(sessionId, changes.turnId).then(result => {
      if (!result.undo || result.undo.turnId !== changes.turnId) throw new Error("无法读取本轮撤销状态，请重试。");
      if (!canceled) setState(result.undo);
    }).catch(cause => { if (!canceled) setError(cause instanceof Error ? cause.message : "无法读取撤销状态。"); });
    return () => { canceled = true; };
  }, [sessionId, changes.turnId, reload]);
  useEffect(() => {
    if (!confirming) return;
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal();
    cancel.current?.focus();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, [confirming]);
  const operate = async (action: "undo" | "restore") => {
    if (!state || busyRef.current) return;
    busyRef.current = true;
    setBusy(true); setError("");
    try {
      const result = await api.applyTurnUndo(sessionId, changes.turnId, action, state.revision);
      setState(result.undo); setConfirming(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "文件恢复失败，请重试。");
      // A lost response may follow a successful write. Read its durable receipt
      // before enabling another action; never replay the mutation automatically.
      try {
        const result = await api.getTurnUndo(sessionId, changes.turnId);
        if (result.undo) {
          setState(result.undo);
          if ((action === "undo" && result.undo.state === "undone") || (action === "restore" && result.undo.state === "applied")) {
            setError(""); setConfirming(false);
          }
        }
      } catch {
        setState(null);
      }
    } finally {
      busyRef.current = false; setBusy(false);
    }
  };
  const undone = state?.state === "undone";
  return <div className="turn-undo">
    {undone && <span className="turn-undo-status" role="status">已撤销</span>}
    <button className="change-undo-button" disabled={busy || !state || (undone ? !state.canRestore : !state.canUndo)}
      title={state?.reason || (undone ? "恢复刚才撤销的本轮修改" : "撤销最近一轮已完成的修改")}
      onClick={() => { setError(""); if (undone) void operate("restore"); else setConfirming(true); }}>
      {busy ? <LoaderCircle size={14} className="spin" /> : undone ? <RotateCw size={14} /> : <RotateCcw size={14} />}
      {busy ? undone ? "恢复中…" : "撤销中…" : undone ? "恢复修改" : "撤销"}
    </button>
    {error && !confirming && <span className="turn-undo-error" role="alert">{error}
      {!state && <button onClick={() => setReload(value => value + 1)}>重试</button>}</span>}
    {confirming && <dialog ref={dialog} className="turn-undo-confirm" aria-labelledby={heading} aria-describedby={description}
      aria-modal="true" tabIndex={-1} onKeyDown={event => {
        if (event.key !== "Tab") return;
        const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
        const first = buttons[0], last = buttons.at(-1);
        if (!first || !last) { event.preventDefault(); event.currentTarget.focus(); }
        else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }}
      onCancel={event => { event.preventDefault(); if (!busyRef.current) setConfirming(false); }}>
      <h2 id={heading}>撤销本轮修改？</h2>
      <p id={description}>将撤销本轮对 {changes.files.length} 个文件的修改，恢复到本轮开始时的内容。撤销后可以恢复修改。</p>
      <ul>{changes.files.map(file => <li key={file.path}><span>{file.status === "A" ? "移除新增文件" : file.status === "D" ? "恢复删除文件" : "还原修改"}</span><code>{file.path}</code></li>)}</ul>
      {error && <p className="turn-undo-error" role="alert">{error}</p>}
      <div className="turn-undo-confirm-actions">
        <button ref={cancel} disabled={busy} onClick={() => setConfirming(false)}>取消</button>
        <button className="turn-undo-submit" disabled={busy || !state?.canUndo} onClick={() => void operate("undo")}>
          {busy && <LoaderCircle size={14} className="spin" />}{busy ? "撤销中…" : "确认撤销"}
        </button>
      </div>
    </dialog>}
  </div>;
}
