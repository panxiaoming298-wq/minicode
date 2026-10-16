import { useEffect, useId, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Download, Expand, ImageOff, X } from "lucide-react";
import type { VerificationView } from "../types";
import { projectArtifactUrl } from "../lib/projectChecks";
import { imageStatusLabel, type DisplayImageStatus } from "../lib/runImages";
import "./ProjectCheck.css";

export interface ImageShot {
  url: string; label: string; filename: string; detail?: string; downloadUrl?: string;
  availability?: DisplayImageStatus;
  reload?: number;
  onStateChange?: (state: "loading" | "ready" | "failed") => void;
}
type Shot = ImageShot;

function unavailable(shot: Shot) {
  return shot.availability === "cleared" || shot.availability === "missing" || shot.availability === "corrupt";
}

function ScreenshotImage({ shot, thumbnail = false }: { shot: Shot; thumbnail?: boolean }) {
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [retry, setRetry] = useState(0);
  useEffect(() => { setState("loading"); }, [shot.reload]);
  useEffect(() => {
    if (shot.availability === "available" && state === "failed") {
      setState("loading"); setRetry(value => value + 1);
    }
  }, [shot.availability]);
  if (unavailable(shot)) return <div className="project-screenshot-image">
    <span className="project-image-status"><ImageOff size={20} />{imageStatusLabel(shot.availability!)}
      {!thumbnail && <span>{shot.filename} · 原有图片记录仍保留。</span>}</span>
  </div>;
  return <div className={`project-screenshot-image ${state === "ready" ? "is-ready" : ""}`}>
    {state === "loading" && <span className="project-image-status" role="status">正在读取截图…</span>}
    {state === "failed" ? <span className="project-image-status"><ImageOff size={20} />图片加载失败
      {!thumbnail && <><span>请检查连接后重试。</span><button type="button" onClick={() => {
        setState("loading"); setRetry(value => value + 1); shot.onStateChange?.("loading");
      }}>重新加载</button></>}
    </span> : <img src={shot.url + (retry || shot.reload ? `${shot.url.includes("?") ? "&" : "?"}reload=${shot.reload || 0}&retry=${retry}` : "")} alt={shot.label}
      loading={thumbnail ? "lazy" : "eager"} onLoad={() => { setState("ready"); shot.onStateChange?.("ready"); }}
      onError={() => { setState("failed"); shot.onStateChange?.("failed"); }} />}
  </div>;
}

function ScreenshotViewer({ shots, initial, onClose }: { shots: Shot[]; initial: number; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();
  const [index, setIndex] = useState(initial);
  const [actualSize, setActualSize] = useState(false);
  const shot = shots[Math.min(index, shots.length - 1)];
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);
  const move = (offset: number) => { setIndex(value => (value + offset + shots.length) % shots.length); setActualSize(false); };
  return <dialog ref={dialog} className="project-image-viewer" aria-labelledby={heading}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onKeyDown={event => {
      if (event.key === "Tab") {
        const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], [tabindex="0"]')];
        const first = controls[0];
        const last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first && last) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last && first) { event.preventDefault(); first.focus(); }
      }
      if (shots.length > 1 && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
        event.preventDefault(); move(event.key === "ArrowLeft" ? -1 : 1);
      }
    }}>
    <header><div><h2 id={heading}>{shot.label}</h2><p>{shot.detail || shot.filename} · {index + 1} / {shots.length}</p></div>
      <button type="button" className="icon-button" aria-label="关闭截图预览" onClick={onClose} autoFocus><X size={20} /></button></header>
    <div className="project-image-toolbar">
      {shots.length > 1 && <div className="project-image-navigation">
        <button type="button" aria-label="上一张截图" onClick={() => move(-1)}><ChevronLeft size={18} /></button>
        <button type="button" aria-label="下一张截图" onClick={() => move(1)}><ChevronRight size={18} /></button>
      </div>}
      <button type="button" aria-pressed={actualSize} onClick={() => setActualSize(value => !value)}>{actualSize ? "适应窗口" : "原始尺寸"}</button>
      {unavailable(shot) ? <span className="project-image-download-disabled" aria-disabled="true"><Download size={15} />下载不可用</span>
        : <a href={shot.downloadUrl || shot.url} download={shot.filename}><Download size={15} />下载原图</a>}
    </div>
    <div className={`project-image-canvas ${actualSize ? "is-actual-size" : ""}`} tabIndex={0} aria-label="截图，可滚动查看">
      <ScreenshotImage key={shot.url} shot={shot} />
    </div>
  </dialog>;
}

export function ProjectScreenshots({ record, sessionId, runId }: { record: VerificationView; sessionId?: string | null; runId?: string | null }) {
  const shots = (record.projectCheck?.artifacts || []).flatMap(artifact => {
    const url = projectArtifactUrl(sessionId, runId, artifact.artifactId);
    if (!url || !/^(desktop|mobile)(?:-[A-Za-z0-9_-]{1,64})?\.png$/.test(artifact.name)) return [];
    return [{ url, label: (artifact.name.startsWith("desktop") ? "桌面截图" : "手机截图") +
      (artifact.name.endsWith("-failure.png") ? " · 失败现场" : ""),
      filename: `minicode-${artifact.artifactId.slice(-8)}-${artifact.name}` }];
  }).filter((shot, index, all) => all.findIndex(item => item.url === shot.url) === index);
  return <ImageGallery shots={shots} />;
}

export function ImageGallery({ shots, compact = false }: { shots: ImageShot[]; compact?: boolean }) {
  const [selected, setSelected] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  if (!shots.length) return null;
  // A new check remounts the viewer instead of retaining an old image/index.
  // A newly arriving screenshot must not close or reset the image being viewed.
  const galleryKey = shots[0].url;
  return <div className={`project-screenshots${compact ? " is-compact" : ""}`} aria-label="图片预览">
    {(expanded ? shots : shots.slice(0, 4)).map((shot, index) => <figure key={shot.url}>
      <button type="button" className="project-screenshot-open" aria-label={`放大${shot.label}`} aria-haspopup="dialog"
        disabled={unavailable(shot)} onClick={() => setSelected(index)}>
        <ScreenshotImage shot={shot} thumbnail />
        <span className="project-screenshot-expand"><Expand size={14} />放大</span>
      </button>
      <figcaption><span title={`${shot.filename}${shot.detail ? ` · ${shot.detail}` : ""}`}>{shot.label}
        {unavailable(shot) && shot.filename !== shot.label && <small className="project-image-filename">{shot.filename}</small>}
        {shot.detail && <small>{shot.detail}</small>}</span>
        {unavailable(shot) ? <span className="project-image-download-disabled" aria-label={`下载${shot.label}不可用`} aria-disabled="true"><Download size={14} />{!compact && "下载不可用"}</span>
          : <a href={shot.downloadUrl || shot.url} download={shot.filename} aria-label={`下载${shot.label}`}><Download size={14} />{!compact && "下载"}</a>}</figcaption>
    </figure>)}
    {shots.length > 4 && <button className="image-gallery-more" type="button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>
      {expanded ? "收起更多图片" : `查看其余 ${shots.length - 4} 张图片`}</button>}
    {selected !== null && <ScreenshotViewer key={galleryKey} shots={shots} initial={Math.min(selected, shots.length - 1)} onClose={() => setSelected(null)} />}
  </div>;
}
