import { CircleAlert, LoaderCircle, PanelRight, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";

import { api, parseRunEvent } from "./api/client";
import { AppRail } from "./components/AppRail";
import { Composer } from "./components/Composer";
import { Inspector } from "./components/Inspector";
import { RunStream } from "./components/RunStream";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { historyToTimeline, reduceRunEvent } from "./lib/timeline";
import type {
  ConversationScope,
  RunEvent,
  RunInfo,
  RunStatus,
  NavigationPayload,
  SessionSummary,
  SettingsView,
  TimelineItem,
  WorkspaceStatus,
} from "./types";

const EMPTY_NAVIGATION: NavigationPayload = {
  projects: [],
  sessions: [],
  pinnedSessionIds: [],
  pinnedWorkspacePaths: [],
};

const INSPECTOR_WIDTH_STORAGE_KEY = "minicode.inspector.width";
const RAIL_WIDTH = 292;
const INSPECTOR_MIN_WIDTH = 360;
const INSPECTOR_MAX_WIDTH = 760;
const CENTER_MIN_WIDTH = 480;

function inspectorWidthBounds() {
  return {
    min: INSPECTOR_MIN_WIDTH,
    max: Math.max(
      INSPECTOR_MIN_WIDTH,
      Math.min(INSPECTOR_MAX_WIDTH, window.innerWidth - RAIL_WIDTH - CENTER_MIN_WIDTH),
    ),
  };
}

function clampInspectorWidth(width: number): number {
  const { min, max } = inspectorWidthBounds();
  return Math.round(Math.min(max, Math.max(min, width)));
}

function defaultInspectorWidth(): number {
  return clampInspectorWidth(Math.min(INSPECTOR_MAX_WIDTH, Math.max(440, window.innerWidth * 0.4)));
}

function initialInspectorWidth(): number {
  try {
    const savedWidth = Number(window.localStorage.getItem(INSPECTOR_WIDTH_STORAGE_KEY));
    if (Number.isFinite(savedWidth) && savedWidth > 0) return clampInspectorWidth(savedWidth);
  } catch {
    // Browser storage can be unavailable in hardened/private contexts.
  }
  return defaultInspectorWidth();
}

function persistInspectorWidth(width: number) {
  try {
    window.localStorage.setItem(INSPECTOR_WIDTH_STORAGE_KEY, String(width));
  } catch {
    // Resizing remains fully functional even when persistence is unavailable.
  }
}

function isActive(status: RunStatus): boolean {
  return status === "queued" || status === "running" || status === "stopping";
}

function terminalStatus(event: RunEvent): RunStatus | null {
  if (event.kind !== "status") return null;
  if (event.status === "completed" || event.status === "failed" || event.status === "stopped") {
    return event.status;
  }
  return null;
}

function workspaceName(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts.at(-1) || path;
}

export default function App() {
  const [booting, setBooting] = useState(true);
  const [workspace, setWorkspace] = useState("");
  const [workspaceStatus, setWorkspaceStatus] = useState<WorkspaceStatus | null>(null);
  const [conversationScope, setConversationScope] = useState<ConversationScope>("standalone");
  const [navigation, setNavigation] = useState<NavigationPayload>(EMPTY_NAVIGATION);
  const [settings, setSettings] = useState<SettingsView | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [run, setRun] = useState<RunInfo | null>(null);
  const [runStatus, setRunStatus] = useState<RunStatus>("idle");
  const [prompt, setPrompt] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [railOpen, setRailOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(() => window.innerWidth >= 1180);
  const [inspectorWidth, setInspectorWidth] = useState(initialInspectorWidth);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [diff, setDiff] = useState("");
  const [error, setError] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);
  const seenEventsRef = useRef(new Set<number>());
  const inspectorWidthRef = useRef(inspectorWidth);
  const inspectorResizeRef = useRef<{
    pointerId: number;
    startX: number;
    startWidth: number;
  } | null>(null);

  useEffect(() => {
    inspectorWidthRef.current = inspectorWidth;
  }, [inspectorWidth]);

  useEffect(() => {
    function clampToViewport() {
      setInspectorWidth((current) => clampInspectorWidth(current));
    }
    window.addEventListener("resize", clampToViewport);
    return () => {
      window.removeEventListener("resize", clampToViewport);
      document.body.classList.remove("is-resizing-inspector");
    };
  }, []);

  const refreshWorkspace = useCallback(async (path: string) => {
    const [status, nextNavigation] = await Promise.all([
      api.workspaceStatus(path),
      api.getNavigation(),
    ]);
    setWorkspaceStatus(status);
    setNavigation(nextNavigation);
    setSelectedFile((current) => {
      if (current && status.files.some((file) => file.path === current)) return current;
      return status.files[0]?.path || null;
    });
    return status;
  }, []);

  useEffect(() => {
    let cancelled = false;
    api.bootstrap()
      .then(async (payload) => {
        if (cancelled) return;
        setWorkspace(payload.workspace);
        setSettings(payload.settings);
        setWorkspaceStatus(payload.workspaceStatus);
        const nextNavigation = await api.getNavigation();
        if (!cancelled) setNavigation(nextNavigation);
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "无法连接 MiniCode 本机服务。");
      })
      .finally(() => {
        if (!cancelled) setBooting(false);
      });
    return () => {
      cancelled = true;
      sourceRef.current?.close();
    };
  }, []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        if (!isActive(runStatus)) newStandaloneTask();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [runStatus]);

  useEffect(() => {
    if (conversationScope === "standalone" || !workspace || !selectedFile) {
      setDiff("");
      return;
    }
    api.workspaceDiff(workspace, selectedFile)
      .then((result) => setDiff(result.diff))
      .catch(() => setDiff(""));
  }, [conversationScope, workspace, selectedFile, workspaceStatus]);

  function resetTask(scope: ConversationScope) {
    if (isActive(runStatus)) {
      setError("请先停止当前任务。");
      return;
    }
    setConversationScope(scope);
    setSelectedSessionId(null);
    setTimeline([]);
    setEvents([]);
    setRun(null);
    setRunStatus("idle");
    setPrompt("");
  }

  function newStandaloneTask() {
    resetTask("standalone");
  }

  async function selectSession(sessionId: string) {
    if (isActive(runStatus)) {
      setError("任务运行中，暂时不能切换会话。");
      return;
    }
    setError(null);
    try {
      const summary = navigation.sessions.find((item) => item.sessionId === sessionId);
      if (!summary) throw new Error("会话不存在或已被删除。");
      if (summary.scope === "project" && summary.projectPath && summary.projectPath !== workspace) {
        const opened = await api.openWorkspace(summary.projectPath);
        const nextNavigation = await api.getNavigation();
        setWorkspace(opened.workspace);
        setWorkspaceStatus(opened.workspaceStatus);
        setNavigation(nextNavigation);
        setSelectedFile(opened.workspaceStatus.files[0]?.path || null);
        setDiff("");
      }
      const result = await api.getSession(sessionId);
      setConversationScope(summary.scope);
      setSelectedSessionId(sessionId);
      setTimeline(historyToTimeline(result.messages));
      setEvents([]);
      setRun(null);
      setRunStatus("idle");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取会话。");
    }
  }

  async function deleteSelectedSession(sessionId: string) {
    const session = navigation.sessions.find((item) => item.sessionId === sessionId);
    if (!window.confirm(`删除任务“${session?.firstMessage || "新任务"}”？此操作不可撤销。`)) return;
    try {
      await api.deleteSession(sessionId);
      if (selectedSessionId === sessionId) resetTask(session?.scope || conversationScope);
      setNavigation(await api.getNavigation());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除失败。");
    }
  }

  async function changeWorkspace(path: string) {
    if (isActive(runStatus)) throw new Error("请先停止当前任务。");
    setError(null);
    try {
      const result = await api.openWorkspace(path);
      setWorkspace(result.workspace);
      setWorkspaceStatus(result.workspaceStatus);
      setConversationScope("project");
      setNavigation(await api.getNavigation());
      setSelectedFile(result.workspaceStatus.files[0]?.path || null);
      setDiff("");
      setSelectedSessionId(null);
      setTimeline([]);
      setEvents([]);
      setRun(null);
      setRunStatus("idle");
      setPrompt("");
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "无法打开工作区。";
      setError(message);
      throw reason;
    }
  }

  async function pickWorkspace() {
    if (isActive(runStatus)) throw new Error("请先停止当前任务。");
    setError(null);
    try {
      const result = await api.pickWorkspace(workspace);
      if (result.cancelled) return false;
      await changeWorkspace(result.workspace);
      return true;
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "无法打开文件夹选择器。";
      setError(message);
      throw reason;
    }
  }

  async function togglePin(kind: "workspace" | "session", target: string, pinned: boolean) {
    try {
      setNavigation(await api.setNavigationPin({ kind, target, pinned }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法更新置顶状态。");
    }
  }

  function connectRun(nextRun: RunInfo) {
    sourceRef.current?.close();
    seenEventsRef.current = new Set();
    const source = api.eventSource(nextRun.runId);
    sourceRef.current = source;
    source.onmessage = (message) => {
      const event = parseRunEvent(message);
      if (seenEventsRef.current.has(event.sequence)) return;
      seenEventsRef.current.add(event.sequence);
      setEvents((current) => [...current, event]);
      setTimeline((current) => reduceRunEvent(current, event));
      if (event.kind === "status" && event.status) setRunStatus(event.status);
      const terminal = terminalStatus(event);
      if (terminal) {
        source.close();
        sourceRef.current = null;
        setRunStatus(terminal);
        window.setTimeout(() => {
          if (conversationScope === "project") {
            void refreshWorkspace(nextRun.workspace);
          } else {
            void api.getNavigation().then(setNavigation);
          }
        }, 250);
      }
    };
    source.onerror = () => {
      source.close();
      sourceRef.current = null;
    };
  }

  async function startRun() {
    const cleanPrompt = prompt.trim();
    if (!cleanPrompt || (conversationScope === "project" && !workspace) || isActive(runStatus)) return;
    setError(null);
    setTimeline((current) => [
      ...current,
      { id: `user-${Date.now()}`, type: "user", content: cleanPrompt, timestamp: Date.now() },
    ]);
    setPrompt("");
    setRunStatus("queued");
    setEvents([]);
    try {
      const result = await api.startRun({
        scope: conversationScope,
        ...(conversationScope === "project" ? { workspace } : {}),
        prompt: cleanPrompt,
        ...(selectedSessionId ? { sessionId: selectedSessionId } : {}),
      });
      setRun(result.run);
      setSelectedSessionId(result.run.sessionId);
      setRunStatus(result.run.status);
      void api.getNavigation().then(setNavigation);
      connectRun(result.run);
    } catch (reason) {
      setRunStatus("failed");
      const message = reason instanceof Error ? reason.message : "任务启动失败。";
      setError(message);
      setTimeline((current) => [
        ...current,
        { id: `error-${Date.now()}`, type: "error", content: message, timestamp: Date.now() },
      ]);
    }
  }

  async function stopRun() {
    if (!run) return;
    try {
      const result = await api.stopRun(run.runId);
      setRun(result.run);
      setRunStatus(result.run.status);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法停止任务。");
    }
  }

  async function approve(requestId: string, decision: string, feedback?: string) {
    if (!run) throw new Error("任务已结束。");
    try {
      await api.approve(run.runId, { requestId, decision, feedback });
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "无法提交审批结果。";
      setError(message);
      throw reason;
    }
  }

  async function saveSettings(payload: Parameters<typeof api.saveSettings>[0]) {
    const nextSettings = await api.saveSettings(payload);
    setSettings(nextSettings);
    return nextSettings;
  }

  function beginInspectorResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    inspectorResizeRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: inspectorWidthRef.current,
    };
    document.body.classList.add("is-resizing-inspector");
  }

  function moveInspectorResize(event: ReactPointerEvent<HTMLDivElement>) {
    const resize = inspectorResizeRef.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    const nextWidth = clampInspectorWidth(resize.startWidth + resize.startX - event.clientX);
    inspectorWidthRef.current = nextWidth;
    setInspectorWidth(nextWidth);
  }

  function finishInspectorResize() {
    if (!inspectorResizeRef.current) return;
    inspectorResizeRef.current = null;
    document.body.classList.remove("is-resizing-inspector");
    persistInspectorWidth(inspectorWidthRef.current);
  }

  function resizeInspectorWithKeyboard(event: ReactKeyboardEvent<HTMLDivElement>) {
    const { min, max } = inspectorWidthBounds();
    let nextWidth: number | null = null;
    if (event.key === "ArrowLeft") nextWidth = inspectorWidthRef.current + 16;
    if (event.key === "ArrowRight") nextWidth = inspectorWidthRef.current - 16;
    if (event.key === "Home") nextWidth = min;
    if (event.key === "End") nextWidth = max;
    if (nextWidth === null) return;
    event.preventDefault();
    const clampedWidth = clampInspectorWidth(nextWidth);
    inspectorWidthRef.current = clampedWidth;
    setInspectorWidth(clampedWidth);
    persistInspectorWidth(clampedWidth);
  }

  function resetInspectorWidth() {
    const nextWidth = defaultInspectorWidth();
    inspectorWidthRef.current = nextWidth;
    setInspectorWidth(nextWidth);
    persistInspectorWidth(nextWidth);
  }

  if (booting) {
    return (
      <div className="boot-screen">
        <div className="brand-mark">M</div>
        <LoaderCircle className="spin" size={19} />
        <span>正在连接 MiniCode 本机服务</span>
      </div>
    );
  }

  return (
    <>
      <a className="skip-link" href="#main-content">跳到任务内容</a>
      <div
        className={`app-shell ${inspectorOpen ? "has-inspector" : ""}`}
        style={{ "--inspector-w": `${inspectorWidth}px` } as CSSProperties}
      >
        <button className={`mobile-scrim ${railOpen ? "is-visible" : ""}`} onClick={() => setRailOpen(false)} aria-label="关闭任务栏" />
        <AppRail
          workspace={workspace}
          workspaceStatus={workspaceStatus}
          projects={navigation.projects}
          sessions={navigation.sessions}
          pinnedSessionIds={navigation.pinnedSessionIds}
          pinnedWorkspacePaths={navigation.pinnedWorkspacePaths}
          selectedSessionId={selectedSessionId}
          conversationScope={conversationScope}
          runStatus={runStatus}
          mobileOpen={railOpen}
          onCloseMobile={() => setRailOpen(false)}
          onNewStandaloneTask={newStandaloneTask}
          onNewProjectTask={changeWorkspace}
          onSelectSession={(sessionId) => void selectSession(sessionId)}
          onDeleteSession={(sessionId) => void deleteSelectedSession(sessionId)}
          onChangeWorkspace={changeWorkspace}
          onPickWorkspace={pickWorkspace}
          onTogglePin={(kind, target, pinned) => void togglePin(kind, target, pinned)}
          onOpenSettings={() => setSettingsOpen(true)}
          onToggleInspector={() => setInspectorOpen((value) => !value)}
        />
        <div className="center-stack">
          <RunStream
            items={timeline}
            runStatus={runStatus}
            hasSession={Boolean(selectedSessionId)}
            standalone={conversationScope === "standalone"}
            contextLabel={conversationScope === "standalone" ? "独立对话" : workspaceName(workspace)}
            onPromptSuggestion={setPrompt}
            onApproval={approve}
            onOpenRail={() => setRailOpen(true)}
          />
          <Composer
            value={prompt}
            runStatus={runStatus}
            settings={settings}
            placeholder={conversationScope === "standalone"
              ? "输入问题，当前对话不读取项目文件…"
              : `描述你要 MiniCode 在 ${workspaceName(workspace)} 完成的任务…`}
            hint={conversationScope === "standalone"
              ? "Enter 发送。当前是独立对话，不读取项目文件。"
              : "Enter 发送。Shift + Enter 换行。文件修改和高风险命令会先请你确认。"}
            onChange={setPrompt}
            onSubmit={() => void startRun()}
            onStop={() => void stopRun()}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        </div>
        {!inspectorOpen && (
          <button
            className="icon-button inspector-toggle workspace-inspector-toggle"
            onClick={() => setInspectorOpen(true)}
            aria-label="打开检查器"
            title="打开文件与检查器"
          >
            <PanelRight size={18} />
          </button>
        )}
        <button className={`inspector-scrim ${inspectorOpen ? "is-visible" : ""}`} onClick={() => setInspectorOpen(false)} aria-label="关闭检查器" />
        {inspectorOpen && (
          <div
            className="inspector-resize-handle"
            role="separator"
            tabIndex={0}
            aria-label="调整检查器宽度"
            aria-orientation="vertical"
            aria-controls="workspace-inspector"
            aria-valuemin={inspectorWidthBounds().min}
            aria-valuemax={inspectorWidthBounds().max}
            aria-valuenow={inspectorWidth}
            title="拖动调整宽度，双击恢复默认"
            onDoubleClick={resetInspectorWidth}
            onKeyDown={resizeInspectorWithKeyboard}
            onPointerDown={beginInspectorResize}
            onPointerMove={moveInspectorResize}
            onPointerUp={finishInspectorResize}
            onPointerCancel={finishInspectorResize}
            onLostPointerCapture={finishInspectorResize}
          />
        )}
        <Inspector
          open={inspectorOpen}
          workspace={conversationScope === "project" ? workspace : ""}
          workspaceStatus={conversationScope === "project" ? workspaceStatus : null}
          runStatus={runStatus}
          events={events}
          settings={settings}
          diff={diff}
          selectedFile={selectedFile}
          onSelectFile={setSelectedFile}
          onClose={() => setInspectorOpen(false)}
          onOpenSettings={() => setSettingsOpen(true)}
        />
      </div>
      <SettingsDrawer
        open={settingsOpen}
        settings={settings}
        onClose={() => setSettingsOpen(false)}
        onSave={saveSettings}
        onTest={api.testSettings}
      />
      {error && (
        <div className="toast" role="alert">
          <CircleAlert size={17} />
          <span>{error}</span>
          <button onClick={() => setError(null)} aria-label="关闭错误提示"><X size={15} /></button>
        </div>
      )}
    </>
  );
}
