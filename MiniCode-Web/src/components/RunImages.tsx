import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Images, ChevronDown, RefreshCw } from "lucide-react";
import type { RunImage } from "../types";
import { imageGroupSummary, imageUrl, validRunImages, type DisplayImageStatus } from "../lib/runImages";
import { api } from "../api/client";
import { ImageGallery } from "./ProjectScreenshots";

const collapsedKey = "minicode.collapsed-image-groups.v1";
function collapsedGroups(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(collapsedKey) || "[]");
    return Array.isArray(value) ? value.filter((key): key is string => typeof key === "string").slice(-128) : [];
  } catch { return []; }
}

export function RunImages({ images, active = false }: { images: RunImage[]; active?: boolean }) {
  const records = useMemo(() => validRunImages(images), [images]);
  const key = records.length ? `${records[0].sessionId}:${records[0].id}` : "";
  const [open, setOpen] = useState(() => !collapsedGroups().includes(key));
  const [hasOpened, setHasOpened] = useState(open);
  const [statuses, setStatuses] = useState<Record<string, DisplayImageStatus>>({});
  const [revision, setRevision] = useState(0);
  const mounted = useRef(true);
  const signature = records.map(image => imageUrl(image)).join("|");
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const cleared = (event: Event) => {
      const sessionId = (event as CustomEvent<{ sessionId: string }>).detail?.sessionId;
      if (!records.some(image => image.sessionId === sessionId)) return;
      setStatuses(current => ({ ...current, ...Object.fromEntries(records.filter(image => image.sessionId === sessionId).map(image => [imageUrl(image), "cleared" as const])) }));
      setRevision(value => value + 1);
    };
    window.addEventListener("minicode:images-cleared", cleared);
    return () => window.removeEventListener("minicode:images-cleared", cleared);
  }, [signature]);
  useEffect(() => {
    // Image loads establish availability without another full-file status scan.
    // Reserve scans for refresh/storage changes, not ordinary rendering or folds.
    if (!revision || !records.length) return;
    const controller = new AbortController();
    const groups = new Map<string, RunImage[]>();
    for (const record of records) groups.set(record.sessionId, [...(groups.get(record.sessionId) || []), record]);
    for (const [sessionId, group] of groups) for (let offset = 0; offset < group.length; offset += 24) {
      const batch = group.slice(offset, offset + 24);
      void api.imageStatus(sessionId, batch.map(image => image.id), controller.signal).then(result => {
        if (controller.signal.aborted) return;
        setStatuses(current => {
          const next = { ...current };
          for (const status of result.images || []) {
            const image = batch.find(item => item.id === status.id);
            if (image && ["available", "cleared", "missing", "corrupt"].includes(status.status)) next[imageUrl(image)] = status.status;
          }
          return next;
        });
      }).catch(() => { /* A failed status request does not prove a picture was deleted. */ });
    }
    return () => controller.abort();
  }, [signature, revision]);
  function imageState(image: RunImage, state: "loading" | "ready" | "failed") {
    const url = imageUrl(image);
    setStatuses(current => {
      if (["cleared", "missing", "corrupt"].includes(current[url])) return current;
      const next = { ...current };
      if (state === "loading") delete next[url];
      else next[url] = state === "ready" ? "available" : "failed";
      return next;
    });
    if (state === "failed") void api.imageStatus(image.sessionId, [image.id]).then(result => {
      const status = result.images?.find(item => item.id === image.id)?.status;
      if (mounted.current && status && ["cleared", "missing", "corrupt"].includes(status)) {
        setStatuses(current => ({ ...current, [url]: status }));
      }
    }).catch(() => { /* Retain the recoverable loading failure. */ });
  }
  const contentId = useId();
  if (!records.length) return null;
  return <section className="tool-images" aria-label="执行截图与图片" data-image-tool-call-id={records[0].toolCallId}>
    <header className="tool-images-header"><button className="tool-images-toggle" type="button" aria-expanded={open} aria-controls={contentId}
      title="图片副本随对话保存，点击展开或收起" onClick={() => {
        const next = !open; setOpen(next);
        if (next) setHasOpened(true);
        const collapsed = collapsedGroups().filter(item => item !== key);
        if (!next) collapsed.push(key);
        try { localStorage.setItem(collapsedKey, JSON.stringify(collapsed.slice(-128))); } catch { /* Viewing works without browser storage. */ }
      }}><Images size={16} /><span>{imageGroupSummary(records, statuses)}</span><ChevronDown size={14} className={open ? "" : "is-collapsed"} /></button>
      {open && <button className="icon-button tool-images-refresh"
        type="button" aria-label="重新检查图片" title="重新检查图片状态" onClick={() => setRevision(value => value + 1)}><RefreshCw size={14} /></button>}</header>
    <p className="tool-images-context">{records[0].sourceCreatedAt ? "引用此前图片 · 原图保存于 " : "保存于 "}
      {new Date(records[0].sourceCreatedAt || records[0].createdAt).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}
      {records[0].toolName === "view_image" && " · 供模型检查，结论见回复"}</p>
    <span className="sr-only" role="status" aria-live="polite">{active ? `已生成 ${records.length} 张图片，可以查看。` : ""}</span>
    {/* Keep loaded previews mounted across folds; never-opened groups stay lazy. */}
    <div id={contentId} hidden={!open}>{hasOpened && <ImageGallery compact shots={records.map(image => ({ url: imageUrl(image), downloadUrl: imageUrl(image, true),
      label: image.caption || image.name, filename: image.name,
      availability: statuses[imageUrl(image)] || image.availability, reload: revision, onStateChange: state => imageState(image, state),
      detail: `${image.width} × ${image.height} · ${image.sourceCreatedAt ? "引用此前图片 · " : ""}${new Date(image.sourceCreatedAt || image.createdAt).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })}${image.capture ? ` · ${image.capture.scenario}${image.capture.ready ? "" : " · 页面未就绪"}` : ""}${image.toolName === "view_image" ? " · 供模型视觉检查" : ""}` }))} />}</div>
  </section>;
}
