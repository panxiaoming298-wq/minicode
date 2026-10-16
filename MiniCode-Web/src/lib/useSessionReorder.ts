import { useCallback, useEffect, useRef, useState, type MouseEvent, type PointerEvent, type RefObject } from "react";
import type { WorkspaceDropPosition } from "./workspaceOrder";

export interface SessionDropTarget {
  id: string;
  group: string;
  position: WorkspaceDropPosition;
}

interface SessionDrag {
  id: string;
  group: string;
  pointerId: number;
  startX: number;
  startY: number;
  x: number;
  y: number;
  active: boolean;
  target: SessionDropTarget | null;
}

function dropTarget(root: HTMLDivElement | null, drag: SessionDrag): SessionDropTarget | null {
  const node = document.elementFromPoint(drag.x, drag.y)?.closest<HTMLElement>("[data-session-group][data-session-id]");
  if (!node || !root?.contains(node) || node.dataset.sessionGroup !== drag.group || node.dataset.sessionId === drag.id) return null;
  const bounds = node.getBoundingClientRect();
  return { id: node.dataset.sessionId!, group: drag.group, position: drag.y < bounds.top + bounds.height / 2 ? "before" : "after" };
}

export function useSessionReorder(root: RefObject<HTMLDivElement | null>, onMove: (source: string, target: SessionDropTarget) => void) {
  const dragRef = useRef<SessionDrag | null>(null);
  const suppressClickRef = useRef(false);
  const [dragging, setDragging] = useState<{ id: string; group: string } | null>(null);
  const [target, setTarget] = useState<SessionDropTarget | null>(null);

  function suppressNextClick() {
    suppressClickRef.current = true;
    window.setTimeout(() => { suppressClickRef.current = false; }, 0);
  }

  const cancel = useCallback(() => {
    if (dragRef.current?.active) suppressNextClick();
    dragRef.current = null;
    setDragging(null);
    setTarget(null);
  }, []);

  function updateTarget(drag: SessionDrag) {
    drag.target = dropTarget(root.current, drag);
    setTarget(current => current?.id === drag.target?.id && current?.group === drag.target?.group
      && current?.position === drag.target?.position ? current : drag.target);
  }

  useEffect(() => {
    if (!dragging) return;
    let frame = 0;
    function scroll() {
      const drag = dragRef.current;
      const navigation = root.current;
      if (!drag || !navigation) return;
      const bounds = navigation.getBoundingClientRect();
      const previous = navigation.scrollTop;
      if (drag.x >= bounds.left && drag.x <= bounds.right) {
        if (drag.y < bounds.top + 32) navigation.scrollTop -= 10;
        else if (drag.y > bounds.bottom - 32) navigation.scrollTop += 10;
      }
      if (navigation.scrollTop !== previous) updateTarget(drag);
      frame = requestAnimationFrame(scroll);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); cancel(); }
    }
    frame = requestAnimationFrame(scroll);
    document.addEventListener("keydown", onKeyDown);
    return () => { cancelAnimationFrame(frame); document.removeEventListener("keydown", onKeyDown); };
  }, [cancel, dragging, root]);

  return {
    dragging, target, cancel,
    begin(event: PointerEvent<HTMLButtonElement>, id: string, group: string) {
      if (event.button !== 0 || !event.isPrimary) return;
      if (event.pointerType === "touch" && !event.currentTarget.classList.contains("session-reorder-handle")) return;
      dragRef.current = { id, group, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
        x: event.clientX, y: event.clientY, active: false, target: null };
      event.currentTarget.setPointerCapture(event.pointerId);
    },
    move(event: PointerEvent<HTMLButtonElement>) {
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      drag.x = event.clientX;
      drag.y = event.clientY;
      if (!drag.active && Math.hypot(drag.x - drag.startX, drag.y - drag.startY) < 6) return;
      event.preventDefault();
      drag.active = true;
      setDragging(current => current?.id === drag.id && current?.group === drag.group ? current : { id: drag.id, group: drag.group });
      updateTarget(drag);
    },
    finish(event: PointerEvent<HTMLButtonElement>) {
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      if (drag.active) {
        drag.x = event.clientX;
        drag.y = event.clientY;
        updateTarget(drag);
        if (drag.target) onMove(drag.id, drag.target);
      }
      cancel();
    },
    suppressClick(event: MouseEvent<HTMLDivElement>) {
      if (!suppressClickRef.current) return;
      suppressClickRef.current = false;
      event.preventDefault();
      event.stopPropagation();
    },
  };
}
