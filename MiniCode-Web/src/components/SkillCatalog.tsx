import { ArrowLeft, ArrowUpRight, Check, ChevronDown, CircleAlert, CircleCheck, Copy, Download, Eye, FolderOpen, Info, LoaderCircle, RefreshCw, Search, Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { ConversationScope, SkillInventory, SkillInventoryItem, SkillPreview } from "../types";
import { skillAvailability, skillSourceLabel, skillStatusLabel } from "../lib/skillPresentation";
import "./SkillCatalog.css";

interface Props {
  workspace: string;
  scope: ConversationScope;
  invocationWorkspace: string;
  invocationScope: ConversationScope;
  importForm?: ReactNode;
  refreshVersion?: number;
  projectLabel?: string;
  onChooseProject?: () => void;
  initialName?: string;
  initialExtension?: string;
  focusVersion?: number;
  onUse: (command: string) => void;
  onManage: (extensionKey: string) => void;
  onImport: () => void;
  onRefresh?: () => void;
  onViewConversation?: (name: string) => void;
}

export function SkillCatalog({ workspace, scope, invocationWorkspace, invocationScope, importForm, refreshVersion = 0,
  projectLabel, onChooseProject, initialName = "", initialExtension = "", focusVersion = 0, onUse, onManage, onImport, onRefresh, onViewConversation }: Props) {
  const [skills, setSkills] = useState<SkillInventoryItem[]>([]);
  const [roots, setRoots] = useState<SkillInventory["discoveryRoots"]>([]);
  const [selectedKey, setSelectedKey] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [detailOpen, setDetailOpen] = useState(false);
  const alive = useRef(true);
  const revision = useRef(0);
  const actionRevision = useRef(0);
  const selection = useRef("");
  const list = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const detail = useRef<HTMLElement>(null);
  const appliedFocus = useRef("");
  const sameInvocationContext = scope === invocationScope && (scope === "standalone" || workspace === invocationWorkspace);

  async function refresh(initial = false) {
    const focusToken = JSON.stringify([initialName, initialExtension, focusVersion]);
    const applyFocus = initial && Boolean(initialName || initialExtension) && appliedFocus.current !== focusToken;
    const ticket = ++revision.current;
    setLoading(true); setError(""); setPreview(null); setNotice(""); setBusy("");
    try {
      const next = await api.getSkillInventory(workspace, scope, {workspace: invocationWorkspace, scope: invocationScope});
      if (!alive.current || ticket !== revision.current) return;
      setSkills(next.skills);
      setRoots(next.discoveryRoots || []);
      if (applyFocus) appliedFocus.current = focusToken;
      setSelectedKey(current => {
        const preferred = applyFocus ? next.skills.find(item => (!initialName || item.name === initialName) && (!initialExtension || item.extensionKey === initialExtension))?.key : current;
        return next.skills.find(item => item.key === preferred)?.key || next.skills[0]?.key || "";
      });
      if (!initial) onRefresh?.();
    } catch (reason) {
      if (alive.current && ticket === revision.current) setError(reason instanceof Error ? reason.message : "读取技能列表失败。");
    } finally {
      if (alive.current && ticket === revision.current) setLoading(false);
    }
  }

  useEffect(() => {
    alive.current = true;
    search.current?.focus();
    void refresh(true);
    return () => { alive.current = false; revision.current += 1; };
  }, [workspace, scope, invocationWorkspace, invocationScope, initialName, initialExtension, focusVersion, refreshVersion]);

  useEffect(() => {
    if (initialName || initialExtension) {
      setQuery(""); setFilter("all"); setDetailOpen(true);
    }
  }, [initialName, initialExtension, focusVersion]);

  const filtered = useMemo(() => skills.filter(item => (filter === "all" || item.scope === filter)
    && [item.name, item.description, item.path, skillSourceLabel(item)].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [skills, query, filter]);
  const selected = filtered.find(item => item.key === selectedKey) || filtered[0] || null;
  selection.current = selected?.key || "";
  useEffect(() => {
    actionRevision.current += 1; setPreview(null); setBusy(""); setNotice("");
    detail.current?.scrollTo({ top: 0 });
  }, [selected?.key]);
  const winner = selected?.shadowedBy ? skills.find(item => item.key === selected.shadowedBy) : null;
  const availability = selected ? skillAvailability(selected, sameInvocationContext) : null;

  function selectSkill(key: string) {
    setSelectedKey(key); setDetailOpen(true);
    if (window.matchMedia("(max-width: 819px)").matches) requestAnimationFrame(() => detail.current?.focus());
  }

  function showWinner() {
    if (!winner) return;
    setFilter("all"); setQuery(""); selectSkill(winner.key);
  }

  async function action(kind: "preview" | "use" | "copy") {
    if (!selected || loading || error) return;
    const key = selected.key;
    const ticket = revision.current;
    const actionTicket = ++actionRevision.current;
    const current = () => alive.current && ticket === revision.current && actionTicket === actionRevision.current && selection.current === key;
    setBusy(kind); setNotice("");
    try {
      if (kind === "copy") {
        await navigator.clipboard.writeText(selected.path);
        if (current()) setNotice("已复制 Skill 文件路径。");
      } else if (kind === "preview") {
        if (preview?.key === key) { setPreview(null); return; }
        const result = await api.previewSkill(workspace, scope, key);
        if (current()) setPreview(result);
      } else {
        const result = await api.skillCommand(workspace, scope, key, {workspace: invocationWorkspace, scope: invocationScope});
        if (current()) onUse(result.command);
      }
    } catch (reason) {
      if (current()) setNotice(reason instanceof Error ? reason.message : "操作失败，请刷新列表后重试。");
    } finally { if (current()) setBusy(""); }
  }

  const discovery = roots.length > 0 && <details className="capability-discovery-roots"><summary><FolderOpen size={15} aria-hidden="true" /><span>发现目录</span><ChevronDown size={14} className="capability-discovery-chevron" aria-hidden="true" /></summary><ul>
    {roots.map(root => <li key={`${root.source}:${root.scope}`}><span>{root.scope === "project" ? "项目" : "全局"}
      {root.source === "extensions" ? "扩展" : root.source.startsWith("compat_") ? " Claude 兼容" : " Skill"} · {root.exists ? "目录存在" : "尚未创建"}</span><code>{root.path}</code></li>)}
  </ul></details>;

  return <div className={`skill-catalog ${detailOpen ? "is-detail-open" : ""} ${importForm ? "is-installing" : ""} ${skills.length === 0 ? "is-empty" : ""}`} aria-label="技能目录">
    <section className="skill-catalog-sidebar" aria-label="Skill 目录">
      <div className="skill-catalog-toolbar">
        <label className="extension-search"><Search size={15} /><input ref={search} aria-label="搜索技能" placeholder="搜索名称或用途" value={query} onChange={event => setQuery(event.target.value)} /></label>
        <button className="icon-button" aria-label="刷新 Skill 目录" title="刷新 Skill 目录" onClick={() => void refresh()} disabled={loading}><RefreshCw size={16} className={loading ? "spin" : ""} /></button>
      </div>
      <div className="skill-catalog-filters" role="group" aria-label="技能来源">
        <span className="capability-filter-label">来源</span>
        {scope === "project" ? [['all', '全部'], ['project', '本项目'], ['global', '全局']].map(([value, label]) => <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>) : <span className="capability-source-label">全局</span>}
      </div>
      <p className="skill-catalog-count" aria-live="polite">{loading ? "正在发现技能…" : `${filtered.length} 个技能${filtered.length !== skills.length ? ` / 共 ${skills.length} 个` : ""} · ${filtered.filter(item => item.canInvokeInConversation).length} 个当前对话可用`}</p>
      {error ? <p role="alert" className="skill-catalog-message">{error}</p> : <div ref={list} className="skill-catalog-list" role="listbox" aria-label="已发现 Skill"
        onKeyDown={event => {
          if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) || !filtered.length) return;
          event.preventDefault();
          const index = filtered.findIndex(item => item.key === selected?.key);
          const next = event.key === "Home" ? 0 : event.key === "End" ? filtered.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + filtered.length) % filtered.length;
          setSelectedKey(filtered[next].key);
          list.current?.querySelectorAll<HTMLButtonElement>('[role="option"]')[next]?.focus();
        }}>
        {filtered.map(item => {
          const state = skillAvailability(item, sameInvocationContext);
          return <button type="button" key={item.key} role="option" aria-selected={selected?.key === item.key}
            tabIndex={selected?.key === item.key ? 0 : -1}
            aria-label={`${item.name} · ${skillSourceLabel(item)} · ${state.label}`} onClick={() => selectSkill(item.key)}>
            <span className="skill-row-heading"><strong>{item.name}</strong></span>
            <p>{item.description || "没有描述"}</p>
            <span className="skill-row-meta"><small>{skillSourceLabel(item)}</small><span className={`skill-status tone-${state.tone}`}>{state.label}</span></span>
          </button>;
        })}
        {!loading && !filtered.length && <p className="capability-list-empty">{skills.length ? "没有匹配项" : "此范围没有技能"}</p>}
      </div>}
    </section>
    <section ref={detail} tabIndex={-1} className="skill-catalog-detail" aria-label="技能详情">
      {importForm || (selected && !error ? <>
        <button className="skill-list-back" onClick={() => {
          setDetailOpen(false);
          requestAnimationFrame(() => list.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus());
        }}><ArrowLeft size={16} />返回技能列表</button>
        <div className="skill-detail-heading"><span className="skill-detail-icon"><Sparkles size={24} /></span><div><h2>{selected.name}</h2><p>{skillSourceLabel(selected)}{selected.version ? ` · ${selected.version}` : ""}</p></div></div>
        <p className="skill-detail-description">{selected.description || "此技能未提供说明。"}</p>
        <div className="skill-call-methods" aria-label="调用方式"><span>调用方式</span>
          <span className={!selected.userInvocable ? "is-unavailable" : ""}>{selected.userInvocable ? "手动调用" : "手动调用关闭"}</span>
          <span className={!selected.modelInvocable ? "is-unavailable" : ""}>{selected.modelInvocable ? "自动匹配" : "自动匹配关闭"}</span>
        </div>
        {availability && <div className={`skill-availability tone-${availability.tone}`} aria-label="当前对话调用状态">
          {availability.tone === "ready" ? <CircleCheck size={18} /> : availability.tone === "danger" ? <CircleAlert size={18} /> : <Info size={18} />}
          <div><strong>{availability.label}</strong><p>{availability.detail}</p>
            {!selected.canInvokeInConversation && winner ? <button className="skill-next-action" onClick={showWinner}>查看此范围生效版本<ArrowUpRight size={13} /></button>
              : !selected.canInvokeInConversation && !sameInvocationContext && onViewConversation && selected.status === "ready"
                ? <button className="skill-next-action" onClick={() => onViewConversation(selected.name)}>查看当前对话的技能<ArrowUpRight size={13} /></button> : null}
          </div>
        </div>}
        <div className="skill-detail-actions">
          <button className="button button-primary" onClick={() => void action("use")} disabled={!selected.canInvokeInConversation || loading || Boolean(busy)}>{busy === "use" ? <LoaderCircle size={15} className="spin" /> : <ArrowUpRight size={15} />}添加到对话</button>
          <button className="button button-secondary" onClick={() => void action("preview")} aria-expanded={preview?.key === selected.key} aria-controls="skill-content-preview" disabled={!selected.canPreview || loading || Boolean(busy)}>{busy === "preview" ? <LoaderCircle size={15} className="spin" /> : <Eye size={15} />}{preview?.key === selected.key ? "收起说明" : "查看说明"}</button>
        </div>
        {notice && <p role="status" className="skill-catalog-notice">{notice}</p>}
        {preview?.key === selected.key && <section id="skill-content-preview" className="skill-preview" aria-label="技能说明"><header><strong>技能说明 · SKILL.md</strong><span><Check size={13} />仅预览</span></header>{preview.truncated && <p>文件较大，展示前 256 KiB；完整内容请查看源文件。</p>}<pre>{preview.content}</pre></section>}
        {selected.extensionKey && <div className="skill-owner"><div><strong>由 {selected.extensionId} 提供</strong><p>信任、权限、启停和更新由所属扩展包统一管理。</p></div><button className="button button-secondary" onClick={() => onManage(selected.extensionKey!)}><FolderOpen size={15} />管理所属扩展</button></div>}
        <details key={selected.key} className="skill-technical-details"><summary><FolderOpen size={15} /><span>文件与发现信息</span><ChevronDown size={14} /></summary>
          <dl className="skill-facts">
            <div><dt>查看范围状态</dt><dd><strong>{skillStatusLabel(selected.status)}</strong><p>{selected.reason}</p>{selected.error && selected.error !== selected.reason && <p>{selected.error}</p>}</dd></div>
            <div><dt>文件位置</dt><dd className="skill-file-path">{selected.path || "无法解析安全的文件位置"}</dd></div>
            {winner && <div><dt>此范围生效版本</dt><dd><button className="skill-winner-link" onClick={showWinner}>{skillSourceLabel(winner)}<ArrowUpRight size={13} /></button><p className="skill-file-path">{winner.path}</p></dd></div>}
          </dl>
          {!selected.extensionKey && <p className="skill-file-note">此技能来自本地文件，可在原始目录中使用编辑器维护。</p>}
          <button className="button button-secondary" onClick={() => void action("copy")} disabled={!selected.path || loading || Boolean(busy)}><Copy size={15} />复制文件路径</button>
          {discovery}
        </details>
      </> : <><div className="skill-catalog-empty capability-empty-detail"><span className="capability-empty-icon"><Sparkles size={28} strokeWidth={1.5} aria-hidden="true" /></span>
        <h2>{loading ? "正在读取目录" : error ? "暂时无法读取技能" : skills.length ? "没有匹配的技能" : scope === "standalone" ? "本机全局尚未发现技能" : "此查看范围尚未发现技能"}</h2>
        <p>{error ? "请重试，或检查本机服务是否正常运行。" : skills.length ? "调整搜索词或来源筛选后查看。" : scope === "standalone" ? "全局技能可供独立对话和项目使用。项目技能需切换查看范围。" : "这里包含本项目和继承的全局技能，也可以导入新的技能。"}</p>
        {!loading && <div className="capability-empty-actions">{error ? <button className="button button-secondary" onClick={() => void refresh()}>重试</button>
          : skills.length ? <button className="button button-secondary" onClick={() => { setQuery(""); setFilter("all"); }}>清除筛选</button>
          : <><button className="button button-primary" onClick={onImport}><Download size={15} />导入技能</button>
            {scope === "standalone" && onChooseProject && <button className="button button-secondary" onClick={onChooseProject}>查看项目技能{projectLabel ? ` · ${projectLabel}` : ""}</button>}</>}</div>}
      </div>{!loading && !error && discovery}</>)}
    </section>
  </div>;
}
