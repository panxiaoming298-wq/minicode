import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface RailRenameTarget {
  kind: "workspace" | "session";
  target: string;
  name: string;
}

export function RailRenameDialog({ item, onRename, onClose }: {
  item: RailRenameTarget;
  onRename: (kind: RailRenameTarget["kind"], target: string, name: string) => Promise<void>;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const savingRef = useRef(false);
  const [name, setName] = useState(item.name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const label = item.kind === "workspace" ? "项目名称" : "对话名称";

  useEffect(() => {
    const dialog = dialogRef.current!;
    dialog.showModal();
    inputRef.current?.select();
    return () => dialog.close();
  }, []);

  async function save() {
    const nextName = name.trim();
    if (!nextName || savingRef.current) return;
    if (nextName === item.name) { onClose(); return; }
    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      await onRename(item.kind, item.target, nextName);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重命名失败，请重试。");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  return createPortal(<dialog ref={dialogRef} className="project-dialog rail-rename-dialog" aria-labelledby="rail-rename-title"
    onCancel={event => { event.preventDefault(); if (!savingRef.current) onClose(); }}
    onClick={event => {
      if (event.target !== event.currentTarget || savingRef.current) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
    }}>
    <header>
      <div><strong id="rail-rename-title">重命名{item.kind === "workspace" ? "项目" : "对话"}</strong></div>
      <button className="icon-button" onClick={onClose} disabled={saving} aria-label="关闭重命名窗口"><X size={18} /></button>
    </header>
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <label htmlFor="rail-rename-name">{label}</label>
      <input ref={inputRef} id="rail-rename-name" value={name} onChange={event => setName(event.target.value)}
        maxLength={160} autoFocus autoComplete="off" disabled={saving} />
      {error && <p className="rail-rename-error" role="alert">{error}</p>}
      <div className="project-close-actions">
        <button type="button" className="button" onClick={onClose} disabled={saving}>取消</button>
        <button type="submit" className="button button-primary" disabled={saving || !name.trim()}>{saving ? "保存中…" : "保存"}</button>
      </div>
    </form>
  </dialog>, document.body);
}
