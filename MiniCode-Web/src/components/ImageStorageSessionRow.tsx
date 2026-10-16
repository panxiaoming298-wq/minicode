import { useEffect, useId, useState } from "react";
import { ChevronDown, Images, LoaderCircle, RefreshCw, Trash2 } from "lucide-react";

import { api, ApiError } from "../api/client";
import { messagePreview } from "../lib/attachments";
import { validRunImages } from "../lib/runImages";
import type { ImageStorageSession, RunImage } from "../types";
import { ImageGallery } from "./ProjectScreenshots";

function StoredSessionImages({ row }: { row: ImageStorageSession }) {
  const [images, setImages] = useState<RunImage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    void api.sessionImages(row.sessionId, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      const records = validRunImages(result.images);
      if (result.sessionId !== row.sessionId || !Array.isArray(result.images) || records.length !== result.images.length
        || records.some(image => image.sessionId !== row.sessionId)) throw new Error("图片列表数据无法读取，请刷新后重试。");
      setImages(records);
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof ApiError && reason.status === 404 && reason.message === "Not Found"
        ? "图片浏览接口尚未加载，请重启 MiniCode 后端后重新加载图片。"
        : reason instanceof Error ? reason.message : "图片列表加载失败，请重试。");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [row.sessionId, row.revision, retry]);

  if (loading) return <p className="image-storage-preview-status" role="status"><LoaderCircle size={15} className="spin" />正在加载图片…</p>;
  if (error) return <div className="image-storage-preview-status"><p className="image-storage-error" role="alert">{error}</p>
    <button type="button" className="button button-secondary" onClick={() => setRetry(value => value + 1)}><RefreshCw size={14} />重新加载图片</button></div>;
  if (!images.length) return <p className="image-storage-preview-status">暂无可查看的图片，可能已被清理。可刷新图片用量后查看。</p>;
  return <ImageGallery compact shots={images.map(image => ({
    url: api.storedImageUrl(row.sessionId, image.id), downloadUrl: api.storedImageUrl(row.sessionId, image.id, true),
    label: image.caption || image.name, filename: image.name, availability: image.availability,
    detail: `${image.width} × ${image.height}`,
  }))} />;
}

export function ImageStorageSessionRow({ row, disabled, formatBytes, onClear }: {
  row: ImageStorageSession;
  disabled: boolean;
  formatBytes: (value: number | null) => string;
  onClear: (row: ImageStorageSession) => void;
}) {
  const [open, setOpen] = useState(false);
  const [hasOpened, setHasOpened] = useState(false);
  const contentId = useId();
  const title = messagePreview(row.title) || "未命名对话";

  return <li className={open ? "is-expanded" : ""}>
    <div className="image-storage-row-heading">
      <button type="button" className="image-storage-session" aria-label={`查看图片 ${title}`}
        aria-expanded={open} aria-controls={contentId} disabled={disabled}
        onClick={() => { setOpen(value => !value); setHasOpened(true); }}>
        <span className="image-storage-session-copy"><strong title={title}>{title}</strong>
          <span>{row.workspaceName} · {new Date(row.updatedAt).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>
          <span>{row.storedCount === null ? "图片数量未知" : `${row.storedCount} 张图片`} · {formatBytes(row.storedBytes)}
            {row.archived && " · 已归档"}{row.running && " · 任务运行中"}</span>
          {row.error && <span className="image-storage-error">{row.error}</span>}
        </span>
        <span className="image-storage-row-action" aria-hidden="true"><Images size={14} /><span>{open ? "收起图片" : "查看图片"}</span><ChevronDown size={15} /></span>
      </button>
      <button type="button" className="button button-secondary image-storage-clear" disabled={disabled || !row.canClear}
        aria-label={`清理图片 ${title}`} title={row.running ? "任务运行中，请先停止再清理图片" : row.error || "清理前需要再次确认"}
        onClick={() => onClear(row)}><Trash2 size={14} />清理图片</button>
    </div>
    <div id={contentId} className="image-storage-preview" hidden={!open} role="region" aria-label={`对话图片 ${title}`}>
      {hasOpened && <StoredSessionImages row={row} />}
    </div>
  </li>;
}
