import { useEffect, useState } from "react";
import { Database, FileSymlink, History, Layers3, NotebookPen, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import type { ContextKnowledgeView, InstructionLayerView, MemoryEntryView, SavedTaskNote } from "../types";
import { TaskMemoryExport } from "./TaskMemoryExport";
import "./ContextKnowledge.css";

const scopes: Record<string, string> = { user: "个人 · 跨项目", project: "项目共享", local: "项目本地", global: "全局" };
const origins: Record<string, string> = { user_explicit: "用户明确保存", user_corrected: "用户已纠正",
  agent_reflection: "Agent 任务总结", agent_runtime: "Agent 任务记录", agent_consolidated: "Agent 自动整理", file_import: "来自记忆文件", unknown: "来源未记录" };
const stamp = (value?: number | null) => value && Number.isFinite(value) ? new Date(value * 1000).toLocaleString("zh-CN") : "时间未记录";
const historyStates: Record<string, string> = { eligible: "可按需检索", legacy: "来源未关联 · 不自动引用", archived: "已归档 · 不自动引用",
  source_archived: "来源在回收站 · 不自动引用", source_missing: "来源会话已不存在 · 不自动引用", source_unavailable: "来源无法核实 · 不自动引用" };
const sourceStates: Record<string, string> = { active: "正常", archived: "回收站", missing: "已不存在", unavailable: "无法核实" };
const scopeName = (scope: string, standalone?: boolean) => standalone && scope !== "user"
  ? scope === "local" ? "独立对话本地" : "独立对话共享" : scopes[scope] || scope;
export function ruleStatus(rule: InstructionLayerView, sources: ContextKnowledgeView["sources"]): string {
  if (rule.status === "unreadable") return "读取失败";
  if (rule.status === "empty") return "文件为空";
  if (rule.status === "missing") return "未配置";
  const used = sources?.rules.find(item => item.name === rule.name && item.path === rule.path && item.status === "loaded");
  if (used) return used.contentHash === rule.contentHash ? "最近请求已加载" : "已修改 · 最近请求为旧版";
  if (sources?.rulesKnown) return "最近请求未加载";
  return rule.status === "available" ? "可加载 · 尚无请求记录" : "加载状态未记录";
}

function Rule({ rule, sources, sessionId, onOpenFile }: { rule: InstructionLayerView; sources: ContextKnowledgeView["sources"];
  sessionId?: string; onOpenFile?: (rule: InstructionLayerView) => void }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const title = rule.kind === "user" ? "用户偏好" : rule.kind === "managed" ? "管理规则" : rule.scope === "global" ? "通用规则" : "项目规则";
  const filename = rule.path?.split(/[\\/]/).pop() || rule.name;
  const missing = rule.status === "missing" || !rule.exists;
  const unavailable = missing ? "文件尚不存在" : rule.status === "unreadable" ? "文件无法读取" : !sessionId ? "选择会话后可打开" : "";
  return <details className="knowledge-item knowledge-rule">
    <summary><span><strong>{title} · {filename}</strong>
      <small>{scopes[rule.scope] || rule.scope} · {ruleStatus(rule, sources)}</small></span>
      {onOpenFile && rule.path && <button type="button" className="knowledge-rule-open" disabled={Boolean(unavailable)}
        aria-label={`打开规则文件 ${scopes[rule.scope] || rule.scope} · ${filename}`} title={unavailable ? `${unavailable} · ${rule.path}` : `打开 ${rule.path}`}
        onClick={event => { event.preventDefault(); event.stopPropagation(); onOpenFile(rule); }}>
        <FileSymlink size={13} aria-hidden="true" />打开文件
      </button>}
    </summary>
    <p className="knowledge-path">{rule.path || "旧记录未保存路径"}</p>
    <p className="section-note">{missing ? "文件尚不存在，可在上述路径配置规则。" : `来自规则文件 · ${stamp(rule.updatedAt)}。文件名不代表作者。`}</p>
    {rule.preview && text === null && <p className="knowledge-text">{rule.preview}</p>}
    {sessionId && rule.status === "available" && <button className="button button-secondary" disabled={busy} onClick={async () => {
      setBusy(true); setError("");
      try { setText((await api.contextRule(sessionId, rule.name)).content); }
      catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取规则文件"); }
      finally { setBusy(false); }
    }}>{busy ? "正在读取…" : "查看当前文件全文"}</button>}
    {text !== null && <pre className="knowledge-text">{text}</pre>}
    {error && <p role="alert" className="knowledge-error">{error}</p>}
  </details>;
}

const learningStates: Record<string, string> = {
  eligible: "依据有效，可供后续请求参考", expired: "已到复核期限，暂停自动引用", archived: "已归档，停止自动引用",
  legacy_unverified: "旧经验缺少依据版本，暂停自动引用", source_changed: "依据已被更新，暂停自动引用",
  source_archived: "依据已归档，暂停自动引用", source_missing: "依据已删除，暂停自动引用",
  source_unavailable: "暂时无法核对依据，暂停自动引用", source_unverified: "依据未经核实，暂停自动引用",
  confirmation_changed: "用户确认的知识已更新，暂停自动引用",
  duplicate: "重复记录已归档",
};

function Memory({ entry, referenced, sessionId, onSaved, history = false, standalone }: { entry: MemoryEntryView; referenced?: boolean;
  sessionId?: string; onSaved: (view: ContextKnowledgeView) => void; history?: boolean; standalone?: boolean }) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(entry.content);
  const [editRevision, setEditRevision] = useState(entry.revision);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save(change: { content?: string; archived?: boolean }) {
    if (!sessionId) return;
    setBusy(true); setError("");
    const revision = change.content === undefined ? entry.revision : editRevision;
    try { onSaved(await api.reviseMemory(sessionId, entry.scope, entry.id, revision, change)); setEditing(false); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败，请重试"); }
    finally { setBusy(false); }
  }
  return <details className="knowledge-item">
    <summary><span><strong>{(history ? entry.title || entry.content : entry.content).slice(0, 85)}</strong><small>{origins[entry.origin] || "来源未记录"} · {scopeName(entry.scope, standalone)}
      {entry.archived ? " · 已归档" : ""}{referenced === undefined ? " · 引用未记录" : referenced ? " · 最近请求已引用" : " · 最近请求未引用此版本"}</small></span></summary>
    {history && <>
      <p className="section-note">{historyStates[entry.retrievalStatus || ""] || "自动引用状态未记录"}</p>
      <p className="section-note">保存时间：{stamp(entry.createdAt)}</p>
      {entry.sourceSessions?.length ? entry.sourceSessions.map(source => <p className="knowledge-path" key={source.sessionId}>
        来源会话：{source.sessionId === sessionId ? "本会话" : source.sessionId} · {sourceStates[source.status]}</p>)
        : <p className="section-note">旧记录没有关联来源会话，无法判断是否来自已删除的对话。</p>}
      {!!entry.sourceTurns?.length && <p className="knowledge-path">来源轮次：{entry.sourceTurns.join("；")}</p>}
    </>}
    <p className="knowledge-text">{entry.content}</p>
    {entry.learningStatus && <div className="section-note">
      <p>{learningStates[entry.retentionReason || entry.learningStatus] || "自动经验状态待核对"}</p>
      {entry.reviewAfter && <p>复核期限：{stamp(entry.reviewAfter)}</p>}
      {!!entry.sourceVersions && <p>依据：{Object.keys(entry.sourceVersions).length} 条记忆的保存版本。</p>}
      {entry.learningStatus !== "eligible" && <p>可通过“纠正内容”确认当前知识；恢复归档后仍会检查依据是否有效。</p>}
    </div>}
    <p className="section-note">更新：{stamp(entry.updatedAt)} · {entry.category}</p>
    <p className="knowledge-path">{entry.path}</p>
    <p className="section-note">来源：{entry.sources.length ? entry.sources.join("；") : "旧记忆没有保存来源，不能推断作者。"}</p>
    {editing ? <div className="knowledge-editor">
      {editRevision !== entry.revision && <p role="status" className="section-note">这条记忆已更新，草稿仍基于旧版本。请对照上方最新内容，取消后重新编辑；刷新不会覆盖你的草稿。</p>}
      <label>修改记忆<textarea aria-label="修改记忆" value={content} onChange={event => setContent(event.target.value)} maxLength={16384} /></label>
      <div className="knowledge-actions"><button className="button button-primary" disabled={busy || !content.trim()} onClick={() => void save({ content })}>保存修改</button>
        <button className="button button-secondary" disabled={busy} onClick={() => setEditing(false)}>取消</button></div>
    </div> : sessionId && <div className="knowledge-actions">
      <button className="button button-secondary" disabled={busy} onClick={() => { setContent(entry.content); setEditRevision(entry.revision); setError(""); setEditing(true); }}>纠正内容</button>
      <button className="button button-secondary" disabled={busy} onClick={() => void save({ archived: !entry.archived })}>{entry.archived ? history ? "恢复摘要" : "恢复记忆" : history ? "归档摘要" : "归档记忆"}</button>
    </div>}
    {error && <p role="alert" className="knowledge-error">{error}</p>}
  </details>;
}

function TaskNote({ note, sources }: { note: SavedTaskNote; sources: ContextKnowledgeView["sources"] }) {
  const referenced = sources?.notes.some(item => item.sourceEventId === note.sourceEventId && item.contentHash === note.contentHash);
  const referenceLabel = referenced ? "最近请求已引用" : sources?.taskMemoryKnown ? "最近请求未引用此版本" : "引用未记录";
  const fields: Array<[string, string[]]> = [
    ["已确认事项", note.fields.confirmed_findings.map(item => item.text)],
    ["已排除事项", note.fields.ruled_out.map(item => item.text)],
    ["待解决问题", note.fields.open_questions], ["下一步", note.fields.next_steps],
  ];
  const evidence = [...new Set([...note.fields.confirmed_findings, ...note.fields.ruled_out].flatMap(item => item.source_event_ids))];
  return <details className="knowledge-item knowledge-task-note">
    <summary><span><strong>交接笔记 · 第 {note.revision} 版{note.owner !== "primary" ? ` · ${note.owner}` : ""}</strong>
      <small>{note.origin === "task_store" ? "本会话保存" : "会话历史保留"} · {referenceLabel}</small></span></summary>
    {fields.map(([label, values]) => <div className="knowledge-note-field" key={label}>
      <h3>{label}</h3>{values.length ? <ul>{values.map((text, index) => <li key={index}>{text}</li>)}</ul> : <p className="section-note">未记录</p>}
    </div>)}
    <details className="knowledge-missing"><summary>来源与保存信息</summary>
      <p className="section-note">保存时间：{stamp(note.savedAt)}</p>
      <p className="knowledge-path">来源事件：{note.sourceEventId}</p>
      {evidence.length > 0 && <p className="knowledge-path">关联依据：{evidence.join("；")}</p>}
      {note.origin === "session_history" && <p className="section-note">根据当前会话保留的工具调用与成功保存回执展示，包含分支继承的历史记录。</p>}
    </details>
  </details>;
}

export function ContextKnowledge({ initial, sessionId, requestId, onOpenRule }: { initial: ContextKnowledgeView; sessionId?: string;
  requestId?: string; onOpenRule?: (rule: InstructionLayerView) => void }) {
  const [loaded, setLoaded] = useState<ContextKnowledgeView | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!sessionId) return;
    const controller = new AbortController();
    setBusy(true);
    void api.contextKnowledge(sessionId, controller.signal).then(value => {
      if (!Array.isArray(value.rules) || !value.memory || !Array.isArray(value.memory.scopes)) throw new Error("服务尚未返回规则与记忆详情，请更新本机服务后重试。");
      if (!controller.signal.aborted) { setLoaded(value); setError(""); }
    }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "详情读取失败"); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [sessionId, requestId, revision]);
  const view = loaded || initial;
  const pendingSources = Boolean(requestId && view.sources && view.sources.requestId !== requestId);
  const sources = pendingSources ? undefined : view.sources;
  const rules = view.rules.filter(rule => rule.status !== "missing" && (rule.status !== undefined || rule.exists));
  const missing = view.rules.filter(rule => rule.status === "missing" || (rule.status === undefined && !rule.exists));
  const entries = view.memory.entries || [];
  const history = view.memory.taskHistory || [];
  const standalone = view.memory.workspaceScope === "standalone";
  const currentHistory = history.filter(entry => !entry.archived && entry.sourceSessions?.some(source => source.sessionId === sessionId));
  const sharedHistory = history.filter(entry => !entry.archived && entry.sourceSessions?.length && !currentHistory.includes(entry));
  const legacyHistory = history.filter(entry => !entry.archived && !entry.sourceSessions?.length);
  const archivedHistory = history.filter(entry => entry.archived);
  const usedHistory = sources?.memoryKnown ? sources.memories.filter(entry => entry.kind === "task_history").length : null;
  const taskNotes = view.taskNotes;
  const notes = taskNotes?.notes || [];
  const latestNotes = notes.filter(note => note.latest);
  const olderNotes = notes.filter(note => !note.latest);
  const receiptOnlyNotes = sources?.notes.filter(note => !notes.some(savedNote => savedNote.sourceEventId === note.sourceEventId)) || [];
  const count = view.memory.scopes.reduce((total, scope) => total + scope.entries, 0);
  const incomplete = !view.memory.status || view.memory.status !== "ready";
  const used = sources?.memoryKnown ? sources.memories.filter(entry => entry.kind !== "task_history").length : null;
  const changed = (value: ContextKnowledgeView) => { setLoaded(value); setSaved(true); };
  const memoryRow = (entry: MemoryEntryView) => <Memory key={`${entry.scope}:${entry.id}`} entry={entry} sessionId={sessionId} onSaved={changed} standalone={standalone} history={history.includes(entry)}
    referenced={sources?.memoryKnown ? sources.memories.some(item => item.id === entry.id && item.scope === entry.scope && item.contentHash === entry.contentHash) : undefined} />;
  return <div className="context-knowledge">
    <div className="knowledge-toolbar"><span>规则与记忆详情</span>{sessionId && <button className="button button-secondary" disabled={busy} onClick={() => setRevision(value => value + 1)}><RefreshCw size={12} />{busy ? "刷新中…" : "刷新"}</button>}</div>
    {sessionId && <TaskMemoryExport key={sessionId} sessionId={sessionId} />}
    {error && <p role="alert" className="knowledge-error">{error} 当前展示已有记录。</p>}
    {pendingSources && <p className="section-note">当前请求的来源记录尚未就绪，可刷新后重试。</p>}
    {saved && <p role="status" className="section-note">已保存。已发送的请求不变；后续重新读取记忆时使用修改后的内容。</p>}
    <section className="inspector-section">
      <div className="section-heading"><h2 data-inspector-section="instructions" tabIndex={-1}><Layers3 size={15} />规则与偏好</h2>
        <span>{sources?.rulesKnown ? `最近请求加载 ${sources.rules.filter(rule => rule.status === "loaded").length} 份` : "加载情况未记录"}</span></div>
      <p className="section-note">用户与项目维护的行为约定。已加载表示送入请求，不保证模型一定遵守。</p>
      {rules.map(rule => <Rule key={rule.name + rule.contentHash} rule={rule} sources={sources} sessionId={sessionId} onOpenFile={onOpenRule} />)}
      {!rules.length && <p className="section-empty">{view.rules.length ? "尚未配置规则文件。" : "尚未获取规则文件信息。"}</p>}
      {missing.length > 0 && <details className="knowledge-missing"><summary>可配置来源（{missing.length}）</summary>
        {missing.map(rule => <Rule key={rule.name} rule={rule} sources={sources} sessionId={sessionId} onOpenFile={onOpenRule} />)}</details>}
    </section>
    <section className="inspector-section">
      <div className="section-heading"><h2 data-inspector-section="memory" tabIndex={-1}><Database size={15} />长期记忆</h2>
        <span>{incomplete ? "统计不完整" : `已保存 ${count} 条`}</span></div>
      <p className="section-note">可由用户明确保存或 Agent 整理。个人、项目、本地表示适用范围；记忆作为参考，不会自动变成项目规则。</p>
      <p className="section-note">{used === null ? "最近请求的引用情况尚未记录。" : `最近请求引用 ${used} 条长期记忆。`}</p>
      {view.memory.scopes.map(scope => <p className="knowledge-scope" key={scope.name}><strong>{scopeName(scope.name, standalone)}</strong>
        <span>{scope.status === "unavailable" ? "读取失败，请检查记忆文件" : scope.status === "ready" ? `${scope.entries} 条` : "旧版统计，范围未核实"}</span></p>)}
      {entries.filter(entry => !entry.archived).map(memoryRow)}
      {!entries.some(entry => !entry.archived) && !incomplete && <p className="section-empty">尚无长期记忆。当前聊天内容和任务笔记单独保留。</p>}
      {entries.some(entry => entry.archived) && <details className="knowledge-missing"><summary>已归档记忆</summary>{entries.filter(entry => entry.archived).map(memoryRow)}</details>}
    </section>
    <section className="inspector-section">
      <div className="section-heading"><h2 data-inspector-section="task-notes" tabIndex={-1}><NotebookPen size={15} />任务笔记</h2>
        <span>{taskNotes?.status === "ready" ? `已保存 ${latestNotes.length} 份` : taskNotes?.status === "partial" ? `已找到 ${latestNotes.length} 份 · 部分未读取` : "保存情况未读取"}</span></div>
      <p className="section-note">本会话的交接笔记，保存任务进展、待办和当时的验证结果。保存情况与最近请求是否引用分别显示。</p>
      {taskNotes?.error && <p role="alert" className="knowledge-error">{taskNotes.error}</p>}
      {latestNotes.map(note => <TaskNote key={note.sourceEventId} note={note} sources={sources} />)}
      {olderNotes.length > 0 && <details className="knowledge-missing"><summary>交接笔记历史版本（{olderNotes.length}）</summary>
        {olderNotes.map(note => <TaskNote key={note.sourceEventId} note={note} sources={sources} />)}</details>}
      {taskNotes?.truncated && <p className="section-note">仅展示最近保存的版本。</p>}
      {!notes.length && taskNotes?.status === "ready" && <p className="section-empty">此会话尚未保存交接笔记。</p>}
      {!taskNotes && <p className="section-empty">尚未读取已保存的交接笔记，可刷新重试。</p>}
      {receiptOnlyNotes.map(note => <details className="knowledge-item" key={note.id}><summary>请求中的交接笔记 · 最近请求已引用</summary>
        <pre className="knowledge-text">{note.text}</pre>{note.truncated && <p className="section-note">此处仅展示部分内容。</p>}
        <p className="knowledge-path">来源事件：{note.sourceEventId || "未记录"} · {note.id}</p></details>)}
      {sources && <details className="knowledge-missing"><summary>最近请求来源记录 · {stamp(sources.capturedAt)}</summary>
        <p className="knowledge-path">请求：{sources.requestId}</p><p className="knowledge-path">运行：{sources.runId || "未记录"}</p>
        <p className="section-note">分页任务资料引用：{sources.taskMemoryKnown ? sources.taskReferences.length : "未记录"}。此记录对应最近捕获的主 Agent 请求。</p></details>}
    </section>
    <section className="inspector-section knowledge-history">
      <div className="section-heading"><h2><History size={15} />历史任务摘要</h2><span>可检索 {history.filter(entry => entry.autoRetrieval).length} 条</span></div>
      <p className="section-note">{standalone ? "独立对话共用的历史摘要库，可能包含其他独立对话的记录。" : "当前工作空间共享的历史摘要，可能来自其他会话。"}这里只保存当时的过程，不代表当前任务已完成。</p>
      <p className="section-note">{usedHistory === null ? "最近请求的历史摘要引用情况尚未记录。" : `最近请求引用 ${usedHistory} 条历史摘要。`}下列检索状态用于后续请求，旧请求记录保持原样。</p>
      {currentHistory.length > 0 && <details className="knowledge-missing"><summary>来自本会话（{currentHistory.length}）</summary>{currentHistory.map(memoryRow)}</details>}
      {sharedHistory.length > 0 && <details className="knowledge-missing"><summary>来自其他会话（{sharedHistory.length}）</summary>{sharedHistory.map(memoryRow)}</details>}
      {legacyHistory.length > 0 && <details className="knowledge-missing"><summary>旧版摘要 · 来源未关联（{legacyHistory.length}）</summary>
        <p className="section-note">保留原文供查看和整理，默认不自动引用。恢复归档也不会补齐来源。</p>{legacyHistory.map(memoryRow)}</details>}
      {archivedHistory.length > 0 && <details className="knowledge-missing"><summary>已归档摘要（{archivedHistory.length}）</summary>{archivedHistory.map(memoryRow)}</details>}
      {!history.length && <p className="section-empty">{incomplete ? "历史摘要尚未完整读取。" : "尚无历史任务摘要。"}</p>}
      <p className="section-note">来源对话进入回收站后，自动摘要停止引用；永久删除时清理仅归属该对话的自动摘要。用户明确保存或纠正的记忆、多个来源的记录及无法关联的旧摘要单独保留。</p>
    </section>
  </div>;
}
