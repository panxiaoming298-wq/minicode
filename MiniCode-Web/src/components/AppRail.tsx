import {
  Check,
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  GitBranch,
  MessageSquare,
  PanelRight,
  Pin,
  PinOff,
  Plus,
  Settings,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

import type {
  ConversationScope,
  RecentWorkspace,
  SessionSummary,
  WorkspaceStatus,
} from "../types";

interface AppRailProps {
  workspace: string;
  workspaceStatus: WorkspaceStatus | null;
  projects: RecentWorkspace[];
  sessions: SessionSummary[];
  pinnedSessionIds: string[];
  pinnedWorkspacePaths: string[];
  selectedSessionId: string | null;
  conversationScope: ConversationScope;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onNewStandaloneTask: () => void;
  onNewProjectTask: (workspace: string) => Promise<void>;
  onSelectSession: (sessionId: string) => void;
  onDeleteSession: (sessionId: string) => void;
  onChangeWorkspace: (workspace: string) => Promise<void>;
  onPickWorkspace: () => Promise<boolean>;
  onTogglePin: (kind: "workspace" | "session", target: string, pinned: boolean) => void;
  onOpenSettings: () => void;
  onToggleInspector: () => void;
}

function workspaceName(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts.at(-1) || path;
}

function pathIdentity(path: string): string {
  return path.replace(/\//g, "\\").replace(/[\\]+$/, "").toLocaleLowerCase();
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
  return session.firstMessage || session.lastMessage || "新对话";
}

function sessionState(session: SessionSummary): string {
  return {
    queued: "排队中",
    running: "运行中",
    stopping: "停止中",
    stopped: "已停止，可继续",
    failed: "失败，可重试",
  }[session.status] || "";
}

export function AppRail({
  workspace,
  workspaceStatus,
  projects,
  sessions,
  pinnedSessionIds,
  pinnedWorkspacePaths,
  selectedSessionId,
  conversationScope,
  mobileOpen,
  onCloseMobile,
  onNewStandaloneTask,
  onNewProjectTask,
  onSelectSession,
  onDeleteSession,
  onChangeWorkspace,
  onPickWorkspace,
  onTogglePin,
  onOpenSettings,
  onToggleInspector,
}: AppRailProps) {
  const [projectDialogOpen, setProjectDialogOpen] = useState(false);
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
  const pinnedSessions = pinnedSessionIds.flatMap((id) => {
    const session = sessionById.get(id);
    return session ? [session] : [];
  });
  const pinnedProjects = pinnedWorkspacePaths.flatMap((path) => {
    const project = projectByPath.get(pathIdentity(path));
    return project ? [project] : [];
  });
  const standaloneConversations = sessions.filter((session) => session.scope === "standalone");

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

  function expandProject(path: string) {
    setExpandedProjects((current) => new Set(current).add(pathIdentity(path)));
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

  function startStandaloneConversation() {
    onNewStandaloneTask();
    onCloseMobile();
  }

  function selectConversation(session: SessionSummary) {
    onSelectSession(session.sessionId);
    onCloseMobile();
  }

  function renderConversation(session: SessionSummary, nested = false) {
    const pinned = pinnedSessionSet.has(session.sessionId);
    const state = sessionState(session);
    return (
      <div
        className={`rail-item conversation-item ${nested ? "is-nested" : ""} ${selectedSessionId === session.sessionId ? "is-active" : ""}`}
        key={`${nested ? "nested" : "root"}-${session.sessionId}`}
      >
        <button className="rail-item-main" onClick={() => selectConversation(session)}>
          <MessageSquare size={nested ? 14 : 15} />
          <span>
            <strong>{sessionTitle(session)}</strong>
            <small className="session-meta">
              <span className="session-context">
                {session.scope === "standalone" ? "独立对话" : workspaceName(session.projectPath || session.workspace)}
                {` · ${relativeTime(session.updatedAt)}`}
              </span>
              {state && <span className={`session-state session-state-${session.status}`}><i />{state}</span>}
            </small>
          </span>
        </button>
        <div className="rail-item-actions">
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
            onClick={() => onDeleteSession(session.sessionId)}
            aria-label={`删除对话 ${sessionTitle(session)}`}
            title="删除"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    );
  }

  return (
    <aside
      className={`app-rail ${mobileOpen ? "is-open" : ""}`}
      aria-label="项目和对话"
      aria-hidden={mobileLayout && !mobileOpen}
      inert={mobileLayout && !mobileOpen}
    >
      <div className="rail-brand">
        <div className="brand-mark" aria-hidden="true">M</div>
        <div>
          <strong>MiniCode</strong>
          <span>Local workbench</span>
        </div>
        <button className="icon-button rail-close" onClick={onCloseMobile} aria-label="关闭侧栏">
          <X size={18} />
        </button>
      </div>

      <button className="new-task-button" onClick={startStandaloneConversation}>
        <MessageSquare size={17} />
        <span>新对话</span>
        <kbd>Ctrl N</kbd>
      </button>

      <div className="rail-navigation">
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
                  <button className="rail-item-action is-pinned" onClick={() => onTogglePin("workspace", project.path, false)} aria-label={`取消置顶项目 ${project.name}`} title="取消置顶">
                    <PinOff size={14} />
                  </button>
                </div>
              ))}
              {pinnedSessions.map((session) => renderConversation(session))}
            </div>
          </section>
        )}

        <section className="rail-group" aria-labelledby="projects-heading">
          <div className="rail-section-heading" id="projects-heading">
            <span>项目</span>
            <button onClick={openProjectDialog} aria-label="打开项目" title="打开项目"><Plus size={15} /></button>
          </div>
          <div className="project-tree">
            {projects.length === 0 ? (
              <p className="rail-empty">还没有项目。点击加号选择一个文件夹。</p>
            ) : projects.map((project) => {
              const identity = pathIdentity(project.path);
              const active = conversationScope === "project" && samePath(project.path, workspace);
              const pinned = pinnedWorkspaceSet.has(identity);
              const expanded = expandedProjects.has(identity);
              const projectSessions = sessionsByProject.get(identity) || [];
              return (
                <div className={`project-node ${active ? "is-active" : ""}`} key={project.path}>
                  <div className={`rail-item project-item ${active ? "is-active" : ""}`}>
                    <button className="project-disclosure" onClick={() => toggleProject(project.path)} aria-label={`${expanded ? "收起" : "展开"}项目 ${project.name}`}>
                      {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                    <button
                      className="rail-item-main"
                      onClick={() => {
                        expandProject(project.path);
                        if (!active) void startProjectConversation(project.path);
                      }}
                    >
                      <Folder size={16} />
                      <span>
                        <strong>{project.name}</strong>
                        <small title={project.path}>{projectSessions.length} 个对话</small>
                      </span>
                      {active && <Check size={14} className="current-check" aria-label="当前项目" />}
                    </button>
                    <div className="project-item-actions">
                      <button className="rail-item-action project-new-action" onClick={() => void startProjectConversation(project.path)} aria-label={`在 ${project.name} 中新建对话`} title="新建项目对话"><Plus size={14} /></button>
                      <button className={`rail-item-action ${pinned ? "is-pinned" : ""}`} onClick={() => onTogglePin("workspace", project.path, !pinned)} aria-label={`${pinned ? "取消置顶" : "置顶"}项目 ${project.name}`} title={pinned ? "取消置顶" : "置顶"}><Pin size={14} /></button>
                    </div>
                  </div>
                  {expanded && (
                    <div className="project-conversations">
                      {projectSessions.length === 0 ? (
                        <button className="project-empty-conversation" onClick={() => void startProjectConversation(project.path)}>
                          <Plus size={13} />在这个项目中新建对话
                        </button>
                      ) : (
                        projectSessions.map((session) => renderConversation(session, true))
                      )}
                      {active && workspaceStatus?.isGitRepository && (
                        <div className="active-project-meta" title={workspaceStatus.summary}>
                          <GitBranch size={12} /><span>{workspaceStatus.branch || "Git"}</span><small>{workspaceStatus.summary}</small>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>

        <section className="rail-group conversations-group" aria-labelledby="conversations-heading">
          <div className="rail-section-heading" id="conversations-heading">
            <span>对话</span>
            {standaloneConversations.some((session) => ["queued", "running", "stopping"].includes(session.status)) && <span className="running-label"><i />有任务运行中</span>}
          </div>
          <div className="rail-items">
            {standaloneConversations.length === 0 ? (
              <p className="rail-empty">这里只显示不绑定项目的对话。点击上方“新对话”开始。</p>
            ) : standaloneConversations.map((session) => renderConversation(session))}
          </div>
        </section>
      </div>

      <div className="rail-footer">
        <button onClick={onToggleInspector} className="rail-action inspector-action"><PanelRight size={17} /><span>文件与检查器</span></button>
        <button onClick={onOpenSettings} className="rail-action"><Settings size={17} /><span>模型与 API</span></button>
      </div>

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
    </aside>
  );
}
