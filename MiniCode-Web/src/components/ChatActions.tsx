import { Check, Copy, FileDown, Image, LoaderCircle, MoreHorizontal, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { formatConversationMarkdown, readConversationExport } from "../lib/conversationMarkdown";
import "./ChatActions.css";

export function ChatActions({ sessionId, active, loading }: { sessionId: string; active: boolean; loading: boolean }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<"copy" | "pdf" | "appearance" | null>(null);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [manual, setManual] = useState("");
  const root = useRef<HTMLSpanElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const item = useRef<HTMLButtonElement>(null);
  const pdfItem = useRef<HTMLButtonElement>(null);
  const appearanceItem = useRef<HTMLButtonElement>(null);
  const focusLast = useRef(false);
  const pending = useRef<AbortController | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const text = useRef<HTMLTextAreaElement>(null);
  const id = useId();

  useEffect(() => () => { pending.current?.abort(); }, [sessionId]);
  useEffect(() => {
    if (!status) return;
    const timer = window.setTimeout(() => setStatus(""), 3000);
    return () => window.clearTimeout(timer);
  }, [status]);
  useEffect(() => {
    if (!open) return;
    (focusLast.current ? appearanceItem : item).current?.focus();
    function dismiss(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);
  useEffect(() => {
    if (!manual) return;
    dialog.current?.showModal();
    text.current?.focus();
    text.current?.select();
  }, [manual]);

  function closeMenu() {
    setOpen(false);
    trigger.current?.focus();
  }

  async function exportChat(format: "copy" | "pdf" | "appearance") {
    if (pending.current || active || loading) return;
    let preview: Window | null = null;
    if (format !== "copy") {
      // Open during the click, before fetching history, so browsers accept it.
      preview = window.open("about:blank", "_blank");
      if (!preview) { setError("浏览器拦截了预览窗口，请允许弹出窗口后重试。"); return; }
      preview.opener = null;
      preview.document.title = "正在准备 PDF - MiniCode";
      preview.document.body.textContent = "正在整理完整聊天，请稍候…";
      preview.document.body.style.cssText = "padding:32px;font:16px/1.7 system-ui,sans-serif";
    }
    const controller = new AbortController();
    const closePreview = () => { preview?.close(); };
    controller.signal.addEventListener("abort", closePreview, { once: true });
    pending.current = controller;
    setBusy(format);
    setStatus("");
    setError("");
    try {
      const [conversation, pdf] = await Promise.all([
        readConversationExport(sessionId, controller.signal),
        format !== "copy" ? import("../lib/conversationPdf") : undefined,
      ]);
      if (controller.signal.aborted) return;
      const content = format === "appearance" ? conversation : formatConversationMarkdown(conversation.title, conversation.turns);
      if (preview && pdf) {
        if (preview.closed) throw new Error("PDF 预览窗口已关闭，请重新导出。");
        pdf.showConversationPdf(preview, content);
        setStatus("PDF 预览已打开");
      } else {
        const markdown = content as string;
        try {
          await navigator.clipboard.writeText(markdown);
          if (!controller.signal.aborted) setStatus("已复制 Markdown");
        } catch {
          if (!controller.signal.aborted) setManual(markdown);
        }
      }
      if (!controller.signal.aborted) closeMenu();
    } catch (cause) {
      closePreview();
      if (!controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : "导出失败，请重试。");
        setOpen(true);
      }
    } finally {
      controller.signal.removeEventListener("abort", closePreview);
      if (pending.current === controller) {
        pending.current = null;
        if (!controller.signal.aborted) setBusy(null);
      }
    }
  }

  const unavailable = active ? "任务结束后可复制或导出完整聊天" : loading ? "正在读取聊天" : "";
  return <span ref={root} className="chat-actions" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }} onKeyDown={event => {
    if (event.key === "Escape" && open) { event.preventDefault(); event.stopPropagation(); closeMenu(); }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) && !manual) {
      event.preventDefault();
      const buttons = [item.current, pdfItem.current, appearanceItem.current];
      if (open) {
        const current = buttons.findIndex(button => button === document.activeElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
          : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
        buttons[next]?.focus();
      } else {
        focusLast.current = event.key === "ArrowUp" || event.key === "End";
        setOpen(true);
      }
    }
  }}>
    <button type="button" ref={trigger} className={`icon-button chat-actions-trigger ${open ? "is-active" : ""}`}
      aria-label="聊天操作" title="聊天操作" aria-haspopup="menu" aria-expanded={open}
      aria-controls={open ? id : undefined} onClick={() => { focusLast.current = false; setOpen(value => !value); }}>
      {busy ? <LoaderCircle size={18} className="spin" aria-hidden="true" />
        : status ? <Check size={18} aria-hidden="true" /> : <MoreHorizontal size={20} aria-hidden="true" />}
    </button>
    {open && <div id={id} className="chat-actions-menu" role="menu" aria-label="聊天操作菜单">
      <button type="button" ref={item} role="menuitem" tabIndex={-1} aria-disabled={Boolean(busy || unavailable)}
        title={unavailable || undefined} onClick={() => void exportChat("copy")}>
        {busy === "copy" ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
        <span>{busy === "copy" ? "正在整理聊天…" : "复制为 Markdown"}</span>
      </button>
      <button type="button" ref={pdfItem} role="menuitem" tabIndex={-1} aria-disabled={Boolean(busy || unavailable)}
        title={unavailable || "在浏览器打印窗口中另存为 PDF"} onClick={() => void exportChat("pdf")}>
        {busy === "pdf" ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <FileDown size={16} aria-hidden="true" />}
        <span>{busy === "pdf" ? "正在准备 PDF…" : "导出 PDF（简洁版）"}</span>
      </button>
      <button type="button" ref={appearanceItem} role="menuitem" tabIndex={-1} aria-disabled={Boolean(busy || unavailable)}
        title={unavailable || "保留聊天气泡、图片、表格和代码样式"} onClick={() => void exportChat("appearance")}>
        {busy === "appearance" ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Image size={16} aria-hidden="true" />}
        <span>{busy === "appearance" ? "正在准备原样 PDF…" : "导出 PDF（原样版）"}</span>
      </button>
      {unavailable && <p className="chat-actions-hint">{unavailable}</p>}
      {error && <p className="chat-actions-error" role="alert">{error}</p>}
    </div>}
    {status && !open && <span className="chat-actions-status" role="status"><Check size={14} aria-hidden="true" />{status}</span>}
    {manual && <dialog ref={dialog} className="chat-copy-dialog" aria-labelledby={`${id}-title`} aria-describedby={`${id}-help`}
      onClose={() => { setManual(""); trigger.current?.focus(); }}>
      <header><strong id={`${id}-title`}>手动复制 Markdown</strong>
        <button type="button" className="icon-button" aria-label="关闭手动复制" onClick={() => dialog.current?.close()}><X size={18} /></button></header>
      <p id={`${id}-help`}>浏览器未允许自动复制。可复制下方已选中的文本（Ctrl+C / ⌘C），或在手机上长按复制。</p>
      <textarea ref={text} aria-label="聊天 Markdown" readOnly value={manual} spellCheck={false} />
    </dialog>}
  </span>;
}
