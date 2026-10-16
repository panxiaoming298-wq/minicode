import { useEffect, useId, useRef, useState, type RefObject } from "react";

export interface ConversationAnchor {
  id: string;
  prompt: string;
  preview: string;
}

interface ConversationNavigatorProps {
  entries: ConversationAnchor[];
  scrollRef: RefObject<HTMLDivElement | null>;
  onNavigate: (id: string) => void;
}

export function ConversationNavigator({ entries, scrollRef, onNavigate }: ConversationNavigatorProps) {
  const [activeId, setActiveId] = useState("");
  const [preview, setPreview] = useState<{ id: string; top: number } | null>(null);
  const navRef = useRef<HTMLElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const previewId = useId();
  const entryIds = entries.map(entry => entry.id).join("\n");

  useEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll) return;
    const anchors = entries.map(entry => document.getElementById(entry.id)).filter((element): element is HTMLElement => Boolean(element));
    let frame = 0;
    function update() {
      frame = 0;
      const bounds = scroll!.getBoundingClientRect();
      const readingLine = bounds.top + Math.min(120, bounds.height * .25);
      let current = anchors[0]?.id || "";
      for (const anchor of anchors) {
        if (anchor.getBoundingClientRect().top > readingLine) break;
        current = anchor.id;
      }
      setActiveId(current);
    }
    function schedule() { if (!frame) frame = window.requestAnimationFrame(update); }
    const observer = new ResizeObserver(schedule);
    observer.observe(scroll);
    const content = scroll.querySelector(".stream-content");
    if (content) observer.observe(content);
    scroll.addEventListener("scroll", schedule, { passive: true });
    schedule();
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      scroll.removeEventListener("scroll", schedule);
    };
  }, [entryIds, scrollRef]);

  useEffect(() => {
    const list = listRef.current;
    const marker = list?.querySelector<HTMLButtonElement>('[aria-current="location"]');
    if (!list || !marker) return;
    if (marker.offsetTop < list.scrollTop) list.scrollTop = marker.offsetTop;
    else if (marker.offsetTop + marker.offsetHeight > list.scrollTop + list.clientHeight) {
      list.scrollTop = marker.offsetTop + marker.offsetHeight - list.clientHeight;
    }
  }, [activeId, entryIds]);

  function showPreview(id: string, button: HTMLButtonElement) {
    const bounds = navRef.current?.getBoundingClientRect();
    if (!bounds) return;
    const top = button.getBoundingClientRect().top - bounds.top;
    setPreview({ id, top: Math.max(0, Math.min(top - 36, bounds.height - 170)) });
  }

  const previewIndex = preview ? entries.findIndex(entry => entry.id === preview.id) : -1;
  const previewEntry = entries[previewIndex];

  return <nav className="conversation-navigator" aria-label="对话输入定位" ref={navRef}
    onMouseLeave={() => setPreview(null)} onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setPreview(null);
    }}>
    <div className="conversation-navigator-ticks" ref={listRef} onScroll={() => setPreview(null)}>
      {entries.map((entry, index) => <button key={entry.id} type="button"
        className="conversation-marker" aria-label={`定位第 ${index + 1} 次输入：${entry.prompt.slice(0, 70)}`}
        aria-current={activeId === entry.id ? "location" : undefined}
        aria-describedby={preview?.id === entry.id ? previewId : undefined}
        tabIndex={activeId === entry.id || (!activeId && index === 0) ? 0 : -1}
        onMouseEnter={event => showPreview(entry.id, event.currentTarget)}
        onFocus={event => showPreview(entry.id, event.currentTarget)}
        onClick={() => { onNavigate(entry.id); setActiveId(entry.id); setPreview(null); }}
        onKeyDown={event => {
          if (event.key === "Escape") { setPreview(null); return; }
          if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "Home" ? 0 : event.key === "End" ? entries.length - 1
            : Math.max(0, Math.min(entries.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
          listRef.current?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
        }}><span aria-hidden="true" /></button>)}
    </div>
    {previewEntry && <div className="conversation-marker-preview" id={previewId} role="tooltip" style={{ top: preview!.top }}>
      <span>第 {previewIndex + 1} 次输入{activeId === previewEntry.id ? " · 当前阅读" : ""}</span>
      <strong>{previewEntry.prompt}</strong>
      {previewEntry.preview && <p>{previewEntry.preview}</p>}
    </div>}
  </nav>;
}
