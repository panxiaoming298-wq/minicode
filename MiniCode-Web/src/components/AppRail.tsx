import {
  Check,
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  GripVertical,
  MessageSquare,
  PanelLeftClose,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Settings,
  SquarePen,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import { messagePreview } from "../lib/attachments";
import { loadWorkspaceOrder, mergeWorkspaceOrder, moveWorkspace, orderedWorkspaces, saveWorkspaceOrder,
  workspacePathIdentity as pathIdentity, type WorkspaceDropPosition } from "../lib/workspaceOrder";
import { loadSessionOrders, mergeSessionOrder, moveSession, orderedSessions, saveSessionOrders } from "../lib/sessionOrder";
import { useSessionReorder, type SessionDropTarget } from "../lib/useSessionReorder";
import { RailRenameDialog, type RailRenameTarget } from "./RailRenameDialog";

import type {
  ConversationScope,
  RecentWorkspace,
  SessionSummary,
} from "../types";

interface AppRailProps {
  workspace: string;
  projects: RecentWorkspace[];
  sessions: SessionSummary[];
  pinnedSessionIds: string[];
  pinnedWorkspacePaths: string[];
  selectedSessionId: string | null;
  conversationScope: ConversationScope;
  mobileOpen: boolean;
  collapsed: boolean;
  searchRequest: number;
  activePage: "conversation" | "trash" | "settings" | "extensions";
  archivingSessionId: string | null;
  closingProjectPath: string | null;
  onToggleCollapse: () => void;
  onCloseMobile: () => void;
  onNewStandaloneTask: () => void;
  onNewProjectTask: (workspace: string) => Promise<void>;
  onSelectSession: (sessionId: string) => void;
  onArchiveSession: (sessionId: string) => void;
  onOpenTrash: () => void;
  onChangeWorkspace: (workspace: string) => Promise<void>;
  onPickWorkspace: () => Promise<boolean>;
  onTogglePin: (kind: "workspace" | "session", target: string, pinned: boolean) => void;
  onRename: (kind: "workspace" | "session", target: string, name: string) => Promise<void>;
  onCloseProject: (workspace: string) => void;
  onOpenSettings: () => void;
}

function workspaceName(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts.at(-1) || path;
}

function samePath(left: string | null | undefined, right: string | null | undefined): boolean {
  return Boolean(left && right && pathIdentity(left) === pathIdentity(right));
}

function relativeTime(timestamp: number): string {
  const elapsed = Math.max(0, Date.now() - timestamp);
  if (elapsed < 60_000) return "刚刚";
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)} 分钟前`;
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)} 小时前`;
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(timestamp);
}

function sessionTitle(session: SessionSummary): string {
  return session.title || messagePreview(session.firstMessage || session.lastMessage || "新对话");
}

function sessionState(session: Pick<SessionSummary, "status" | "reviewPending">): string {
  if (session.status === "failed" && session.reviewPending) return "验收待处理";
  return {
    queued: "排队中",
    running: "运行中",
    stopping: "停止中",
    stopped: "已停止，可继续",
    failed: "失败，可重试",
  }[session.status] || "";
}

export function SessionState({ session }: { session: Pick<SessionSummary, "status" | "reviewPending"> }) {
  const label = sessionState(session);
  const tone = session.status === "failed" && session.reviewPending ? "pending" : session.status;
  return label ? <span className={`session-state session-state-${tone}`}><i />{label}</span> : null;
}

interface ProjectDropTarget {
  path: string;
  position: WorkspaceDropPosition;
}

interface ProjectDrag {
  path: string;
  pointerId: number;
  startX: number;
  startY: number;
  x: number;
  y: number;
  active: boolean;
  target: ProjectDropTarget | null;
}

function projectDropTarget(tree: HTMLDivElement | null, drag: ProjectDrag): ProjectDropTarget | null {
  const node = document.elementFromPoint(drag.x, drag.y)?.closest<HTMLElement>(".project-node");
  if (!node || !tree?.contains(node) || node.dataset.workspacePath === drag.path) return null;
  const header = node.querySelector(".project-item")!.getBoundingClientRect();
  return { path: node.dataset.workspacePath!, position: drag.y < header.top + header.height / 2 ? "before" : "after" };
}

function sameDropTarget(left: ProjectDropTarget | null, right: ProjectDropTarget | null): boolean {
  return left?.path === right?.path && left?.position === right?.position;
}

export function AppRail({
  workspace,
  projects,
  sessions,
  pinnedSessionIds,
  pinnedWorkspacePaths,
  selectedSessionId,
  conversationScope,
  mobileOpen,
  collapsed,
  searchRequest,
  activePage,
  archivingSessionId,
  closingProjectPath,
  onToggleCollapse,
  onCloseMobile,
  onNewStandaloneTask,
  onNewProjectTask,
  onSelectSession,
  onArchiveSession,
  onOpenTrash,
  onChangeWorkspace,
  onPickWorkspace,
  onTogglePin,
  onRename,
  onCloseProject,
  onOpenSettings,
}: AppRailProps) {
  const [orderStorage] = useState(() => {
    try { return window.localStorage; }
    catch { return null; }
  });
  const [workspaceOrder, setWorkspaceOrder] = useState(() => loadWorkspaceOrder(orderStorage));
  const [sessionOrders, setSessionOrders] = useState(() => loadSessionOrders(orderStorage));
  const sortedProjects = useMemo(() => orderedWorkspaces(projects, workspaceOrder), [projects, workspaceOrder]);
  const projectTreeRef = useRef<HTMLDivElement>(null);
  const navigationRef = useRef<HTMLDivElement>(null);
  const sessionReorder = useSessionReorder(navigationRef, reorderConversation);
  const projectDragRef = useRef<ProjectDrag | null>(null);
  const suppressProjectClickRef = useRef(false);
  const [draggingProject, setDraggingProject] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<ProjectDropTarget | null>(null);
  const [orderAnnouncement, setOrderAnnouncement] = useState("");
  const [projectDialogOpen, setProjectDialogOpen] = useState(false);
  const [closingProject, setClosingProject] = useState<RecentWorkspace | null>(null);
  const [renaming, setRenaming] = useState<RailRenameTarget | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const searchTriggerRef = useRef<HTMLButtonElement>(null);
  const [draftWorkspace, setDraftWorkspace] = useState(workspace);
  const [opening, setOpening] = useState<"picker" | "path" | null>(null);
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(
    () => new Set(workspace ? [pathIdentity(workspace)] : []),
  );
  const [mobileLayout, setMobileLayout] = useState(() => window.matchMedia("(max-width: 819px)").matches);
  const pinnedSessionSet = useMemo(() => new Set(pinnedSessionIds), [pinnedSessionIds]);
  const pinnedWorkspaceSet = useMemo(
    () => new Set(pinnedWorkspacePaths.map(pathIdentity)),
    [pinnedWorkspacePaths],
  );
  const sessionById = useMemo(() => new Map(sessions.map((item) => [item.sessionId, item])), [sessions]);
  const projectByPath = useMemo(
    () => new Map(projects.map((item) => [pathIdentity(item.path), item])),
    [projects],
  );
  const pinnedSessions = orderedSessions(pinnedSessionIds.flatMap((id) => {
    const session = sessionById.get(id);
    return session ? [session] : [];
  }), sessionOrders.pinned);
  const pinnedProjects = pinnedWorkspacePaths.flatMap((path) => {
    const project = projectByPath.get(pathIdentity(path));
    return project ? [project] : [];
  });
  const standaloneConversations = orderedSessions(sessions.filter((session) => session.scope === "standalone"), sessionOrders.standalone);
  const query = search.trim().toLocaleLowerCase();
  const matchingProjects = sortedProjects.filter((project) => `${project.name} ${project.path}`.toLocaleLowerCase().includes(query));
  const matchingSessions = sessions.filter((session) =>
    `${sessionTitle(session)} ${session.lastMessage || ""} ${session.projectPath || ""}`.toLocaleLowerCase().includes(query),
  );

  useEffect(() => {
    setWorkspaceOrder(current => {
      const next = mergeWorkspaceOrder(current, projects.map(project => project.path));
      return next.length === current.length && next.every((path, index) => path === current[index]) ? current : next;
    });
  }, [projects]);

  useEffect(() => { saveWorkspaceOrder(orderStorage, workspaceOrder); }, [orderStorage, workspaceOrder]);
  useEffect(() => { saveSessionOrders(orderStorage, sessionOrders); }, [orderStorage, sessionOrders]);

  const draggedSessionId = sessionReorder.dragging?.id;
  useEffect(() => {
    if (query || (draggedSessionId && !sessions.some(session => session.sessionId === draggedSessionId))) sessionReorder.cancel();
  }, [draggedSessionId, query, sessions, sessionReorder.cancel]);

  useEffect(() => {
    if (!draggingProject) return;
    let frame: number;
    const scroll = () => {
      const drag = projectDragRef.current;
      const navigation = navigationRef.current;
      if (!drag || !navigation) return;
      const rect = navigation.getBoundingClientRect();
      const previous = navigation.scrollTop;
      if (drag.y < rect.top + 32) navigation.scrollTop -= 10;
      else if (drag.y > rect.bottom - 32) navigation.scrollTop += 10;
      if (navigation.scrollTop !== previous) {
        drag.target = projectDropTarget(projectTreeRef.current, drag);
        setDropTarget(current => sameDropTarget(current, drag.target) ? current : drag.target);
      }
      frame = requestAnimationFrame(scroll);
    };
    frame = requestAnimationFrame(scroll);
    return () => cancelAnimationFrame(frame);
  }, [draggingProject]);

  useEffect(() => {
    if (draggingProject && (query || !projects.some(project => pathIdentity(project.path) === draggingProject))) {
      cancelProjectDrag();
    }
  }, [draggingProject, projects, query]);

  useEffect(() => {
    if (!searchRequest) return;
    setSearchOpen(true);
    searchRef.current?.focus();
  }, [searchRequest]);

  useEffect(() => {
    if (searchOpen) searchRef.current?.focus();
  }, [searchOpen]);

  const sessionsByProject = useMemo(() => {
    const grouped = new Map<string, SessionSummary[]>();
    for (const session of sessions) {
      if (session.scope !== "project" || !session.projectPath) continue;
      const identity = pathIdentity(session.projectPath);
      const current = grouped.get(identity) || [];
      current.push(session);
      grouped.set(identity, current);
    }
    return grouped;
  }, [sessions]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 819px)");
    const update = () => setMobileLayout(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    setDraftWorkspace(workspace);
    if (!workspace || conversationScope !== "project") return;
    setExpandedProjects((current) => new Set(current).add(pathIdentity(workspace)));
  }, [conversationScope, workspace]);

  useEffect(() => {
    if (!projectDialogOpen) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && !opening) setProjectDialogOpen(false);
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [opening, projectDialogOpen]);

  const closing = closingProject !== null && samePath(closingProject.path, closingProjectPath);

  useEffect(() => {
    if (!closingProject) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && !closing) setClosingProject(null);
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [closing, closingProject]);

  useEffect(() => {
    // A successful close removes the project from the list, which also ends the
    // confirmation dialog; a failed close keeps it open for a retry.
    if (!closingProject) return;
    if (projects.some((project) => samePath(project.path, closingProject.path))) return;
    setClosingProject(null);
  }, [closingProject, projects]);

  function expandProject(path: string) {
    setExpandedProjects((current) => new Set(current).add(pathIdentity(path)));
  }

  function reorderProject(source: string, target: ProjectDropTarget) {
    const next = moveWorkspace(mergeWorkspaceOrder(workspaceOrder, projects.map(project => project.path)), source, target.path, target.position);
    setWorkspaceOrder(next);
    const reordered = orderedWorkspaces(projects, next);
    const index = reordered.findIndex(project => pathIdentity(project.path) === pathIdentity(source));
    if (index >= 0) setOrderAnnouncement(`${reordered[index].name} 已移到第 ${index + 1} 个位置。`);
  }

  function conversationGroup(group: string): SessionSummary[] {
    if (group === "pinned") return pinnedSessions;
    if (group === "standalone") return standaloneConversations;
    return orderedSessions(sessionsByProject.get(group.slice("project:".length)) || [], sessionOrders[group]);
  }

  function reorderConversation(source: string, target: SessionDropTarget) {
    const items = conversationGroup(target.group);
    if (!items.some(session => session.sessionId === source) || !items.some(session => session.sessionId === target.id)) return;
    const next = moveSession(mergeSessionOrder(sessionOrders[target.group] || [], items.map(session => session.sessionId)), source, target.id, target.position);
    setSessionOrders(current => ({ ...current, [target.group]: next }));
    const reordered = orderedSessions(items, next);
    const index = reordered.findIndex(session => session.sessionId === source);
    if (index >= 0) setOrderAnnouncement(`${sessionTitle(reordered[index])} 已移到第 ${index + 1} 个位置。`);
  }

  function projectRenameAction(project: RecentWorkspace) {
    return <button className="rail-item-action" onClick={() => setRenaming({ kind: "workspace", target: project.path, name: project.name })}
      aria-label={`重命名项目 ${project.name}`} title="重命名"><Pencil size={14} /></button>;
  }

  function beginProjectDrag(event: ReactPointerEvent<HTMLButtonElement>, path: string) {
    if (event.button !== 0 || !event.isPrimary) return;
    if (event.pointerType === "touch" && !event.currentTarget.classList.contains("project-reorder-handle")) return;
    projectDragRef.current = { path, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
      x: event.clientX, y: event.clientY, active: false, target: null };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveProjectDrag(event: ReactPointerEvent<HTMLButtonElement>) {
    const drag = projectDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    drag.x = event.clientX;
    drag.y = event.clientY;
    if (!drag.active && Math.hypot(drag.x - drag.startX, drag.y - drag.startY) < 6) return;
    event.preventDefault();
    drag.active = true;
    setDraggingProject(drag.path);
    drag.target = projectDropTarget(projectTreeRef.current, drag);
    setDropTarget(current => sameDropTarget(current, drag.target) ? current : drag.target);
  }

  function cancelProjectDrag() {
    projectDragRef.current = null;
    setDraggingProject(null);
    setDropTarget(null);
  }

  function finishProjectDrag(event: ReactPointerEvent<HTMLButtonElement>) {
    const drag = projectDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (drag.active) {
      suppressProjectClickRef.current = true;
      window.setTimeout(() => { suppressProjectClickRef.current = false; }, 0);
      if (drag.target) reorderProject(drag.path, drag.target);
    }
    cancelProjectDrag();
  }

  function toggleProject(path: string) {
    const identity = pathIdentity(path);
    setExpandedProjects((current) => {
      const next = new Set(current);
      if (next.has(identity)) next.delete(identity);
      else next.add(identity);
      return next;
    });
  }

  async function startProjectConversation(path: string) {
    expandProject(path);
    try {
      await onNewProjectTask(path);
      onCloseMobile();
    } catch {
      // The app-level toast contains the backend's actionable error.
    }
  }

  async function applyWorkspace(path = draftWorkspace) {
    const nextWorkspace = path.trim();
    if (!nextWorkspace) return;
    setOpening("path");
    try {
      await onChangeWorkspace(nextWorkspace);
      expandProject(nextWorkspace);
      setProjectDialogOpen(false);
      onCloseMobile();
    } catch {
      // The app-level toast contains the backend's actionable error.
    } finally {
      setOpening(null);
    }
  }

  async function pickWorkspace() {
    setOpening("picker");
    try {
      const changed = await onPickWorkspace();
      if (changed) {
        setProjectDialogOpen(false);
        onCloseMobile();
      }
    } catch {
      // The app-level toast contains the backend's actionable error.
    } finally {
      setOpening(null);
    }
  }

  function openProjectDialog() {
    setDraftWorkspace(workspace);
    setProjectDialogOpen(true);
  }

  function confirmCloseProject() {
    if (!closingProject || closing) return;
    onCloseProject(closingProject.path);
  }
  function startStandaloneConversation() {
    onNewStandaloneTask();
    onCloseMobile();
  }

  function selectConversation(session: SessionSummary) {
    onSelectSession(session.sessionId);
    onCloseMobile();
  }

  function closeSearch() {
    setSearch("");
    setSearchOpen(false);
    searchTriggerRef.current?.focus();
  }

  function renderConversation(session: SessionSummary, nested = false, group?: string) {
    const pinned = pinnedSessionSet.has(session.sessionId);
    const dragging = sessionReorder.dragging?.id === session.sessionId && sessionReorder.dragging.group === group;
    const target = sessionReorder.target?.id === session.sessionId && sessionReorder.target.group === group ? sessionReorder.target : null;
    const pointerHandlers = group ? {
      onPointerDown: (event: ReactPointerEvent<HTMLButtonElement>) => sessionReorder.begin(event, session.sessionId, group),
      onPointerMove: sessionReorder.move,
      onPointerUp: sessionReorder.finish,
      onPointerCancel: sessionReorder.cancel,
      onLostPointerCapture: sessionReorder.cancel,
    } : {};
    return (
      <div
        className={`rail-item conversation-item ${nested ? "is-nested" : ""} ${selectedSessionId === session.sessionId ? "is-active" : ""} ${dragging ? "is-dragging" : ""} ${target ? `is-drop-${target.position}` : ""} ${group ? "is-sortable" : ""}`}
        key={`${group || "search"}-${session.sessionId}`}
        data-session-id={session.sessionId} data-session-group={group}
        onClickCapture={sessionReorder.suppressClick}
      >
        {group && <button className="session-reorder-handle" aria-label={`拖动排序对话 ${sessionTitle(session)}`}
          title="拖动调整位置，也可用 ↑ / ↓ 移动" {...pointerHandlers} onClick={() => selectConversation(session)}
          onKeyDown={event => {
            if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
            event.preventDefault();
            const items = conversationGroup(group);
            const index = items.findIndex(item => item.sessionId === session.sessionId);
            const neighbor = items[index + (event.key === "ArrowUp" ? -1 : 1)];
            if (neighbor) reorderConversation(session.sessionId, { id: neighbor.sessionId, group, position: event.key === "ArrowUp" ? "before" : "after" });
          }}>
          <MessageSquare size={nested ? 14 : 15} className="session-chat-mark" />
          <GripVertical size={16} className="session-grip-mark" />
        </button>}
        <button className="rail-item-main" {...pointerHandlers} onClick={() => selectConversation(session)} title={sessionTitle(session)} aria-current={selectedSessionId === session.sessionId ? "page" : undefined}>
          {!group && <MessageSquare size={nested ? 14 : 15} />}
          <span>
            <strong>{sessionTitle(session)}</strong>
            <small className="session-meta">
              <span className="session-context">
                {!nested && session.scope === "project" ? `${workspaceName(session.projectPath || session.workspace)} · ` : ""}
                {relativeTime(session.updatedAt)}
              </span>
              <SessionState session={session} />
            </small>
          </span>
        </button>
        <div className="rail-item-actions">
          <button className="rail-item-action" onClick={() => setRenaming({ kind: "session", target: session.sessionId, name: sessionTitle(session) })}
            aria-label={`重命名对话 ${sessionTitle(session)}`} title="重命名"><Pencil size={14} /></button>
          {!nested && (
            <button
              className={`rail-item-action ${pinned ? "is-pinned" : ""}`}
              onClick={() => onTogglePin("session", session.sessionId, !pinned)}
              aria-label={`${pinned ? "取消置顶" : "置顶"}对话 ${sessionTitle(session)}`}
              title={pinned ? "取消置顶" : "置顶"}
            >
              {pinned ? <PinOff size={14} /> : <Pin size={14} />}
            </button>
          )}
          <button
            className="rail-item-action delete-action"
            onClick={() => onArchiveSession(session.sessionId)}
            disabled={archivingSessionId !== null}
            aria-label={`移入回收站 ${sessionTitle(session)}`}
            title={archivingSessionId === session.sessionId ? "正在移入回收站…" : "移入回收站"}
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    );
  }

  return (
    <aside
      className={`app-rail ${mobileOpen ? "is-open" : ""} ${draggingProject ? "is-project-dragging" : ""} ${sessionReorder.dragging ? "is-session-dragging" : ""}`}
      aria-label="项目和对话"
      aria-hidden={mobileLayout ? !mobileOpen : collapsed}
      inert={mobileLayout ? !mobileOpen : collapsed}
    >
      <div className="rail-brand">
        <div className="rail-wordmark">
          <strong>MiniCode</strong>
        </div>
        <button ref={searchTriggerRef} className="icon-button rail-search-toggle" onClick={() => searchOpen ? closeSearch() : setSearchOpen(true)} aria-label="搜索项目和对话" title="搜索项目和对话 (Ctrl K)" aria-expanded={searchOpen}>
          <Search size={18} />
        </button>
        <button className="icon-button desktop-only" onClick={onToggleCollapse} aria-label="收起侧栏" title="收起侧栏">
          <PanelLeftClose size={18} />
        </button>
        <button className="icon-button rail-close" onClick={onCloseMobile} aria-label="关闭侧栏">
          <X size={18} />
        </button>
      </div>

      <button className="new-task-button" onClick={startStandaloneConversation}>
        <SquarePen size={18} />
        <span>新对话</span>
        <kbd>Ctrl N</kbd>
      </button>

      {searchOpen && <div className="rail-search">
        <Search size={15} aria-hidden="true" />
        <input ref={searchRef} value={search} onChange={(event) => setSearch(event.target.value)}
          aria-label="搜索项目和对话" placeholder="搜索项目或对话…" autoComplete="off"
          onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); closeSearch(); } }} />
        <button className="icon-button" onClick={closeSearch} aria-label="关闭搜索" title="关闭搜索 (Esc)"><X size={15} /></button>
      </div>}

      <div className="rail-navigation" ref={navigationRef}>
        {query ? <section className="rail-group search-results" aria-label="搜索结果">
          <div className="rail-section-heading" aria-live="polite">找到 {matchingProjects.length + matchingSessions.length} 项结果</div>
          {matchingProjects.map((project) => <div className="rail-item project-item" key={project.path}>
            <button className="rail-item-main" title={project.path} onClick={() => void startProjectConversation(project.path)}>
              <Folder size={16} /><span><strong>{project.name}</strong><small>项目</small></span>
            </button>
            <div className="project-item-actions">{projectRenameAction(project)}</div>
          </div>)}
          {matchingSessions.map((session) => renderConversation(session))}
          {!matchingProjects.length && !matchingSessions.length && <p className="rail-empty">没有找到匹配内容，试试其他关键词。</p>}
        </section> : <>
        {(pinnedProjects.length > 0 || pinnedSessions.length > 0) && (
          <section className="rail-group" aria-labelledby="pinned-heading">
            <div className="rail-section-heading" id="pinned-heading"><span>置顶</span></div>
            <div className="rail-items">
              {pinnedProjects.map((project) => (
                <div className="rail-item project-item" key={`pinned-${project.path}`}>
                  <button className="rail-item-main" onClick={() => void startProjectConversation(project.path)}>
                    <Folder size={16} />
                    <span><strong>{project.name}</strong><small>项目</small></span>
                  </button>
                  <div className="project-item-actions">
                    {projectRenameAction(project)}
                    <button className="rail-item-action is-pinned" onClick={() => onTogglePin("workspace", project.path, false)} aria-label={`取消置顶项目 ${project.name}`} title="取消置顶">
                      <PinOff size={14} />
                    </button>
                    <button className="rail-item-action delete-action" onClick={() => setClosingProject(project)} aria-label={`关闭项目 ${project.name}`} title="关闭项目">
                      <X size={14} />
                    </button>
                  </div>
                </div>
              ))}
              {pinnedSessions.map((session) => renderConversation(session, false, "pinned"))}
            </div>
          </section>
        )}

        <section className="rail-group" aria-labelledby="projects-heading">
          <div className="rail-section-heading" id="projects-heading">
            <span>项目</span>
            <button onClick={openProjectDialog} aria-label="打开项目" title="打开项目"><Plus size={15} /></button>
          </div>
          <div className="project-tree" ref={projectTreeRef}>
            {projects.length === 0 ? (
              <p className="rail-empty">还没有项目。点击加号选择一个文件夹。</p>
            ) : sortedProjects.map((project) => {
              const identity = pathIdentity(project.path);
              const active = conversationScope === "project" && samePath(project.path, workspace);
              const pinned = pinnedWorkspaceSet.has(identity);
              const expanded = expandedProjects.has(identity);
              const group = `project:${identity}`;
              const projectSessions = conversationGroup(group);
              return (
                <div className={`project-node ${active ? "is-active" : ""} ${draggingProject === identity ? "is-dragging" : ""} ${dropTarget?.path === identity ? `is-drop-${dropTarget.position}` : ""}`}
                  key={identity} data-workspace-path={identity}>
                  <div className={`rail-item project-item ${active ? "is-active" : ""}`} onClickCapture={event => {
                    if (!suppressProjectClickRef.current) return;
                    suppressProjectClickRef.current = false;
                    event.preventDefault();
                    event.stopPropagation();
                  }}>
                    <button className="project-disclosure" onClick={() => toggleProject(project.path)} aria-expanded={expanded} aria-label={`${expanded ? "收起" : "展开"}项目 ${project.name}`}>
                      {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                    <button className="project-reorder-handle" aria-label={`拖动排序 ${project.name}`} title="拖动调整位置，也可用 ↑ / ↓ 移动"
                      onPointerDown={event => beginProjectDrag(event, identity)} onPointerMove={moveProjectDrag}
                      onPointerUp={finishProjectDrag} onPointerCancel={cancelProjectDrag} onLostPointerCapture={cancelProjectDrag}
                      onClick={() => { expandProject(project.path); if (!active) void startProjectConversation(project.path); }}
                      onKeyDown={event => {
                        if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
                        event.preventDefault();
                        const index = sortedProjects.findIndex(item => pathIdentity(item.path) === identity);
                        const neighbor = sortedProjects[index + (event.key === "ArrowUp" ? -1 : 1)];
                        if (neighbor) reorderProject(identity, { path: neighbor.path, position: event.key === "ArrowUp" ? "before" : "after" });
                      }}>
                      <Folder size={16} className="project-folder-mark" />
                      <GripVertical size={16} className="project-grip-mark" />
                    </button>
                    <button
                      className="rail-item-main"
                      title={`${project.path} · ${projectSessions.length} 个对话`}
                      onPointerDown={event => beginProjectDrag(event, identity)} onPointerMove={moveProjectDrag}
                      onPointerUp={finishProjectDrag} onPointerCancel={cancelProjectDrag} onLostPointerCapture={cancelProjectDrag}
                      onClick={() => {
                        expandProject(project.path);
                        if (!active) void startProjectConversation(project.path);
                      }}
                    >
                      <span>
                        <strong>{project.name}</strong>
                      </span>
                      {active && <Check size={14} className="current-check" aria-label="当前项目" />}
                    </button>
                    <div className="project-item-actions">
                      {projectRenameAction(project)}
                      <button className="rail-item-action project-new-action" onClick={() => void startProjectConversation(project.path)} aria-label={`在 ${project.name} 中新建对话`} title="新建项目对话"><Plus size={14} /></button>
                      <button className={`rail-item-action ${pinned ? "is-pinned" : ""}`} onClick={() => onTogglePin("workspace", project.path, !pinned)} aria-label={`${pinned ? "取消置顶" : "置顶"}项目 ${project.name}`} title={pinned ? "取消置顶" : "置顶"}>{pinned ? <PinOff size={14} /> : <Pin size={14} />}</button>
                      <button className="rail-item-action delete-action" onClick={() => setClosingProject(project)} aria-label={`关闭项目 ${project.name}`} title="关闭项目"><X size={14} /></button>
                    </div>
                  </div>
                  {expanded && (
                    <div className="project-conversations">
                      {projectSessions.length === 0 ? (
                        active && <button className="project-empty-conversation" onClick={() => void startProjectConversation(project.path)}>
                          <Plus size={13} />在这个项目中新建对话
                        </button>
                      ) : (
                        projectSessions.map((session) => renderConversation(session, true, group))
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          <span className="sr-only" role="status" aria-live="polite">{orderAnnouncement}</span>
        </section>

        <section className="rail-group conversations-group" aria-labelledby="conversations-heading">
          <div className="rail-section-heading" id="conversations-heading">
            <span>对话</span>
            {standaloneConversations.some((session) => ["queued", "running", "stopping"].includes(session.status)) && <span className="running-label"><i />有任务运行中</span>}
          </div>
          <div className="rail-items">
            {standaloneConversations.length === 0 ? (
              <p className="rail-empty">还没有对话，开始聊聊吧。</p>
            ) : standaloneConversations.map((session) => renderConversation(session, false, "standalone"))}
          </div>
        </section>
        </>}
      </div>

      <div className="rail-footer">
        <button onClick={() => { onOpenTrash(); onCloseMobile(); }} className={`rail-action ${activePage === "trash" ? "is-active" : ""}`} aria-current={activePage === "trash" ? "page" : undefined}><Trash2 size={17} /><span>回收站</span></button>
        <button onClick={onOpenSettings} className={`rail-action rail-settings-trigger ${activePage === "settings" || activePage === "extensions" ? "is-active" : ""}`}
          aria-label="设置" aria-haspopup="dialog" aria-expanded={activePage === "settings" || activePage === "extensions"} title="设置与扩展">
          <Settings className="rail-settings-avatar" size={18} />
          <span className="rail-settings-copy"><strong>设置</strong><small>模型、运行与扩展</small></span>
          <ChevronRight className="rail-settings-chevron" size={15} />
        </button>
      </div>

      {renaming && <RailRenameDialog key={`${renaming.kind}-${renaming.target}`} item={renaming} onRename={onRename} onClose={() => setRenaming(null)} />}

      {projectDialogOpen && createPortal(
        <div className="project-dialog-backdrop" onPointerDown={(event) => {
          if (event.target === event.currentTarget && !opening) setProjectDialogOpen(false);
        }}>
          <section className="project-dialog" role="dialog" aria-modal="true" aria-labelledby="project-dialog-title">
            <header>
              <div><strong id="project-dialog-title">打开项目</strong><span>选择 MiniCode 可以读取和修改的项目文件夹。</span></div>
              <button className="icon-button" onClick={() => setProjectDialogOpen(false)} disabled={Boolean(opening)} aria-label="关闭打开项目窗口"><X size={18} /></button>
            </header>

            <button className="workspace-picker-button" onClick={() => void pickWorkspace()} disabled={Boolean(opening)}>
              <FolderOpen size={19} />
              <span>
                <strong>{opening === "picker" ? "系统选择窗口已打开" : "选择文件夹…"}</strong>
                <small>{opening === "picker" ? "请在弹出的窗口中完成选择" : "打开 Windows 文件夹选择器"}</small>
              </span>
            </button>

            {projects.length > 0 && (
              <div className="recent-workspaces">
                <span className="workspace-menu-label">最近项目</span>
                {projects.slice(0, 5).map((project) => {
                  const active = conversationScope === "project" && samePath(project.path, workspace);
                  return (
                    <button key={project.path} className={`recent-workspace-button ${active ? "is-active" : ""}`} onClick={() => void applyWorkspace(project.path)} disabled={Boolean(opening)} aria-current={active ? "true" : undefined}>
                      <span><strong>{project.name}</strong><small title={project.path}>{project.path}</small></span>
                      {active && <Check size={15} aria-label="当前项目" />}
                    </button>
                  );
                })}
              </div>
            )}

            <div className="workspace-manual">
              <label htmlFor="workspace-path">或者输入文件夹路径</label>
              <div>
                <input id="workspace-path" value={draftWorkspace} onChange={(event) => setDraftWorkspace(event.target.value)} onKeyDown={(event) => {
                  if (event.key === "Enter") void applyWorkspace();
                }} spellCheck={false} disabled={Boolean(opening)} />
                <button className="button button-primary" onClick={() => void applyWorkspace()} disabled={Boolean(opening) || !draftWorkspace.trim()}>{opening === "path" ? "打开中" : "打开"}</button>
              </div>
            </div>
          </section>
        </div>,
        document.body,
      )}

      {closingProject && createPortal(
        <div className="project-dialog-backdrop" onPointerDown={(event) => {
          if (event.target === event.currentTarget && !closing) setClosingProject(null);
        }}>
          <section className="project-dialog" role="dialog" aria-modal="true" aria-labelledby="close-project-title">
            <header>
              <div>
                <strong id="close-project-title">关闭项目</strong>
                <span>只从左侧列表移除“{closingProject.name}”，不会删除对话、文件或记忆。</span>
              </div>
              <button className="icon-button" onClick={() => setClosingProject(null)} disabled={closing} aria-label="关闭关闭项目窗口"><X size={18} /></button>
            </header>

            <p className="project-close-summary" title={closingProject.path}>
              {closingProject.path}
            </p>
            <p className="project-close-note">
              该项目的对话仍保留在“对话”列表中并可继续打开；需要再次使用时，通过“打开项目”选择同一文件夹即可恢复。
            </p>

            <div className="project-close-actions">
              <button className="button" onClick={() => setClosingProject(null)} disabled={closing}>取消</button>
              <button className="button button-danger" onClick={confirmCloseProject} disabled={closing}>
                {closing ? "关闭中…" : "关闭项目"}
              </button>
            </div>
          </section>
        </div>,
        document.body,
      )}
    </aside>
  );
}
