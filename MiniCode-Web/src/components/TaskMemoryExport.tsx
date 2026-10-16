import { useEffect, useRef, useState } from "react";
import { Download, LoaderCircle } from "lucide-react";
import { api } from "../api/client";

export function TaskMemoryExport({ sessionId }: { sessionId: string }) {
  const pending = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => () => pending.current?.abort(), [sessionId]);

  const download = async () => {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const blob = await api.exportTaskMemory(sessionId, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `minicode-task-memory-${sessionId.replace(/[^a-zA-Z0-9_-]/g, "_")}.md`;
      document.body.appendChild(link);
      try { link.click(); }
      finally {
        link.remove();
        // Leave time for the browser to consume the object URL before releasing it.
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      setSaved(true);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "导出失败，请重试。");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      if (pending.current === controller) pending.current = null;
    }
  };

  return <section className="knowledge-export" aria-label="任务记忆导出">
    <div className="knowledge-export-heading"><span>当前会话任务记忆</span>
      <button type="button" className="button button-secondary" disabled={busy} onClick={() => void download()}>
        {busy ? <LoaderCircle size={13} className="spin" aria-hidden="true" /> : <Download size={13} aria-hidden="true" />}
        {busy ? "导出中…" : "导出 Markdown"}
      </button>
    </div>
    <p className="section-note">导出任务状态、决策和分页记忆预览，文件仅供查看。</p>
    {error && <p className="knowledge-error" role="alert">{error}</p>}
    {saved && <p className="section-note" role="status">已发起 Markdown 下载，请在浏览器下载列表查看。</p>}
  </section>;
}
