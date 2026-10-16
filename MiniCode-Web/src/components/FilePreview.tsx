import { BookOpen, Check, CircleAlert, Code2, Copy, FileCode2, FileText, LoaderCircle, WrapText } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api/client";
import { isMarkdownFile, previewFileReference } from "../lib/filePreview";
import type { FileNavigationRequest, FileReference } from "../lib/fileReferences";
import type { WorkspaceFileContent } from "../types";
import { ImageGallery } from "./ProjectScreenshots";
import "./FilePreview.css";

interface FilePreviewProps {
  file: WorkspaceFileContent | null;
  workspace: string;
  loading: boolean;
  error: string;
  location: FileNavigationRequest | null;
  allowPreview?: boolean;
  onOpenFile?: (reference: FileReference) => void;
}

function formatFileSize(size: number): string {
  if (size < 1_024) return `${size} B`;
  if (size < 1_048_576) return `${(size / 1_024).toFixed(1)} KB`;
  return `${(size / 1_048_576).toFixed(1)} MB`;
}

export function FilePreview({ file, workspace, loading, error, location, allowPreview = true, onOpenFile }: FilePreviewProps) {
  const [view, setView] = useState<"preview" | "source">(location?.line ? "source" : "preview");
  const [wrap, setWrap] = useState(true);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const copyTimeout = useRef<number | undefined>(undefined);
  const targetLine = useRef<HTMLSpanElement | null>(null);
  const markdown = Boolean(file && isMarkdownFile(file.path));
  const preview = allowPreview && markdown && view === "preview";
  const lines = useMemo(() => !preview && file ? file.content.split("\n") : [], [file, preview]);

  useEffect(() => {
    if (location?.line) setView("source");
  }, [location]);
  useEffect(() => {
    if (!loading && !error && !preview && targetLine.current) {
      targetLine.current.scrollIntoView({ block: "center", behavior: "instant" });
      targetLine.current.focus({ preventScroll: true });
    }
  }, [file, loading, error, location, preview]);
  useEffect(() => () => window.clearTimeout(copyTimeout.current), []);

  async function copySource() {
    if (!file) return;
    try {
      await navigator.clipboard.writeText(file.content);
      setCopyError("");
      setCopied(true);
      window.clearTimeout(copyTimeout.current);
      copyTimeout.current = window.setTimeout(() => setCopied(false), 1_500);
    } catch {
      setCopyError("复制失败，请选中文件内容后手动复制。");
    }
  }

  if (loading) return <div className="inspector-empty" role="status"><LoaderCircle size={19} className="spin" /><p>正在读取文件…</p></div>;
  if (error) return <div className="inspector-empty is-error" role="alert"><CircleAlert size={19} /><p>{error}</p></div>;
  if (!file) return <div className="inspector-empty"><FileText size={22} /><p>选择文件开始阅读。Markdown 支持预览，也可以切换到源码。</p></div>;
  if (file.image) return <div className="run-images">
    <header><strong>{file.name}</strong><span>{file.image.width} × {file.image.height}</span></header>
    <ImageGallery key={file.image.url} shots={[{ url: file.image.url, downloadUrl: `${file.image.url}&download=true`, label: file.name, filename: file.name }]} />
  </div>;

  return <div className="source-document file-preview">
    <header className="file-preview-header">
      {markdown ? <FileText size={18} aria-hidden="true" /> : <FileCode2 size={18} aria-hidden="true" />}
      <div className="file-preview-title">
        <strong title={file.path}>{file.name}</strong>
        {file.path !== file.name && <span className="file-preview-path" title={file.path}>{file.path}</span>}
        <span>{markdown ? "Markdown · " : ""}{file.lineCount} 行 · {formatFileSize(file.size)} · {file.encoding}</span>
        {!preview && location?.line && location.line <= file.lineCount && <span className="file-preview-location">
          第 {location.line} 行{location.column ? `，第 ${location.column} 列` : ""}
        </span>}
      </div>
      <button type="button" className="icon-button" onClick={() => void copySource()}
        aria-label={`复制 ${file.name} 原文`} title={copied ? "已复制" : "复制原文"}>
        {copied ? <Check size={16} /> : <Copy size={16} />}
      </button>
    </header>
    <div className="file-preview-controls">
      {allowPreview && markdown ? <div className="file-preview-tabs" role="tablist" aria-label="文件预览方式"
        onKeyDown={event => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "ArrowLeft" || event.key === "Home" ? "preview" : "source";
          setView(next);
          event.currentTarget.querySelector<HTMLButtonElement>(`[data-view="${next}"]`)?.focus();
        }}>
        <button type="button" role="tab" data-view="preview" aria-selected={preview} tabIndex={preview ? 0 : -1} onClick={() => setView("preview")}>
          <BookOpen size={14} aria-hidden="true" />预览
        </button>
        <button type="button" role="tab" data-view="source" aria-selected={!preview} tabIndex={preview ? -1 : 0} onClick={() => setView("source")}>
          <Code2 size={14} aria-hidden="true" />源码
        </button>
      </div> : <span className="file-preview-source-label"><Code2 size={14} aria-hidden="true" />源码</span>}
      {!preview && <button type="button" className="file-preview-wrap" aria-pressed={wrap} onClick={() => setWrap(value => !value)} title="切换源码自动换行">
        <WrapText size={15} aria-hidden="true" />自动换行
      </button>}
    </div>
    {file.truncated && <div className="source-warning">文件较大，目前只显示前 {formatFileSize(1_500_000)}。</div>}
    {copyError && <div className="source-warning" role="alert">{copyError}</div>}
    {location?.line && location.line > file.lineCount && <div className="source-warning" role="status">
      {file.truncated ? `当前预览未包含引用的第 ${location.line} 行。` : `该文件只有 ${file.lineCount} 行，引用的第 ${location.line} 行不存在。`}
    </div>}
    {preview ? <article className="file-markdown" aria-label={`${file.name} 预览`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}
        urlTransform={(url, key) => {
          const reference = previewFileReference(url, file.path, workspace);
          if (key === "src" && reference) return api.workspaceImageUrl(workspace, reference.path);
          return key === "href" && reference ? url : defaultUrlTransform(url);
        }}
        components={{
          a: ({ href, children, node: _node, ...props }) => {
            const reference = href && previewFileReference(href, file.path, workspace);
            if (reference) return <button type="button" className="file-markdown-link" title={href}
              disabled={!onOpenFile} onClick={() => onOpenFile?.(reference)}>{children}</button>;
            return <a {...props} href={href} target={href?.startsWith("#") ? undefined : "_blank"} rel="noreferrer">{children}</a>;
          },
        }}>{file.content}</ReactMarkdown>
    </article> : <pre className={`source-view ${wrap ? "is-wrapped" : ""}`} aria-label={`${file.name} 原文`}>
      {lines.map((line, index) => {
        const target = location?.line === index + 1 && index < file.lineCount;
        return <span className={`source-line ${target ? "is-target" : ""}`} data-line={index + 1}
          aria-current={target ? "location" : undefined} ref={target ? targetLine : undefined} tabIndex={target ? -1 : undefined}
          key={index}>
          <i>{index + 1}</i><code>{line || " "}</code>
        </span>;
      })}
    </pre>}
  </div>;
}
