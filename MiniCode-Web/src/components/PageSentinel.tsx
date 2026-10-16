import { useEffect, useRef } from "react";

/** Observe the actual page boundary after scroll restoration and layout have settled. */
export function PageSentinel({ cursor, busy, error, onLoad, edge = "bottom" }: {
  cursor: number | null | undefined;
  busy: boolean;
  error?: string;
  onLoad: () => void;
  edge?: "top" | "bottom";
}) {
  const element = useRef<HTMLDivElement>(null);
  const callback = useRef(onLoad);
  callback.current = onLoad;
  const requested = useRef<number | null>(null);
  useEffect(() => {
    if (cursor == null || busy || error) return;
    let observer: IntersectionObserver | undefined;
    const frame = window.requestAnimationFrame(() => {
      const target = element.current;
      const root = target?.closest(".stream-scroll");
      if (!target || !root) return;
      observer = new IntersectionObserver(entries => {
        if (!entries.some(entry => entry.isIntersecting) || requested.current === cursor) return;
        requested.current = cursor;
        callback.current();
      }, { root, rootMargin: edge === "top" ? "240px 0px 0px" : "0px 0px 240px" });
      observer.observe(target);
    });
    return () => { window.cancelAnimationFrame(frame); observer?.disconnect(); };
  }, [cursor, busy, error, edge]);
  return <div ref={element} className={`page-sentinel page-sentinel-${edge}`} aria-hidden="true" />;
}
