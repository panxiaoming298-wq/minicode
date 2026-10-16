import { useEffect, useRef, useState } from "react";
import { Images, LoaderCircle, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { messagePreview } from "../lib/attachments";
import type { ImageStorageSession, ImageStorageView } from "../types";
import { ImageStorageSessionRow } from "./ImageStorageSessionRow";
import "./ImageStorageSettings.css";

function bytes(value: number | null): string {
  if (value === null) return "用量未知";
  if (value < 1024) return `${value} B`;
  const unit = Math.min(3, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** unit).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${["B", "KB", "MB", "GB"][unit]}`;
}

export function ImageStorageSettings() {
  const [view, setView] = useState<ImageStorageView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmation, setConfirmation] = useState<ImageStorageSession | null>(null);
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const busyRef = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);

  async function refresh() {
    request.current?.abort();
    const controller = request.current = new AbortController();
    setLoading(true);
    try {
      const next = await api.imageStorage(controller.signal);
      if (controller.signal.aborted || !mounted.current) return;
      if (!Array.isArray(next.sessions) || !Number.isFinite(next.storedBytes)) {
        throw new Error("图片用量数据无法读取，请确认前后端版本一致后刷新。");
      }
      setView(next);
      setConfirmation(current => current ? next.sessions.find(row => row.sessionId === current.sessionId) || null : null);
    } catch (reason) {
      if (!controller.signal.aborted && mounted.current) setError(reason instanceof Error ? reason.message : "无法读取图片用量，请刷新重试。");
    } finally {
      if (!controller.signal.aborted && mounted.current) setLoading(false);
    }
  }

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => { mounted.current = false; request.current?.abort(); };
  }, []);

  useEffect(() => {
    if (!confirmation) return;
    const previous = document.activeElement;
    dialog.current?.showModal();
    cancel.current?.focus();
    return () => {
      dialog.current?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, [Boolean(confirmation)]);

  async function clear() {
    if (!confirmation || busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const result = await api.clearSessionImages(confirmation.sessionId, confirmation.revision);
      window.dispatchEvent(new CustomEvent("minicode:images-cleared", { detail: { sessionId: confirmation.sessionId } }));
      if (!mounted.current) return;
      setNotice(`已清理 ${result.clearedCount} 张图片副本，释放 ${bytes(result.releasedBytes)}。文字记录仍保留。`);
      setConfirmation(null);
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "清理未完成，请刷新用量后重试。");
    } finally {
      if (mounted.current) { await refresh(); setBusy(false); }
      busyRef.current = false;
    }
  }

  return <section className="settings-section image-storage-settings" aria-labelledby="image-storage-heading">
    <header><h2 id="image-storage-heading"><Images size={17} />聊天图片存储</h2>
      <button type="button" className="button button-secondary" disabled={loading || busy} aria-label="刷新图片用量"
        onClick={() => { setError(""); void refresh(); }}><RefreshCw size={14} />刷新</button></header>
    <p className="settings-help">点击对话查看图片副本。副本随对话保留，删除项目原图或归档对话不影响副本；永久删除聊天会一并清理。</p>
    {view && <p className="image-storage-total">{view.unknownSessionCount ? "已确认占用" : "已占用"} <strong>{bytes(view.storedBytes)}</strong>
      {view.unknownSessionCount > 0 && ` · ${view.unknownSessionCount} 条对话的用量暂不可读取`}</p>}
    {error && !confirmation && <p className="image-storage-error" role="alert">{error}</p>}
    {notice && <p className="image-storage-notice" role="status">{notice}</p>}
    {loading && !view ? <p className="settings-help" role="status"><LoaderCircle size={15} className="spin" /> 正在读取图片用量…</p>
      : view && !view.sessions.length ? <p className="settings-help">暂无占用空间的聊天图片副本。</p>
      : view && <ul className="image-storage-list" aria-label="各对话图片用量">{view.sessions.map(row => <ImageStorageSessionRow key={row.sessionId}
        row={row} disabled={busy || loading} formatBytes={bytes}
        onClear={selected => { setError(""); setNotice(""); setConfirmation(selected); }} />)}</ul>}
    <dialog ref={dialog} className="image-storage-confirm" aria-labelledby="image-storage-confirm-heading"
      aria-describedby="image-storage-confirm-description" onCancel={event => {
        event.preventDefault(); if (!busyRef.current) setConfirmation(null);
      }}>
      <h2 id="image-storage-confirm-heading">清理这条对话的图片副本？</h2>
      <p className="image-storage-confirm-title">{confirmation && messagePreview(confirmation.title)}</p>
      <p id="image-storage-confirm-description">将清理 {confirmation?.storedCount ?? 0} 张图片副本，预计释放 {bytes(confirmation?.storedBytes ?? 0)}。
        文字记录、文件名和保存时间会保留，图片会显示为“已清理”。此操作无法恢复副本，项目原图不受影响。</p>
      {error && <p className="image-storage-error" role="alert">{error}</p>}
      <footer><button ref={cancel} type="button" className="button button-secondary" disabled={busy} onClick={() => setConfirmation(null)}>取消</button>
        <button type="button" className="button image-storage-delete" disabled={busy || loading || !confirmation?.canClear} onClick={() => void clear()}>
          {busy ? <><LoaderCircle size={15} className="spin" />正在清理…</> : "确认清理图片"}</button></footer>
    </dialog>
  </section>;
}
