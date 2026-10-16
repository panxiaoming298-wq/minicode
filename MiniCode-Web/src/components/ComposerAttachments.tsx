import { ArrowLeft, FileText, Folder, LoaderCircle, Paperclip, Plus, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { WorkspaceDirectory } from "../types";
import { IMAGE_FILE_ACCEPT, MAX_FILE_BYTES, TEXT_FILE_ACCEPT, projectAttachment, readLocalAttachment, type Attachment, type ImageAttachment } from "../lib/attachments";
import "./ComposerAdditions.css";

export function AttachmentList({ files, onRemove, disabled = false }: {
  files: readonly Attachment[]; onRemove?: (id: string) => void; disabled?: boolean;
}) {
  const [preview, setPreview] = useState<ImageAttachment | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (preview) dialog.current?.showModal(); }, [preview]);
  if (!files.length) return null;
  return <div className="composer-attachment-list" aria-label={onRemove ? "待发送附件" : "消息附件"}>
    {files.map(file => <div className="composer-attachment" key={file.id}>
      {file.kind === "image" ? <button className="image-attachment-thumbnail" title="放大查看图片" onClick={() => setPreview(file)}>
        <img src={api.imageUrl(file.id)} alt={file.name} loading="lazy" />
        <span><strong>{file.name}</strong><small>{file.width} × {file.height} · {Math.max(1, Math.ceil(file.size / 1024))} KB</small></span>
      </button> : <details>
        <summary title={file.path || file.name}><FileText size={16} /><span><strong>{file.name}</strong>
          <small>{file.source === "project" ? "项目文件" : "本地文件"} · {Math.max(1, Math.ceil(file.size / 1024))} KB</small></span></summary>
        <div className="attachment-preview"><span>{file.path || file.name} · 添加时的内容</span><pre>{file.content || "（空文件）"}</pre></div>
      </details>}
      {onRemove && <button disabled={disabled} aria-label={`移除附件 ${file.name}`} title="移除附件" onClick={() => onRemove(file.id)}><X size={14} /></button>}
    </div>)}
    <dialog ref={dialog} className="image-attachment-dialog" aria-label="图片预览" onClose={() => setPreview(null)}
      onClick={event => { if (event.target === event.currentTarget) dialog.current?.close(); }}>
      {preview && <><div><strong>{preview.name}</strong><button autoFocus aria-label="关闭图片预览" onClick={() => dialog.current?.close()}><X size={20} /></button></div>
        <img src={api.imageUrl(preview.id)} alt={preview.name} /></>}
    </dialog>
  </div>;
}

function ProjectFiles({ workspace, disabled, onAdd, onClose }: {
  workspace: string; disabled: boolean; onAdd: (files: Attachment[]) => void; onClose: () => void;
}) {
  const [directory, setDirectory] = useState("");
  const [listing, setListing] = useState<WorkspaceDirectory | null>(null);
  const [path, setPath] = useState("");
  const [loading, setLoading] = useState(true);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    let current = true;
    setLoading(true); setError(""); setListing(null);
    api.workspaceFiles(workspace, directory).then(result => { if (current) setListing(result); })
      .catch(reason => { if (current) setError(reason instanceof Error ? reason.message : "无法读取目录。"); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [directory, workspace]);
  async function add(file: string) {
    if (reading || disabled) return;
    setReading(true); setError("");
    try {
      const result = await api.workspaceFile(workspace, file);
      if (!mounted.current) return;
      onAdd([projectAttachment(result, workspace)]); onClose();
    } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "无法添加文件。"); }
    finally { if (mounted.current) setReading(false); }
  }
  return <>
    <div className="composer-popover-heading"><strong>引用项目文件</strong><button aria-label="关闭文件选择" onClick={onClose}><X size={16} /></button></div>
    <form className="project-file-path" onSubmit={event => { event.preventDefault(); if (path.trim()) void add(path.trim()); }}>
      <input autoFocus aria-label="项目文件路径" placeholder="输入相对路径，例如 src/App.tsx" value={path} onChange={event => setPath(event.target.value)} />
      <button disabled={!path.trim() || reading || disabled}>添加</button>
    </form>
    <div className="project-file-directory"><button disabled={!directory || reading} aria-label="返回上级目录"
      onClick={() => setDirectory(directory.split("/").slice(0, -1).join("/"))}><ArrowLeft size={16} /></button><span title={directory}>{directory || "项目根目录"}</span></div>
    <div className="composer-project-list project-file-list" aria-busy={loading || reading}>
      {loading ? <p className="composer-popover-empty" role="status">正在读取文件…</p> : listing?.entries.map(entry => <button key={entry.path}
        disabled={reading || disabled || (entry.kind === "file" && (entry.size || 0) > MAX_FILE_BYTES)} title={entry.path}
        onClick={() => entry.kind === "directory" ? setDirectory(entry.path) : void add(entry.path)}>
        {entry.kind === "directory" ? <Folder size={17} /> : <FileText size={17} />}<span>{entry.name}</span>
        {entry.kind === "file" && (entry.size || 0) > MAX_FILE_BYTES && <small>超过 64 KB</small>}
      </button>)}
      {!loading && listing?.entries.length === 0 && <p className="composer-popover-empty">这个目录没有可选文件</p>}
    </div>
    {listing?.truncated && <p className="composer-popover-empty">目录项目较多，请输入文件的相对路径添加。</p>}
    {reading && <p className="composer-popover-empty" role="status">正在添加文件…</p>}
    <p className="composer-popover-note">保存选取时的文本内容；每个文件最多 64 KB。</p>
    {error && <p className="composer-add-error" role="alert">{error}</p>}
  </>;
}

export function ComposerAttachments({ workspace, standalone, disabled, onAdd, onBusyChange }: {
  workspace: string; standalone: boolean; disabled: boolean; onAdd: (files: Attachment[]) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [menu, setMenu] = useState<"add" | "project" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const reading = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function addLocal(files: File[]) {
    if (!files.length || disabled || reading.current) return;
    reading.current = true; setBusy(true); setError("");
    // Capture this draft's callback before upload so navigation cannot move the attachments.
    const addToDraft = onAdd;
    try { addToDraft(await Promise.all(files.map(file => readLocalAttachment(file, api.uploadImage)))); }
    catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "无法添加文件，请重试。"); }
    finally { reading.current = false; if (mounted.current) setBusy(false); }
  }
  useEffect(() => {
    const composer = root.current?.closest(".composer");
    if (!composer) return;
    const paste = (event: Event) => {
      const clipboard = event as ClipboardEvent;
      const files = Array.from(clipboard.clipboardData?.files || []).filter(file => file.type.startsWith("image/"));
      if (files.length) { event.preventDefault(); void addLocal(files); }
    };
    const drag = (event: Event) => {
      if ((event as DragEvent).dataTransfer?.types.includes("Files")) event.preventDefault();
    };
    const drop = (event: Event) => {
      const files = Array.from((event as DragEvent).dataTransfer?.files || []);
      if (files.length) { event.preventDefault(); void addLocal(files); }
    };
    composer.addEventListener("paste", paste);
    composer.addEventListener("dragover", drag);
    composer.addEventListener("drop", drop);
    return () => {
      composer.removeEventListener("paste", paste);
      composer.removeEventListener("dragover", drag);
      composer.removeEventListener("drop", drop);
    };
  }, [disabled, onAdd]);
  useEffect(() => {
    if (!menu) return;
    const dismiss = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setMenu(null); };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [menu]);
  useEffect(() => { onBusyChange(busy || menu === "project"); }, [busy, menu, onBusyChange]);
  useEffect(() => () => onBusyChange(false), [onBusyChange]);
  return <div className="composer-add" ref={root} onKeyDown={event => {
    if (event.key === "Escape") { event.stopPropagation(); setMenu(null); setError(""); trigger.current?.focus(); }
  }}>
    <button ref={trigger} className="composer-add-button" aria-label="添加文件" aria-expanded={Boolean(menu)} aria-haspopup="dialog"
      title="添加文件" disabled={disabled || busy} onClick={() => { setError(""); setMenu(value => value ? null : "add"); }}>
      {busy ? <LoaderCircle size={20} className="spin" /> : <Plus size={21} />}
    </button>
    <input ref={input} className="attachment-file-input" type="file" multiple accept={`${TEXT_FILE_ACCEPT},${IMAGE_FILE_ACCEPT}`} aria-label="选择本地文件或图片"
      onChange={event => {
        const files = Array.from(event.target.files || []); event.target.value = "";
        void addLocal(files);
      }} />
    {busy && <span className="attachment-upload-status" role="status">正在添加附件…</span>}
    {menu && <div className="composer-popover attachment-popover" role="dialog" aria-label={menu === "project" ? "选择项目文件" : "添加参考文件"}>
      {menu === "project" ? <ProjectFiles workspace={workspace} disabled={disabled} onAdd={onAdd} onClose={() => { setMenu(null); trigger.current?.focus(); }} /> : <>
        <div className="composer-popover-heading"><strong>添加参考文件</strong></div>
        <button onClick={() => { setMenu(null); input.current?.click(); }}><Paperclip size={17} /><span>添加文件或图片…<small>图片、文本、代码、日志</small></span></button>
        {!standalone && <button onClick={() => setMenu("project")}><Folder size={17} /><span>引用项目文件…<small>从当前项目中选择</small></span></button>}
        <p className="composer-popover-note">最多 8 个附件；图片每张 8 MB，文本合计 128 KB。支持粘贴或拖入图片。</p>
      </>}
    </div>}
    {error && <div className="composer-popover attachment-popover" role="alert"><p className="composer-add-error">{error}</p>
      <button onClick={() => setError("")}>知道了</button></div>}
  </div>;
}
