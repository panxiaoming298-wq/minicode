import { Check, ChevronDown, Folder, FolderOpen, LoaderCircle, MessageCircle, Plus, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { RecentWorkspace } from "../types";
import "./ComposerAdditions.css";

export function ComposerProjectPicker({ workspace, label, standalone, projects, disabled, onSelect, onPick }: {
  workspace: string; label: string; standalone: boolean; projects: RecentWorkspace[]; disabled: boolean;
  onSelect: (path: string | null) => Promise<void>; onPick: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const dismiss = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);
  const identity = (path: string) => path.replace(/\\/g, "/").replace(/\/+$/, "").toLocaleLowerCase();
  const items = projects.filter(project => `${project.name} ${project.path}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  async function select(action: () => Promise<void>) {
    if (busy || disabled) return;
    setBusy(true); setError("");
    try { await action(); setOpen(false); trigger.current?.focus(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法切换项目，请重试。"); }
    finally { setBusy(false); }
  }
  return <div className="composer-project-picker" ref={root} onKeyDown={event => {
    if (event.key === "Escape" && open) { event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
    if (open && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      const choices = Array.from(root.current?.querySelectorAll<HTMLButtonElement>(".composer-popover button:not(:disabled)") || []);
      const index = choices.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === "ArrowDown" ? (index + 1) % choices.length : (index + choices.length - 1) % choices.length;
      if (choices[next]) { event.preventDefault(); choices[next].focus(); }
    }
  }}>
    <button ref={trigger} className="composer-context-item composer-project-trigger" disabled={disabled || busy}
      aria-label="切换项目" aria-expanded={open} aria-haspopup="dialog" title={standalone ? "选择项目或独立对话" : workspace}
      onClick={() => { setQuery(""); setError(""); setOpen(value => !value); }}>
      {busy ? <LoaderCircle size={16} className="spin" /> : standalone ? <MessageCircle size={16} /> : <Folder size={16} />}
      <span>{standalone ? "独立对话" : label}</span><ChevronDown size={13} />
    </button>
    {open && <div className="composer-popover project-switch-popover" role="dialog" aria-label="选择对话项目" aria-busy={busy}>
      <label className="composer-popover-search"><Search size={16} /><input ref={search} value={query}
        onChange={event => setQuery(event.target.value)} placeholder="搜索项目" aria-label="搜索项目" /></label>
      <div className="composer-project-list" role="listbox" aria-label="已打开的项目">
        {items.map(project => <button key={project.path} role="option" aria-selected={!standalone && identity(project.path) === identity(workspace)}
          disabled={busy || disabled} title={project.path} onClick={() => void select(() => onSelect(project.path))}>
          <Folder size={17} /><span><strong>{project.name}</strong><small>{project.path}</small></span>
          {!standalone && identity(project.path) === identity(workspace) && <Check size={17} />}
        </button>)}
        {!items.length && <p className="composer-popover-empty">{query ? "没有匹配的项目" : "还没有打开的项目"}</p>}
      </div>
      <div className="composer-popover-actions">
        <button disabled={busy || disabled} onClick={() => void select(onPick)}><Plus size={17} /><span>打开项目…</span><FolderOpen size={15} /></button>
        <button disabled={busy || disabled} onClick={() => void select(() => onSelect(null))}><MessageCircle size={17} /><span>独立对话</span>{standalone && <Check size={17} />}</button>
      </div>
      {busy && <p className="composer-popover-empty" role="status">正在打开项目…</p>}
      {error && <p className="composer-add-error" role="alert">{error}</p>}
    </div>}
  </div>;
}
