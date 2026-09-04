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
import { historyToTimeline, limitLiveTimeline, reduceRunEvent } from "./lib/timeline";
import type {
  CapabilitySnapshot,
  ConversationScope,
  ExecutionProfile,
  RunEvent,
  RunInfo,
  RunStatus,
  NavigationPayload,
  SessionSummary,
  SessionInspect,
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
const RESUME_PROMPT = "继续上次任务。先确认已完成和未完成的部分，再从中断处接着处理。";
const RETRY_PROMPT = "重试未完成的任务。先检查失败原因和当前状态，再从未完成处继续。";
const RECONNECT_ERROR = "实时连接已中断，正在等待任务状态恢复。切换对话不会停止后台任务。";
const MAX_RETAINED_RUN_EVENTS = 512;

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

function statusFromSession(status: string): RunStatus {
  if (["queued", "running", "stopping", "completed", "failed", "stopped"].includes(status)) {
    return status as RunStatus;
  }
  return "idle";
}

function timelineWithEvents(base: TimelineItem[], events: RunEvent[]): TimelineItem[] {
  const reduced = [...events]
    .sort((left, right) => left.sequence - right.sequence)
    .reduce((items, event) => reduceRunEvent(items, event), base);
  return limitLiveTimeline(reduced, MAX_RETAINED_RUN_EVENTS);
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
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot | null>(null);
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(false);
  const [capabilitiesError, setCapabilitiesError] = useState("");
  const [sessionInspect, setSessionInspect] = useState<SessionInspect | null>(null);
  const [executionProfile, setExecutionProfile] = useState<ExecutionProfile>("standard");
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
  const [sessionLoading, setSessionLoading] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);
  const lastEventSequenceRef = useRef(0);
  const connectionGenerationRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const selectionRequestRef = useRef(0);
  const startInFlightRef = useRef(false);
  const selectedSessionIdRef = useRef<string | null>(null);
  const workspaceRef = useRef(workspace);
  const conversationScopeRef = useRef(conversationScope);
  const inspectorWidthRef = useRef(inspectorWidth);
  const inspectorResizeRef = useRef<{
    pointerId: number;
    startX: number;
    startWidth: number;
  } | null>(null);

  function disconnectRunStream() {
    connectionGenerationRef.current += 1;
    sourceRef.current?.close();
    sourceRef.current = null;
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }

  function setSessionStatus(sessionId: string, status: RunStatus) {
    setNavigation((current) => ({
      ...current,
      sessions: current.sessions.map((session) =>
        session.sessionId === sessionId
          ? { ...session, status, updatedAt: Date.now() }
          : session,
      ),
    }));
  }

  useEffect(() => {
    inspectorWidthRef.current = inspectorWidth;
  }, [inspectorWidth]);

  useEffect(() => {
    selectedSessionIdRef.current = selectedSessionId;
  }, [selectedSessionId]);

  useEffect(() => {
    workspaceRef.current = workspace;
  }, [workspace]);

  useEffect(() => {
    conversationScopeRef.current = conversationScope;
  }, [conversationScope]);

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

  useEffect(() => {
    const hasBackgroundRun = navigation.sessions.some((session) =>
      isActive(statusFromSession(session.status)),
    );
    if (!hasBackgroundRun) return;
    const timer = window.setInterval(() => {
      void api.getNavigation().then(setNavigation).catch(() => undefined);
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [navigation.sessions]);

  useEffect(() => {
    if (conversationScope === "project" && !workspace) {
      setCapabilities(null);
      return;
    }
    let cancelled = false;
    setCapabilities(null);
    setCapabilitiesLoading(true);
    setCapabilitiesError("");
    api.workspaceCapabilities(workspace, conversationScope)
      .then((result) => {
        if (!cancelled) setCapabilities(result.capabilities);
      })
      .catch((reason) => {
        if (!cancelled) {
          setCapabilities(null);
          setCapabilitiesError(reason instanceof Error ? reason.message : "无法读取工作区能力。");
        }
      })
      .finally(() => {
        if (!cancelled) setCapabilitiesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationScope, workspace]);

  const refreshWorkspace = useCallback(async (path: string) => {
    const selectionRequest = selectionRequestRef.current;
    const [status, nextNavigation] = await Promise.all([
      api.workspaceStatus(path),
      api.getNavigation(),
    ]);
    if (
      selectionRequestRef.current !== selectionRequest
      || workspaceRef.current !== path
      || conversationScopeRef.current !== "project"
    ) return status;
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
        setExecutionProfile(payload.settings.executionProfile);
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
      disconnectRunStream();
    };
  }, []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        newStandaloneTask();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (conversationScope === "standalone" || !workspace || !selectedFile) {
      setDiff("");
      return;
    }
    let cancelled = false;
    api.workspaceDiff(workspace, selectedFile)
      .then((result) => {
        if (!cancelled) setDiff(result.diff);
      })
      .catch(() => {
        if (!cancelled) setDiff("");
      });
    return () => {
      cancelled = true;
    };
  }, [conversationScope, workspace, selectedFile, workspaceStatus]);

  function resetTask(scope: ConversationScope) {
    selectionRequestRef.current += 1;
    disconnectRunStream();
    setConversationScope(scope);
    setSelectedSessionId(null);
    selectedSessionIdRef.current = null;
    setTimeline([]);
    setEvents([]);
    setSessionInspect(null);
    setRun(null);
    setRunStatus("idle");
    setSessionLoading(false);
    setPrompt("");
  }

  function newStandaloneTask() {
    resetTask("standalone");
  }

  async function selectSession(sessionId: string) {
    const requestId = selectionRequestRef.current + 1;
    selectionRequestRef.current = requestId;
    disconnectRunStream();
    setError(null);
    setSessionLoading(true);
    setSelectedSessionId(sessionId);
    selectedSessionIdRef.current = sessionId;
    setTimeline([]);
    setEvents([]);
    setSessionInspect(null);
    setRun(null);
    setRunStatus("idle");
    try {
      const summary = navigation.sessions.find((item) => item.sessionId === sessionId);
      if (!summary) throw new Error("会话不存在或已被删除。");
      if (summary.scope === "project" && summary.projectPath && summary.projectPath !== workspace) {
        const opened = await api.openWorkspace(summary.projectPath);
        if (selectionRequestRef.current !== requestId) return;
        const nextNavigation = await api.getNavigation();
        if (selectionRequestRef.current !== requestId) return;
        setWorkspace(opened.workspace);
        workspaceRef.current = opened.workspace;
        setWorkspaceStatus(opened.workspaceStatus);
        setNavigation(nextNavigation);
        setSelectedFile(opened.workspaceStatus.files[0]?.path || null);
        setDiff("");
      }
      const [result, inspectResult, runSnapshot] = await Promise.all([
        api.getSession(sessionId),
        api.getSessionInspect(sessionId).catch(() => null),
        api.getSessionRun(sessionId).catch(() => ({ run: null, events: [] })),
      ]);
      if (selectionRequestRef.current !== requestId) return;
      setConversationScope(summary.scope);
      conversationScopeRef.current = summary.scope;
      setSelectedSessionId(sessionId);
      selectedSessionIdRef.current = sessionId;
      const history = historyToTimeline(result.messages);
      const restoredTimeline = runSnapshot.run && (
        isActive(runSnapshot.run.status)
        || runSnapshot.run.status === "stopped"
      )
        ? timelineWithEvents(history, runSnapshot.events)
        : history;
      setTimeline(restoredTimeline);
      setSessionInspect(inspectResult?.inspect || null);
      setEvents(runSnapshot.events.slice(-MAX_RETAINED_RUN_EVENTS));
      setRun(runSnapshot.run);
      const restoredStatus = runSnapshot.run?.status || statusFromSession(result.session.status);
      setRunStatus(restoredStatus);
      setSessionStatus(sessionId, restoredStatus);
      if (runSnapshot.run && isActive(runSnapshot.run.status)) {
        connectRun(runSnapshot.run, runSnapshot.events);
      }
    } catch (reason) {
      if (selectionRequestRef.current === requestId) {
        setError(reason instanceof Error ? reason.message : "无法读取会话。");
        setSelectedSessionId(null);
        selectedSessionIdRef.current = null;
      }
    } finally {
      if (selectionRequestRef.current === requestId) setSessionLoading(false);
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
    const requestId = selectionRequestRef.current + 1;
    selectionRequestRef.current = requestId;
    disconnectRunStream();
    setError(null);
    try {
      const result = await api.openWorkspace(path);
      if (selectionRequestRef.current !== requestId) return;
      const nextNavigation = await api.getNavigation();
      if (selectionRequestRef.current !== requestId) return;
      setWorkspace(result.workspace);
      workspaceRef.current = result.workspace;
      setWorkspaceStatus(result.workspaceStatus);
      setConversationScope("project");
      conversationScopeRef.current = "project";
      setNavigation(nextNavigation);
      setSelectedFile(result.workspaceStatus.files[0]?.path || null);
      setDiff("");
      setSelectedSessionId(null);
      selectedSessionIdRef.current = null;
      setSessionInspect(null);
      setTimeline([]);
      setEvents([]);
      setRun(null);
      setRunStatus("idle");
      setPrompt("");
    } catch (reason) {
      if (selectionRequestRef.current !== requestId) return;
      const message = reason instanceof Error ? reason.message : "无法打开工作区。";
      setError(message);
      throw reason;
    }
  }

  async function pickWorkspace() {
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

  function connectRun(nextRun: RunInfo, initialEvents: RunEvent[] = []) {
    disconnectRunStream();
    const generation = connectionGenerationRef.current;
    lastEventSequenceRef.current = Math.max(
      0,
      ...initialEvents.map((event) => event.sequence),
    );
    let terminalSeen = false;

    function isCurrentConnection() {
      return connectionGenerationRef.current === generation
        && selectedSessionIdRef.current === nextRun.sessionId;
    }

    function finishRun(status: RunStatus) {
      terminalSeen = true;
      sourceRef.current?.close();
      sourceRef.current = null;
      setRunStatus(status);
      setRun((current) => current?.runId === nextRun.runId ? { ...current, status } : current);
      setSessionStatus(nextRun.sessionId, status);
      window.setTimeout(() => {
        if (!isCurrentConnection()) return;
        if (
          conversationScopeRef.current === "project"
          && workspaceRef.current === nextRun.workspace
        ) {
          void refreshWorkspace(nextRun.workspace);
        } else {
          void api.getNavigation().then(setNavigation).catch(() => undefined);
        }
        void api.getSessionInspect(nextRun.sessionId)
          .then((result) => {
            if (isCurrentConnection()) setSessionInspect(result.inspect);
          })
          .catch(() => undefined);
      }, 250);
    }

    function applyEvent(event: RunEvent) {
      if (
        !isCurrentConnection()
        || !Number.isSafeInteger(event.sequence)
        || event.sequence <= lastEventSequenceRef.current
      ) return;
      lastEventSequenceRef.current = event.sequence;
      setEvents((current) => [...current, event].slice(-MAX_RETAINED_RUN_EVENTS));
      setTimeline((current) => limitLiveTimeline(
        reduceRunEvent(current, event),
        MAX_RETAINED_RUN_EVENTS,
      ));
      if (event.kind === "capability_snapshot" && event.capabilities) {
        setCapabilities(event.capabilities);
      }
      if (event.kind === "status" && event.status) {
        setRunStatus(event.status);
        setRun((current) => current?.runId === nextRun.runId
          ? { ...current, status: event.status || current.status }
          : current);
        setSessionStatus(nextRun.sessionId, event.status);
      }
      const terminal = terminalStatus(event);
      if (terminal) finishRun(terminal);
    }

    function subscribe() {
      if (!isCurrentConnection() || terminalSeen) return;
      const after = lastEventSequenceRef.current;
      const source = api.eventSource(nextRun.runId, after);
      sourceRef.current = source;
      source.onopen = () => {
        if (isCurrentConnection()) {
          setError((current) => current === RECONNECT_ERROR ? null : current);
        }
      };
      source.onmessage = (message) => applyEvent(parseRunEvent(message));
      source.onerror = () => {
        source.close();
        if (sourceRef.current === source) sourceRef.current = null;
        if (!isCurrentConnection() || terminalSeen) return;
        scheduleReconnect(700);
      };
    }

    function scheduleReconnect(delay: number) {
      if (!isCurrentConnection() || terminalSeen) return;
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = window.setTimeout(async () => {
          reconnectTimerRef.current = null;
          if (!isCurrentConnection()) return;
          try {
            const after = lastEventSequenceRef.current;
            const snapshot = await api.getRun(nextRun.runId, after);
            if (!snapshot.run || !isCurrentConnection()) return;
            [...snapshot.events]
              .sort((left, right) => left.sequence - right.sequence)
              .forEach(applyEvent);
            setRun(snapshot.run);
            setRunStatus(snapshot.run.status);
            setSessionStatus(nextRun.sessionId, snapshot.run.status);
            setError((current) => current === RECONNECT_ERROR ? null : current);
            if (isActive(snapshot.run.status) && !terminalSeen) subscribe();
            else if (!terminalSeen) finishRun(snapshot.run.status);
          } catch {
            if (isCurrentConnection()) {
              setError(RECONNECT_ERROR);
              scheduleReconnect(1_500);
            }
          }
        }, delay);
    }

    if (isActive(nextRun.status)) subscribe();
  }

  async function startRun(promptOverride?: string) {
    const cleanPrompt = (promptOverride ?? prompt).trim();
    if (
      !cleanPrompt
      || startInFlightRef.current
      || sessionLoading
      || (conversationScope === "project" && !workspace)
      || isActive(runStatus)
    ) return;
    startInFlightRef.current = true;
    const selectionRequest = selectionRequestRef.current;
    setError(null);
    setTimeline((current) => [
      ...current,
      { id: `user-${Date.now()}`, type: "user", content: cleanPrompt, timestamp: Date.now() },
    ]);
    if (promptOverride === undefined) setPrompt("");
    setRunStatus("queued");
    if (selectedSessionId) setSessionStatus(selectedSessionId, "queued");
    setEvents([]);
    setSessionInspect(null);
    try {
      const result = await api.startRun({
        scope: conversationScope,
        ...(conversationScope === "project" ? { workspace } : {}),
        prompt: cleanPrompt,
        executionProfile,
        ...(selectedSessionId ? { sessionId: selectedSessionId } : {}),
      });
      if (selectionRequestRef.current !== selectionRequest) {
        void api.getNavigation().then(setNavigation).catch(() => undefined);
        return;
      }
      setRun(result.run);
      setSelectedSessionId(result.run.sessionId);
      selectedSessionIdRef.current = result.run.sessionId;
      setRunStatus(result.run.status);
      void api.getNavigation().then((nextNavigation) => {
        setNavigation({
          ...nextNavigation,
          sessions: nextNavigation.sessions.map((session) =>
            session.sessionId === result.run.sessionId
              ? { ...session, status: result.run.status, updatedAt: Date.now() }
              : session,
          ),
        });
      }).catch(() => undefined);
      connectRun(result.run);
    } catch (reason) {
      if (selectionRequestRef.current !== selectionRequest) return;
      setRunStatus("failed");
      const message = reason instanceof Error ? reason.message : "任务启动失败。";
      setError(message);
      if (promptOverride === undefined) {
        setPrompt((current) => current || cleanPrompt);
      }
      setTimeline((current) => [
        ...current,
        { id: `error-${Date.now()}`, type: "error", content: message, timestamp: Date.now() },
      ]);
    } finally {
      startInFlightRef.current = false;
    }
  }

  async function stopRun() {
    if (!run) return;
    const stoppingRun = run;
    const selectionRequest = selectionRequestRef.current;
    const isCurrentStop = () => (
      selectionRequestRef.current === selectionRequest
      && selectedSessionIdRef.current === stoppingRun.sessionId
    );
    setRunStatus("stopping");
    setRun({ ...stoppingRun, status: "stopping" });
    setSessionStatus(stoppingRun.sessionId, "stopping");
    try {
      const result = await api.stopRun(stoppingRun.runId);
      if (isCurrentStop()) {
        setRun((current) => current?.runId === stoppingRun.runId ? result.run : current);
        setRunStatus(result.run.status);
      }
      setSessionStatus(stoppingRun.sessionId, result.run.status);
    } catch (reason) {
      try {
        const snapshot = await api.getRun(stoppingRun.runId);
        if (snapshot.run) {
          if (isCurrentStop()) {
            setRun((current) => current?.runId === stoppingRun.runId ? snapshot.run : current);
            setRunStatus(snapshot.run.status);
          }
          setSessionStatus(stoppingRun.sessionId, snapshot.run.status);
        }
      } catch {
        // Keep the warning below as the actionable source of truth.
      }
      if (isCurrentStop()) {
        setError(reason instanceof Error ? reason.message : "无法停止任务。");
      }
    }
  }

  function resumeRun() {
    void startRun(runStatus === "failed" ? RETRY_PROMPT : RESUME_PROMPT);
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
            streamKey={selectedSessionId || `draft-${conversationScope}-${workspace}`}
            loading={sessionLoading}
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
            sessionLoading={sessionLoading}
            recoveryMode={selectedSessionId && runStatus === "stopped"
              ? "resume"
              : selectedSessionId && runStatus === "failed"
                ? "retry"
                : null}
            settings={settings}
            executionProfile={executionProfile}
            placeholder={conversationScope === "standalone"
              ? "输入问题，当前对话不读取项目文件…"
              : `描述你要 MiniCode 在 ${workspaceName(workspace)} 完成的任务…`}
            hint={conversationScope === "standalone"
              ? "Enter 发送。当前是独立对话，不读取项目文件。"
              : "Enter 发送。Shift + Enter 换行。文件修改和高风险命令会先请你确认。"}
            onChange={setPrompt}
            onSubmit={() => void startRun()}
            onStop={() => void stopRun()}
            onResume={resumeRun}
            onOpenSettings={() => setSettingsOpen(true)}
            onExecutionProfileChange={setExecutionProfile}
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
          capabilities={capabilities}
          capabilitiesLoading={capabilitiesLoading}
          capabilitiesError={capabilitiesError}
          sessionInspect={sessionInspect}
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
