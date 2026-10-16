import { Check, CircleAlert, LoaderCircle, PanelRight, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";

import { api, ApiError, parseRunEvent } from "./api/client";
import { AppRail } from "./components/AppRail";
import { Composer } from "./components/Composer";
import { ContextUsage } from "./components/ContextUsage";
import { ConversationReasoningSelector } from "./components/ConversationContextFields";
import { ExtensionsPage } from "./components/ExtensionsPage";
import { Inspector } from "./components/Inspector";
import type { InspectorTab, InspectorTabRequest } from "./components/Inspector";
import { RunStream } from "./components/RunStream";
import type { FileNavigationRequest, FileReference } from "./lib/fileReferences";
import { SettingsPage } from "./components/SettingsPage";
import { SettingsDialog, type SettingsSection } from "./components/SettingsDialog";
import { WebSearchSettings } from "./components/WebSearchSettings";
import { ToolCatalog } from "./components/ToolCatalog";
import { TrashPage } from "./components/TrashPage";
import { ForkDialog } from "./components/ForkDialog";
import { waitForWorktree, WorktreeOperationFailure } from "./lib/worktrees";
import { historyToTimeline, reduceRunEvent } from "./lib/timeline";
import { appendRunImages } from "./lib/runImages";
import { ConversationCache, mergeConversationPage, mergeProcessMessages, type ConversationViewState } from "./lib/conversationCache";
import { RunEventBatch } from "./lib/runEventBatch";
import { draftKey } from "./lib/drafts";
import { composeMessage, mergeAttachments, messagePreview, parseMessage } from "./lib/attachments";
import { applySkillCommand } from "./lib/skillCompletion";
import { CONTEXT_ROTATION_REQUEST, type ComposerNavigation, type ComposerInspectorSection } from "./lib/composerCommands";
import { useDraft } from "./lib/useDraft";
import { useExecutionProfile } from "./lib/useExecutionProfile";
import { usePermissionMode } from "./lib/usePermissionMode";
import { useConversationContext } from "./lib/useConversationContext";
import { isContextRefreshEvent, useContextRefresh } from "./lib/useContextRefresh";
import { taskFileSelection, useTaskFile } from "./lib/useTaskFile";
import { TaskFilePicker } from "./components/TaskFilePicker";
import { RunBudgetFields } from "./components/RunBudgetFields";
import { ExecutionLocationFields } from "./components/ExecutionLocationFields";
import { executionLocationError, runExecutionLocation } from "./lib/executionLocation";
import type { ExecutionLocation } from "./types";
import { runBudgetSelection, useRunBudget } from "./lib/useRunBudget";
import { buildRunSummary } from "./lib/runSummary";
import { executionForRun, mergeRunInput, updateRunActivity } from "./lib/execution";
import { traceForRun } from "./lib/runtime";
import { resumeTaskPrompt } from "./lib/runResume";
import type {
  CapabilitySnapshot,
  ConversationScope,
  ExtensionUseRequest,
  RunEvent,
  RunInfo,
  RunInput,
  RunSnapshot,
  RunStatus,
  NavigationPayload,
  SessionSummary,
  SessionInspect,
  SettingsView,
  TimelineItem,
  WorkspaceStatus,
  TurnsPage,
  SessionTurn,
  TurnMessagesPage,
} from "./types";

const EMPTY_NAVIGATION: NavigationPayload = {
  projects: [],
  sessions: [],
  pinnedSessionIds: [],
  pinnedWorkspacePaths: [],
};

type AppPage = "conversation" | "trash" | "settings" | "extensions";

function pageFromHash(): AppPage {
  const page = window.location.hash.slice(1);
  return page === "trash" || page === "settings" || page === "extensions" ? page : "conversation";
}

const INSPECTOR_WIDTH_STORAGE_KEY = "minicode.inspector.width";
const RAIL_WIDTH = 280;
const INSPECTOR_MIN_WIDTH = 360;
const INSPECTOR_MAX_WIDTH = 760;
const CENTER_MIN_WIDTH = 480;
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
  return clampInspectorWidth(Math.min(520, Math.max(400, window.innerWidth * 0.28)));
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
  return reduced;
}

function workspaceName(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts.at(-1) || path;
}

function sameWorkspacePath(left: string, right: string): boolean {
  const identity = (path: string) => {
    const normalized = path.replace(/\\/g, "/").replace(/\/+$/, "");
    return /^[a-z]:\//i.test(normalized) || normalized.startsWith("//") ? normalized.toLowerCase() : normalized;
  };
  return identity(left) === identity(right);
}

export default function App() {
  const [conversationViews, setConversationViews] = useState(false);
  const [sessionSyncing, setSessionSyncing] = useState(false);
  const [restoredView, setRestoredView] = useState<ConversationViewState | undefined>();
  const conversationCache = useRef(new ConversationCache());
  const captureViewRef = useRef<(() => ConversationViewState | undefined) | null>(null);
  const selectionAbortRef = useRef<AbortController | null>(null);
  const historyRefreshRef = useRef<{ identity: string; revision: number } | null>(null);
  const earlierPageRef = useRef<{ selection: number; before: number; revision?: string; request: Promise<TurnsPage> } | null>(null);
  const earlierRequestRef = useRef<number | null>(null);
  const processPageRequests = useRef(new Map<string, Promise<void>>());
  const [booting, setBooting] = useState(true);
  const [workspace, setWorkspace] = useState("");
  const [workspaceStatus, setWorkspaceStatus] = useState<WorkspaceStatus | null>(null);
  const [conversationScope, setConversationScope] = useState<ConversationScope>("standalone");
  const [navigation, setNavigation] = useState<NavigationPayload>(EMPTY_NAVIGATION);
  const [settings, setSettings] = useState<SettingsView | null>(null);
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot | null>(null);
  const capabilitiesContextRef = useRef("");
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(false);
  const [capabilitiesError, setCapabilitiesError] = useState("");
  const [sessionInspect, setSessionInspect] = useState<SessionInspect | null>(null);
  const [sessionInspectLoading, setSessionInspectLoading] = useState(false);
  const [contextRefreshRevision, setContextRefreshRevision] = useState(0);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [history, setHistory] = useState<TurnsPage | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [forkTurn, setForkTurn] = useState<SessionTurn | null>(null);
  const [environment, setEnvironment] = useState<"local" | "worktree">("local");
  const selectedSummary = navigation.sessions.find((session) => session.sessionId === selectedSessionId);
  const composerEnvironment = selectedSessionId ? selectedSummary?.worktreeId ? "worktree" : "local" : environment;
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [run, setRun] = useState<RunInfo | null>(null);
  const [runStatus, setRunStatus] = useState<RunStatus>("idle");
  const [questionOpenRequest, setQuestionOpenRequest] = useState(0);
  const [selectedQuestionId, setSelectedQuestionId] = useState<string>();
  const [startingRun, setStartingRun] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [inputBusy, setInputBusy] = useState(false);
  const inputBusyRef = useRef(false);
  const inputSubmissionRef = useRef<{ key: string; id: string } | null>(null);
  const draft = useDraft({ sessionId: selectedSessionId, scope: conversationScope, workspace });
  const [executionLocations, setExecutionLocations] = useState<Record<string, ExecutionLocation>>({});
  const executionLocation = executionLocations[draft.key] || "project";
  const taskFile = useTaskFile(JSON.stringify([draft.key, environment]), workspace, selectedSessionId);
  const previousRunBudget = run?.runBudget || traceForRun(sessionInspect?.runtimeRuns || [], run?.runId)?.runBudget || null;
  const runBudget = useRunBudget(draft.key, previousRunBudget);
  const { value: prompt, setValue: setPrompt } = draft;
  const [composerFocusRequest, setComposerFocusRequest] = useState(0);
  const [composerNotice, setComposerNotice] = useState("");
  const [inspectorTabRequest, setInspectorTabRequest] = useState<InspectorTabRequest | null>(null);
  const [fileNavigation, setFileNavigation] = useState<{
    workspace: string; sessionId: string | null; request: FileNavigationRequest;
  } | null>(null);
  const [activePage, setActivePage] = useState<AppPage>(pageFromHash);
  const [extensionView, setExtensionView] = useState<"skills" | "extensions">("skills");
  const [settingsSection, setSettingsSection] = useState<SettingsSection>(() => pageFromHash() === "extensions" ? "extensions" : "models");
  const settingsReturnPage = useRef<"conversation" | "trash">(pageFromHash() === "trash" ? "trash" : "conversation");
  const settingsOpen = activePage === "settings" || activePage === "extensions";
  const backgroundPage = settingsOpen ? settingsReturnPage.current : activePage;
  const conversationVisible = backgroundPage === "conversation";
  const [trashRevision, setTrashRevision] = useState(0);
  const [archivingSessionId, setArchivingSessionId] = useState<string | null>(null);
  const archivingRef = useRef(false);
  const [closingProjectPath, setClosingProjectPath] = useState<string | null>(null);
  const closingProjectRef = useRef(false);
  const [railNotice, setRailNotice] = useState("");
  const [trashNotice, setTrashNotice] = useState("");
  const [capabilityRevision, setCapabilityRevision] = useState(0);
  const refreshComposerSkills = useCallback(() => setCapabilityRevision(value => value + 1), []);
  const [railOpen, setRailOpen] = useState(false);
  const [railCollapsed, setRailCollapsed] = useState(() => {
    try { return window.localStorage.getItem("minicode.rail.collapsed") === "true"; }
    catch { return false; }
  });
  const [searchRequest, setSearchRequest] = useState(0);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [agentPanelTarget, setAgentPanelTarget] = useState<HTMLDivElement | null>(null);
  const [inspectorWidth, setInspectorWidth] = useState(initialInspectorWidth);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [diff, setDiff] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);
  const streamBatchRef = useRef<RunEventBatch | null>(null);
  const draftRestoredRef = useRef(false);
  const lastEventSequenceRef = useRef(0);
  const connectionGenerationRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const selectionRequestRef = useRef(0);
  const contextRequestRef = useRef(0);
  const extensionUseRevisionRef = useRef(0);
  const startInFlightRef = useRef(false);
  const worktreeCreateRef = useRef<{ workspace: string; requestId: string } | null>(null);
  const selectedSessionIdRef = useRef<string | null>(null);
  const workspaceRef = useRef(workspace);
  const conversationScopeRef = useRef(conversationScope);
  const inspectorWidthRef = useRef(inspectorWidth);
  const inspectorResizeRef = useRef<{
    pointerId: number;
    startX: number;
    startWidth: number;
  } | null>(null);
  const runSummary = useMemo(() => selectedSessionId && !sessionLoading
    ? buildRunSummary(runStatus, run, sessionInspect, events.filter(event => event.kind === "error").map(event => event.message || event.content || "").filter(Boolean))
    : null, [selectedSessionId, sessionLoading, runStatus, run, sessionInspect]);
  const execution = useMemo(() => executionForRun(run, traceForRun(sessionInspect?.runtimeRuns || [], run?.runId)), [run, sessionInspect]);
  const actualProfile = execution?.profile || run?.executionProfile;
  const profileSelection = useExecutionProfile(draft.key,
    (selectedSessionId ? actualProfile : undefined) || settings?.executionProfile || "standard");
  const executionProfile = profileSelection.value;
  const actualPermissionMode = run?.sessionId === selectedSessionId ? run?.permissionMode ?? "default" : "default";
  const permissionSelection = usePermissionMode(draft.key,
    run?.sessionId === selectedSessionId ? actualPermissionMode : undefined);
  const permissionMode = settings?.permissionModeSupported ? permissionSelection.value : "default";
  const actualLocation = runExecutionLocation(run, traceForRun(sessionInspect?.runtimeRuns || [], run?.runId));
  const displayedLocation = isActive(runStatus) ? actualLocation : executionLocation;
  const locationError = conversationScope === "project" && !isActive(runStatus)
    ? executionLocationError(settings, executionLocation, executionProfile) : "";
  const contextSelection = useConversationContext(draft.key, selectedSessionId, conversationScope, workspace, settings?.conversationContext);
  const recoveryMode = selectedSessionId && !isActive(runStatus) && (runStatus === "stopped" || runStatus === "failed" || runSummary?.failure)
    ? execution?.nodes.some(node => node.state === "WAITING_INPUT") ? "input" : runStatus === "stopped" ? "resume" : "retry"
    : null;
  const recoveryAction = recoveryMode ? {
    label: recoveryMode === "input" ? "补充信息" : "继续任务",
    hint: recoveryMode === "input" ? "补充所需信息后发送，继续本轮任务。" : "按当前预算从已有进度继续，保留原验收条件；历史用量保留，本轮预算单独生效。",
    disabled: sessionLoading || sessionSyncing || startingRun || Boolean(selectedSummary?.worktreeId && selectedSummary.worktreeStatus !== "ready")
      || conversationScope === "project" && (taskFile.blocked || !settings?.executionLocationSupported) || contextSelection.blocked,
    onClick: () => { if (recoveryMode === "input") setComposerFocusRequest(current => current + 1); else void resumeRun(); },
  } : undefined;
  const pendingQuestion = runStatus === "running" && run?.sessionId === selectedSessionId
    ? run.pendingQuestions?.find(question => question.requestId === selectedQuestionId && question.question)
      || run.pendingQuestions?.find(question => question.requestId && question.question) : undefined;
  // A compact reconnect snapshot may arrive before the event replay. Keep its
  // pending question visible immediately, without duplicating replayed records.
  const questionTimeline = useMemo(() => runStatus === "running" && run?.sessionId === selectedSessionId
    ? (run.pendingQuestions || []).reduce(reduceRunEvent, timeline) : timeline,
    [timeline, runStatus, run?.sessionId, run?.pendingQuestions, selectedSessionId]);

  function disconnectRunStream() {
    streamBatchRef.current?.cancel(); streamBatchRef.current = null;
    setReconnecting(false);
    connectionGenerationRef.current += 1;
    sourceRef.current?.close();
    sourceRef.current = null;
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }

  function setSessionStatus(sessionId: string, status: RunStatus, reviewPending?: boolean) {
    setNavigation((current) => ({
      ...current,
      sessions: current.sessions.map((session) =>
        session.sessionId === sessionId
          ? { ...session, status, reviewPending: status === "failed" && (reviewPending ?? session.reviewPending ?? false), updatedAt: Date.now() }
          : session,
      ),
    }));
  }

  function useExtensionContribution(request: ExtensionUseRequest) {
    if (request.target === "settings") {
      navigatePage("settings");
      return;
    }
    showConversation();
    if (request.target === "task") {
      const nextPrompt = request.prompt?.trimEnd();
      if (nextPrompt) {
        setPrompt((current) => current.trim() ? `${current.trimEnd()}\n${nextPrompt}` : nextPrompt);
      }
      setComposerFocusRequest((current) => current + 1);
      return;
    }
    const tab: InspectorTab = request.target === "runtime" ? "run" : request.target;
    extensionUseRevisionRef.current += 1;
    setInspectorTabRequest({ tab, revision: extensionUseRevisionRef.current });
    setInspectorOpen(true);
  }

  useEffect(() => {
    inspectorWidthRef.current = inspectorWidth;
  }, [inspectorWidth]);

  useEffect(() => {
    try { window.localStorage.setItem("minicode.rail.collapsed", String(railCollapsed)); }
    catch { /* The sidebar still works without browser storage. */ }
  }, [railCollapsed]);

  function openRail() {
    setRailCollapsed(false);
    setRailOpen(window.matchMedia("(max-width: 819px)").matches);
  }

  function openInspectorTab(tab: InspectorTab, section?: ComposerInspectorSection) {
    if (tab === "agents") setInspectorWidth(current => clampInspectorWidth(Math.max(current, window.innerWidth * 0.4)));
    extensionUseRevisionRef.current += 1;
    setInspectorTabRequest({ tab, section, revision: extensionUseRevisionRef.current });
    setInspectorOpen(true);
  }

  function navigateComposerCommand(target: ComposerNavigation) {
    if (target.kind === "inspector") openInspectorTab(target.tab, target.section);
    else if (target.kind === "settings") navigatePage("settings");
    else if (target.kind === "extensions") {
      setExtensionView(target.view);
      navigatePage("extensions");
    } else {
      setInspectorOpen(false);
      openRail();
      if (target.search) setSearchRequest(current => current + 1);
    }
  }

  function openFileReference(reference: FileReference) {
    openInspectorTab("files");
    setFileNavigation({ workspace, sessionId: selectedSessionId, request: {
      ...reference, revision: extensionUseRevisionRef.current,
    } });
  }

  const refreshContextStats = useCallback(async () => {
    const sessionId = selectedSessionIdRef.current;
    if (!sessionId) return;
    const request = ++contextRequestRef.current;
    const selection = selectionRequestRef.current;
    const generation = connectionGenerationRef.current;
    const result = conversationViews ? await api.getContextStats(sessionId) : await api.getSessionInspect(sessionId);
    if (contextRequestRef.current !== request || selectionRequestRef.current !== selection
      || connectionGenerationRef.current !== generation || selectedSessionIdRef.current !== sessionId) return;
    const context = "inspect" in result ? result.inspect.contextStats : result.contextStats;
    // The lightweight endpoint has no snapshot before the first model request.
    // Keep any existing history estimate while waiting for native accounting.
    if (!Number.isFinite(context.contextWindow) || context.contextWindow <= 0) return;
    if ("inspect" in result) setSessionInspect(current => current ? { ...current, contextStats: context } : result.inspect);
    else setSessionInspect(current => current ? { ...current, contextStats: context } : current);
    return context;
  }, [conversationViews]);

  const contextRefresh = useContextRefresh({
    identity: `${selectedSessionId || ""}:${run?.runId || ""}`,
    enabled: Boolean(selectedSessionId) && !sessionLoading && (conversationViews || !sessionInspectLoading) && activePage === "conversation",
    active: isActive(runStatus), revision: contextRefreshRevision, onRefresh: refreshContextStats,
  });
  // A fresh run does not load the optional inspector. Its accounting must have
  // its own state rather than being discarded until inspection happens to load.
  const contextStats = sessionLoading ? null : contextRefresh.context ?? sessionInspect?.contextStats ?? null;

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
    let pending = false;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden" || pending) return;
      pending = true;
      void api.getNavigation().then(setNavigation).catch(() => undefined).finally(() => { pending = false; });
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [navigation.sessions]);

  useEffect(() => {
    if (conversationScope === "project" && !workspace) {
      setCapabilities(null);
      return;
    }
    let cancelled = false;
    const context = `${conversationScope}:${workspace}`;
    if (capabilitiesContextRef.current !== context) setCapabilities(null);
    capabilitiesContextRef.current = context;
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
  }, [capabilityRevision, conversationScope, workspace]);

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
        setConversationViews(payload.conversationViewVersion === 1);
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
      selectionAbortRef.current?.abort();
      disconnectRunStream();
    };
  }, []);

  useEffect(() => {
    if (booting || draftRestoredRef.current) return;
    draftRestoredRef.current = true;
    const target = draft.store.lastContext(true);
    if (!target) return;
    if (target.sessionId && navigation.sessions.some((session) => session.sessionId === target.sessionId)) {
      void selectSession(target.sessionId, true);
    } else if (!target.sessionId && target.scope === "project"
      && navigation.projects.some((project) => draftKey({ ...target, workspace: project.path }) === draftKey(target))) {
      void changeWorkspace(target.workspace, true).catch(() => undefined);
    }
  }, [booting, navigation]);

  useEffect(() => {
    if (!booting && !sessionLoading && draftRestoredRef.current && (draft.value || selectedSessionId)) {
      draft.store.remember({ sessionId: selectedSessionId, scope: conversationScope, workspace });
    }
  }, [booting, sessionLoading, draft.key]);

  useEffect(() => {
    const syncPage = () => {
      const page = pageFromHash();
      if (page === "conversation" || page === "trash") settingsReturnPage.current = page;
      else setSettingsSection(page === "extensions" ? "extensions" : "models");
      setActivePage(page);
      setRailOpen(false);
    };
    window.addEventListener("hashchange", syncPage);
    window.addEventListener("popstate", syncPage);
    return () => {
      window.removeEventListener("hashchange", syncPage);
      window.removeEventListener("popstate", syncPage);
    };
  }, []);

  useEffect(() => {
    if (!trashNotice) return;
    const timer = window.setTimeout(() => setTrashNotice(""), 8000);
    return () => window.clearTimeout(timer);
  }, [trashNotice]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (document.querySelector('dialog[open], [role="dialog"][aria-modal="true"]:not([aria-hidden="true"])')) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        newStandaloneTask();
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        const overlay = document.querySelector('[role="dialog"][aria-modal="true"]:not([aria-hidden="true"])');
        if (overlay) return;
        event.preventDefault();
        openRail();
        setSearchRequest((current) => current + 1);
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

  useEffect(() => {
    if (!selectedSessionId || sessionLoading) return;
    if (conversationViews) {
      const previous = historyRefreshRef.current;
      if (previous?.identity === selectedSessionId && previous.revision === historyRevision) return;
      historyRefreshRef.current = { identity: selectedSessionId, revision: historyRevision };
    }
    const selection = selectionRequestRef.current;
    let disposed = false;
    const isCurrent = () => !disposed && selectedSessionIdRef.current === selectedSessionId
      && selectionRequestRef.current === selection;
    const controller = new AbortController();
    void (conversationViews ? api.getConversationPage(selectedSessionId, undefined, controller.signal) : api.getTurns(selectedSessionId)).then((page) => {
      if (!isCurrent()) return;
      setHistory((current) => {
        if (!current) return page;
        if (conversationViews) return mergeConversationPage(current, page);
        if (page.revision && current.revision === page.revision) return current;
        const fresh = new Map(page.turns.map((turn) => [turn.turnId, turn]));
        const retained = current.turns.map((turn) => fresh.get(turn.turnId) || turn);
        const known = new Set(retained.map((turn) => turn.turnId));
        return { ...page, turns: [...retained, ...page.turns.filter((turn) => !known.has(turn.turnId))], nextBefore: current.nextBefore };
      });
      setHistoryError("");
    }).catch((cause) => { if (isCurrent()) setHistoryError(cause instanceof Error ? cause.message : "无法读取轮次快照。"); });
    return () => { disposed = true; controller.abort(); };
  }, [selectedSessionId, sessionLoading, runStatus, historyRevision, conversationViews]);

  useEffect(() => {
    if (conversationViews && selectedSessionId && selectedSummary && history && !sessionLoading) {
      conversationCache.current.set(selectedSessionId, { session: selectedSummary, history, inspect: sessionInspect });
    }
  }, [conversationViews, selectedSessionId, history, sessionInspect, sessionLoading]);

  useEffect(() => {
    if (!booting) conversationCache.current.retain(new Set(navigation.sessions.map(session => session.sessionId)));
  }, [booting, navigation.sessions]);

  function readEarlierPage(identity: string, before: number, selection: number, revision?: string) {
    const cached = earlierPageRef.current;
    if (cached?.selection === selection && cached.before === before && cached.revision === revision) return cached.request;
    const request = api.getConversationPage(identity, before, selectionAbortRef.current?.signal);
    const entry = { selection, before, revision, request };
    earlierPageRef.current = entry;
    void request.catch(() => { if (earlierPageRef.current === entry) earlierPageRef.current = null; });
    return request;
  }

  async function loadEarlierTurns() {
    if (!selectedSessionId || history?.nextBefore == null || historyLoading) return;
    const identity = selectedSessionId;
    const selection = selectionRequestRef.current;
    if (earlierRequestRef.current === selection) return;
    earlierRequestRef.current = selection;
    const isCurrent = () => selectedSessionIdRef.current === identity && selectionRequestRef.current === selection;
    setHistoryLoading(true); setHistoryError("");
    try {
      const page = conversationViews ? await readEarlierPage(identity, history.nextBefore, selection, history.revision)
        : await api.getTurns(identity, history.nextBefore);
      if (!isCurrent()) return;
      setHistory(current => conversationViews ? mergeConversationPage(current, page, true)
        : current ? { ...current, turns: [...page.turns, ...current.turns], nextBefore: page.nextBefore } : page);
      setHistoryError("");
    } catch (cause) { if (isCurrent()) setHistoryError(cause instanceof Error ? cause.message : "无法读取更早轮次。"); }
    finally {
      if (earlierRequestRef.current === selection) earlierRequestRef.current = null;
      if (isCurrent()) setHistoryLoading(false);
    }
  }

  function storeProcessPage(turnId: string, page: TurnMessagesPage, detail = false, revision?: string) {
    setHistory(current => current ? { ...current, turns: current.turns.map(turn => {
      if (turn.turnId !== turnId || revision !== undefined && revision !== turn.displayRevision) return turn;
      return { ...turn, processMessages: mergeProcessMessages(turn.processMessages || [], page.messages),
        ...(detail ? {} : { nextProcessAfter: page.nextAfter, processTotal: page.total }) };
    }) } : current);
  }

  function loadProcessPage(turn: SessionTurn, after: number): Promise<void> {
    const identity = selectedSessionIdRef.current;
    if (!identity) return Promise.reject(new Error("对话已切换。"));
    const selection = selectionRequestRef.current;
    const signal = selectionAbortRef.current?.signal;
    const key = JSON.stringify([selection, turn.turnId, turn.displayRevision, after]);
    const existing = processPageRequests.current.get(key);
    if (existing) return existing;
    const request = api.getTurnMessages(identity, turn.turnId, after, signal).then(page => {
      if (!signal?.aborted && selectedSessionIdRef.current === identity && selectionRequestRef.current === selection) {
        storeProcessPage(turn.turnId, page, false, turn.displayRevision);
      }
    }).finally(() => { processPageRequests.current.delete(key); });
    processPageRequests.current.set(key, request);
    return request;
  }

  useEffect(() => {
    if (!conversationViews || !selectedSessionId || sessionLoading || sessionSyncing || !history || historyError || activePage !== "conversation") return;
    const identity = selectedSessionId;
    const selection = selectionRequestRef.current;
    // Warm at most one older page and one recent process page, after the body is visible.
    const recent = [...history.turns].reverse().find(turn => turn.status !== "running" && turn.processAvailable);
    const timer = window.setTimeout(() => {
      if (document.hidden || selectionRequestRef.current !== selection) return;
      if (history.nextBefore != null) void readEarlierPage(identity, history.nextBefore, selection, history.revision).catch(() => undefined);
      if (recent && !recent.processMessages) void loadProcessPage(recent, 0).catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [conversationViews, selectedSessionId, sessionLoading, sessionSyncing, history, historyError, activePage]);

  function navigatePage(page: AppPage) {
    const modal = page === "settings" || page === "extensions";
    if (!modal) settingsReturnPage.current = page;
    else setSettingsSection(page === "extensions" ? "extensions" : "models");
    const hash = page === "conversation" ? "" : `#${page}`;
    if (window.location.hash !== hash) {
      const url = window.location.pathname + window.location.search + hash;
      if (modal && settingsOpen) window.history.replaceState(window.history.state, "", url);
      else window.history.pushState(modal ? { minicodeSettingsDialog: true } : null, "", url);
    }
    setActivePage(page);
    setRailOpen(false);
  }

  function closeSettings() {
    if (window.history.state?.minicodeSettingsDialog) window.history.back();
    else {
      const page = settingsReturnPage.current;
      window.history.replaceState(null, "", window.location.pathname + window.location.search + (page === "trash" ? "#trash" : ""));
      setActivePage(page);
    }
  }

  function changeSettingsSection(section: SettingsSection) {
    navigatePage(section === "extensions" ? "extensions" : "settings");
    setSettingsSection(section);
  }

  function showConversation() {
    navigatePage("conversation");
  }

  function openTrash() {
    setTrashNotice("");
    navigatePage("trash");
  }

  function resetTask(scope: ConversationScope, preserveView = false) {
    saveCurrentReadingView();
    selectionAbortRef.current?.abort();
    earlierPageRef.current = null;
    setSessionSyncing(false); setRestoredView(undefined);
    setComposerNotice("");
    setHistory(null); setHistoryError(""); setHistoryLoading(false); setForkTurn(null); setEnvironment("local");
    if (!preserveView) showConversation();
    selectionRequestRef.current += 1;
    disconnectRunStream();
    setConversationScope(scope);
    conversationScopeRef.current = scope;
    setSelectedSessionId(null);
    selectedSessionIdRef.current = null;
    setTimeline([]);
    setEvents([]);
    setSessionInspect(null);
    setSessionInspectLoading(false);
    setRun(null);
    setRunStatus("idle");
    setSessionLoading(false);
    setComposerFocusRequest((current) => current + 1);
  }

  function newStandaloneTask() {
    resetTask("standalone");
  }

  async function selectSession(sessionId: string, preserveView = false) {
    if (conversationViews) return selectConversationView(sessionId, preserveView);
    setComposerNotice("");
    setHistory(null); setHistoryError(""); setHistoryLoading(false); setForkTurn(null);
    setHistoryRevision((current) => current + 1);
    if (!preserveView) showConversation();
    const requestId = selectionRequestRef.current + 1;
    selectionRequestRef.current = requestId;
    disconnectRunStream();
    setError(null);
    setSessionLoading(true);
    setSessionInspectLoading(true);
    setSelectedSessionId(sessionId);
    selectedSessionIdRef.current = sessionId;
    setTimeline([]);
    setEvents([]);
    setSessionInspect(null);
    setRun(null);
    setRunStatus("idle");
    try {
      const summary = navigation.sessions.find((item) => item.sessionId === sessionId) || (await api.getSession(sessionId)).session;
      if (summary.worktreeId) {
        const status = summary.worktreeStatus === "ready" ? await api.workspaceStatus(summary.workspace).catch(() => null) : null;
        if (selectionRequestRef.current !== requestId) return;
        setWorkspace(summary.workspace); workspaceRef.current = summary.workspace;
        setWorkspaceStatus(status); setSelectedFile(status?.files[0]?.path || null); setDiff("");
      } else if (summary.scope === "project" && summary.projectPath && summary.projectPath !== workspace) {
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
      // Inspector metadata is optional; it must not hold the conversation loader.
      void api.getSessionInspect(sessionId).then(inspectResult => {
        if (selectionRequestRef.current === requestId && selectedSessionIdRef.current === sessionId) {
          setSessionInspect(inspectResult.inspect);
        }
      }).catch(() => undefined).finally(() => {
        if (selectionRequestRef.current === requestId) setSessionInspectLoading(false);
      });
      const [result, runSnapshot] = await Promise.all([
        api.getSession(sessionId),
        api.getSessionRun(sessionId).catch((): RunSnapshot => ({ run: null, events: [] })),
      ]);
      if (selectionRequestRef.current !== requestId) return;
      setConversationScope(summary.scope);
      conversationScopeRef.current = summary.scope;
      setSelectedSessionId(sessionId);
      selectedSessionIdRef.current = sessionId;
      const history = historyToTimeline(runSnapshot.historyMessages || result.messages);
      const historySequence = runSnapshot.run?.eventHistoryAvailable ? 0
        : runSnapshot.historyMessages ? runSnapshot.run?.historySequence || 0 : 0;
      const restoredEvents = [
        ...(runSnapshot.hasMoreEvents ? [] : runSnapshot.run?.pendingApprovals || []).filter((pending) => !runSnapshot.events.some((event) => event.sequence === pending.sequence && event.sequence > historySequence)),
        ...runSnapshot.events.filter((event) => event.sequence > historySequence),
      ];
      const restoredTimeline = runSnapshot.run && (
        isActive(runSnapshot.run.status)
        || runSnapshot.run.status === "stopped"
        || runSnapshot.run.eventHistoryAvailable
      )
        ? appendRunImages(timelineWithEvents(history, restoredEvents), runSnapshot.run.images)
        : history;
      setTimeline(restoredTimeline);
      setEvents(runSnapshot.events.slice(-MAX_RETAINED_RUN_EVENTS));
      setRun(runSnapshot.run);
      const restoredStatus = runSnapshot.run?.status || statusFromSession(result.session.status);
      setRunStatus(restoredStatus);
      setSessionStatus(sessionId, restoredStatus, runSnapshot.run?.reviewPending ?? result.session.reviewPending ?? false);
      if (runSnapshot.run && (isActive(runSnapshot.run.status) || runSnapshot.hasMoreEvents)) {
        connectRun(runSnapshot.run, runSnapshot.events);
      }
    } catch (reason) {
      if (selectionRequestRef.current === requestId) {
        setError(reason instanceof Error ? reason.message : "无法读取会话。");
        setSelectedSessionId(null);
        selectedSessionIdRef.current = null;
        setHistory(null); setHistoryError(""); setHistoryLoading(false);
        setSessionInspect(null);
        setSessionInspectLoading(false);
      }
    } finally {
      if (selectionRequestRef.current === requestId) {
        setSessionLoading(false);
      }
    }
  }

  async function selectConversationView(sessionId: string, preserveView: boolean) {
    saveCurrentReadingView();
    selectionAbortRef.current?.abort();
    earlierPageRef.current = null;
    const controller = new AbortController();
    selectionAbortRef.current = controller;
    const selection = ++selectionRequestRef.current;
    const isCurrent = () => selectionRequestRef.current === selection && !controller.signal.aborted;
    disconnectRunStream();
    if (!preserveView) showConversation();
    const cached = conversationCache.current.get(sessionId);
    const summary = navigation.sessions.find(item => item.sessionId === sessionId) || cached?.session;
    historyRefreshRef.current = { identity: sessionId, revision: historyRevision };
    setComposerNotice(""); setError(null); setForkTurn(null);
    setSelectedSessionId(sessionId); selectedSessionIdRef.current = sessionId;
    setHistory(cached?.history || null); setHistoryError(""); setHistoryLoading(false);
    setRestoredView(cached?.view);
    setTimeline([]); setEvents([]); setRun(null);
    setRunStatus(summary ? statusFromSession(summary.status) : "idle");
    setSessionInspect(cached?.inspect || null); setSessionInspectLoading(!cached?.inspect);
    setSessionLoading(!cached); setSessionSyncing(true);
    const applyWorkspace = (target: SessionSummary) => {
      setConversationScope(target.scope); conversationScopeRef.current = target.scope;
      if (target.scope === "project") {
        const path = target.worktreeId ? target.workspace : target.projectPath || target.workspace;
        if (workspaceRef.current !== path) {
          setWorkspace(path); workspaceRef.current = path;
          setWorkspaceStatus(null); setSelectedFile(null); setDiff("");
        }
      }
    };
    if (summary) applyWorkspace(summary);
    // Start both reads immediately. Rendering the delivery does not wait for run restoration.
    const snapshotPromise = api.getSessionRun(sessionId, controller.signal, true)
      .then(value => ({ value, error: null })).catch(error => ({ value: null, error }));
    try {
      const page = await api.getConversationPage(sessionId, undefined, controller.signal);
      if (!isCurrent()) return;
      applyWorkspace(page.session);
      setNavigation(current => ({ ...current, sessions: current.sessions.some(item => item.sessionId === sessionId)
        ? current.sessions.map(item => item.sessionId === sessionId ? page.session : item)
        : [page.session, ...current.sessions] }));
      setHistory(current => mergeConversationPage(current, page));
      setSessionLoading(false);
      const restored = await snapshotPromise;
      if (!isCurrent()) return;
      if (restored.error || !restored.value) throw restored.error || new Error("无法同步运行状态，请重新打开对话。");
      let snapshot = restored.value;
      const status = snapshot.run?.status || statusFromSession(page.session.status);
      const latestTurn = page.turns.at(-1);
      const savedProcess = !isActive(status) && latestTurn?.status === status && latestTurn.processAvailable;
      // Completed snapshots include full tool outputs and preserve the same
      // reading surface while the optional run metadata arrives.
      if (!isActive(status) && !savedProcess && snapshot.run?.eventHistoryAvailable) {
        snapshot = await api.getRun(snapshot.run.runId);
        if (!isCurrent()) return;
      }
      setRun(snapshot.run); setRunStatus(status); setSessionStatus(sessionId, status, snapshot.run?.reviewPending ?? page.session.reviewPending ?? false);
      setEvents(snapshot.events.slice(-MAX_RETAINED_RUN_EVENTS));
      if (snapshot.run && (isActive(status) || snapshot.run.eventHistoryAvailable && !savedProcess)) {
        const base = historyToTimeline(snapshot.historyMessages || []);
        const watermark = snapshot.run.eventHistoryAvailable ? 0 : snapshot.historyMessages ? snapshot.run.historySequence || 0 : 0;
        const restoredEvents = [...(snapshot.hasMoreEvents ? [] : snapshot.run.pendingApprovals || []).filter(pending =>
          !snapshot.events.some(event => event.sequence === pending.sequence && event.sequence > watermark)),
          ...snapshot.events.filter(event => event.sequence > watermark)];
        setTimeline(appendRunImages(timelineWithEvents(base, restoredEvents), snapshot.run.images));
        if (isActive(status) || snapshot.hasMoreEvents) connectRun(snapshot.run, snapshot.events);
      }
      setSessionSyncing(false);
      // Optional I/O starts after the first body commit and never blocks the delivery.
      window.requestAnimationFrame(() => {
        if (!isCurrent()) return;
        void api.getSessionInspect(sessionId, controller.signal).then(result => {
          if (isCurrent()) setSessionInspect(result.inspect);
        }).catch(() => undefined).finally(() => { if (isCurrent()) setSessionInspectLoading(false); });
        if (page.session.scope === "project") {
          const path = page.session.worktreeId ? page.session.workspace : page.session.projectPath || page.session.workspace;
          const statusRequest = api.workspaceStatus(path);
          void statusRequest.then(next => {
            if (!isCurrent()) return;
            setWorkspaceStatus(next); setSelectedFile(next.files[0]?.path || null);
          }).catch(() => undefined);
        }
      });
    } catch (cause) {
      if (isCurrent()) {
        setError(cause instanceof Error ? cause.message : "无法读取对话，请重新打开。");
        setSessionLoading(false); setSessionInspectLoading(false);
        // Keep cached reading available; submission waits for authoritative run state.
      }
    }
  }

  async function archiveSelectedSession(sessionId: string) {
    if (archivingRef.current) return;
    const session = navigation.sessions.find((item) => item.sessionId === sessionId);
    archivingRef.current = true;
    setArchivingSessionId(sessionId);
    setError(null);
    try {
      await api.archiveSession(sessionId);
      conversationCache.current.delete(sessionId);
      if (selectedSessionIdRef.current === sessionId) resetTask(session?.scope || conversationScopeRef.current, true);
      setTrashRevision((current) => current + 1);
      setRailOpen(false);
      setNavigation((current) => ({ ...current,
        sessions: current.sessions.filter((item) => item.sessionId !== sessionId),
        pinnedSessionIds: current.pinnedSessionIds.filter((id) => id !== sessionId),
      }));
      setTrashNotice("对话已移入回收站，可随时恢复。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "移入回收站失败，请重试。");
    } finally {
      archivingRef.current = false;
      setArchivingSessionId(null);
    }
  }

  function saveCurrentReadingView() {
    const identity = selectedSessionIdRef.current;
    const view = captureViewRef.current?.();
    if (identity && view) conversationCache.current.saveView(identity, view);
  }

  function carryDraft(sourceKey: string, scope: ConversationScope, path: string) {
    const target = { sessionId: null, scope, workspace: path };
    const previous = draft.store.read(draftKey(target));
    const carried = draft.store.carry(sourceKey, target);
    draft.store.write(draftKey(target), carried.value, carried.attachments);
    draft.store.remember(target);
    const notices = [];
    if (carried.removed) notices.push("原项目的文件引用已移除；文字和本地附件已保留，原草稿仍可在原项目中找到。");
    if (previous.value && carried.value !== previous.value && carried.value !== draft.store.read(sourceKey).value) {
      notices.push("目标项目已有草稿，已一并保留在输入框中。");
    }
    setComposerNotice(notices.join(" "));
  }

  async function switchDraftProject(path: string | null) {
    if (selectedSessionIdRef.current || sessionLoading || startInFlightRef.current || isActive(runStatus)) return;
    const target = { sessionId: null, scope: path === null ? "standalone" as const : "project" as const, workspace: path || workspace };
    if (draftKey(target) === draft.key) return;
    // Check capacity before changing the visible workspace. Never overwrite another draft.
    const carried = draft.store.carry(draft.key, target);
    composeMessage(carried.value, carried.attachments);
    if (path === null) { resetTask("standalone"); carryDraft(draft.key, "standalone", workspace); }
    else await changeWorkspace(path, false, draft.key);
  }

  async function pickDraftProject() {
    const request = selectionRequestRef.current;
    const result = await api.pickWorkspace(workspace);
    if (result.cancelled || request !== selectionRequestRef.current || selectedSessionIdRef.current || startInFlightRef.current) return;
    await switchDraftProject(result.workspace);
  }

  async function changeWorkspace(path: string, preserveView = false, sourceDraftKey?: string) {
    if (!preserveView) showConversation();
    // Starting another task in an already-open project is a local view change.
    // Git status may refresh in the background; the global session list has not changed.
    if (workspaceStatus && sameWorkspacePath(path, workspaceRef.current)) {
      setError(null);
      resetTask("project", preserveView);
      if (sourceDraftKey) carryDraft(sourceDraftKey, "project", path);
      const requestId = selectionRequestRef.current;
      const currentWorkspace = workspaceRef.current;
      void api.workspaceStatus(currentWorkspace).then((status) => {
        if (selectionRequestRef.current !== requestId || selectedSessionIdRef.current !== null) return;
        setWorkspaceStatus(status);
        setSelectedFile((current) => status.files.some((file) => file.path === current) ? current : status.files[0]?.path || null);
      }).catch((reason) => {
        if (selectionRequestRef.current === requestId && selectedSessionIdRef.current === null) {
          setError(reason instanceof Error ? reason.message : "暂时无法刷新项目状态。");
        }
      });
      return;
    }

    let requestId = selectionRequestRef.current + 1;
    selectionRequestRef.current = requestId;
    setError(null);
    setSessionLoading(true);
    try {
      const result = await api.openWorkspace(path);
      if (selectionRequestRef.current !== requestId) return;
      resetTask("project", preserveView);
      if (sourceDraftKey) carryDraft(sourceDraftKey, "project", result.workspace);
      requestId = selectionRequestRef.current;
      setWorkspace(result.workspace);
      workspaceRef.current = result.workspace;
      setWorkspaceStatus(result.workspaceStatus);
      setNavigation((current) => current.projects.some((project) => sameWorkspacePath(project.path, result.workspace))
        ? current
        : { ...current, projects: [{ name: workspaceName(result.workspace), path: result.workspace }, ...current.projects] });
      setSelectedFile(result.workspaceStatus.files[0]?.path || null);
      setDiff("");
      // A slow history scan must not delay typing in the newly opened project.
      void api.getNavigation().then((nextNavigation) => {
        if (selectionRequestRef.current === requestId && selectedSessionIdRef.current === null) setNavigation(nextNavigation);
      }).catch(() => undefined);
    } catch (reason) {
      if (selectionRequestRef.current !== requestId) return;
      const message = reason instanceof Error ? reason.message : "无法打开工作区。";
      setError(message);
      // The previous conversation remains selected when opening the project fails.
      setHistoryLoading(false);
      setHistoryRevision((current) => current + 1);
      throw reason;
    } finally {
      if (selectionRequestRef.current === requestId) setSessionLoading(false);
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

  async function renameNavigationItem(kind: "workspace" | "session", target: string, name: string) {
    const result = await api.renameNavigationItem(kind, target, name);
    setNavigation(current => ({
      ...current,
      projects: current.projects.map(project => {
        const renamed = result.projects.find(item => sameWorkspacePath(item.path, project.path));
        return renamed ? { ...project, name: renamed.name } : project;
      }),
      sessions: current.sessions.map(session => {
        const renamed = result.sessions.find(item => item.sessionId === session.sessionId);
        return renamed ? { ...session, title: renamed.title } : session;
      }),
    }));
  }

  async function closeProject(path: string) {
    if (closingProjectRef.current) return;
    closingProjectRef.current = true;
    setClosingProjectPath(path);
    setError(null);
    const name = workspaceName(path);
    try {
      const result = await api.closeProject(path);
      setRailNotice(`项目“${name}”已从左侧列表关闭。对话与文件均未被删除。`);
      // The active project was closed, so the rail authority has already moved.
      if (result.activeWorkspaceChanged && sameWorkspacePath(workspaceRef.current, result.closedWorkspace)) {
        disconnectRunStream();
        setWorkspace(result.workspace);
        workspaceRef.current = result.workspace;
        setWorkspaceStatus(null);
        setSelectedFile(null);
        setDiff("");
        resetTask("standalone");
      }
      void api.getNavigation().then(setNavigation).catch(() => undefined);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法关闭项目。");
    } finally {
      closingProjectRef.current = false;
      setClosingProjectPath(null);
    }
  }

  function connectRun(nextRun: RunInfo, initialEvents: RunEvent[] = []) {
    disconnectRunStream();
    setTimeline(current => appendRunImages(current, nextRun.images));
    const generation = connectionGenerationRef.current;
    lastEventSequenceRef.current = Math.max(
      0,
      ...initialEvents.map((event) => event.sequence),
    );
    let terminalSeen = false;
    let restoredStream = Boolean(nextRun.restored);
    let replayThrough = nextRun.eventHistoryAvailable ? nextRun.eventCount : 0;
    let pendingTerminal = isActive(nextRun.status) ? null : nextRun.status;

    function isCurrentConnection() {
      return connectionGenerationRef.current === generation
        && selectedSessionIdRef.current === nextRun.sessionId;
    }

    const batch = new RunEventBatch(incoming => {
      if (!isCurrentConnection()) return;
      setRun(current => current?.runId === nextRun.runId
        ? incoming.filter(event => event.sequence > replayThrough).reduce(updateRunActivity, current) : current);
      setEvents(current => [...current, ...incoming].slice(-MAX_RETAINED_RUN_EVENTS));
      setTimeline(current => incoming.reduce(reduceRunEvent, current));
    });
    streamBatchRef.current = batch;

    function finishRun(status: RunStatus) {
      if (conversationViews) setHistoryRevision(value => value + 1);
      contextRequestRef.current += 1;
      setReconnecting(false);
      terminalSeen = true;
      sourceRef.current?.close();
      sourceRef.current = null;
      setRunStatus(status);
      setRun((current) => current?.runId === nextRun.runId ? { ...current, status } : current);
      setSessionStatus(nextRun.sessionId, status);
      setSessionInspectLoading(true);
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
          .catch(() => undefined)
          .finally(() => { if (isCurrentConnection()) setSessionInspectLoading(false); });
      }, 250);
    }

    function applyEvent(event: RunEvent) {
      if (
        !isCurrentConnection()
        || !Number.isSafeInteger(event.sequence)
        || event.sequence <= lastEventSequenceRef.current
      ) return;
      lastEventSequenceRef.current = event.sequence;
      batch.push(event);
      if (event.sequence > replayThrough && isContextRefreshEvent(event)) setContextRefreshRevision(value => value + 1);
      if (event.sequence > replayThrough && event.kind === "turn_saved") setHistoryRevision((value) => value + 1);
      if (event.kind === "capability_snapshot" && event.capabilities) {
        setCapabilities(event.capabilities);
      }
      if (event.kind === "status" && event.status && event.sequence >= replayThrough) {
        setRunStatus(event.status);
        setRun((current) => current?.runId === nextRun.runId
          ? { ...current, status: event.status || current.status, reviewPending: event.reviewPending === true, updatedAt: event.timestamp }
          : current);
        setSessionStatus(nextRun.sessionId, event.status, event.reviewPending);
      }
      const terminal = terminalStatus(event);
      if (terminal) pendingTerminal = terminal;
      if (pendingTerminal && event.sequence >= replayThrough) finishRun(pendingTerminal);
    }

    function subscribe() {
      if (!isCurrentConnection() || terminalSeen) return;
      const after = lastEventSequenceRef.current;
      const source = api.eventSource(nextRun.runId, after);
      sourceRef.current = source;
      source.onopen = () => {
        if (isCurrentConnection()) {
          setReconnecting(false);
          setContextRefreshRevision(value => value + 1);
          setError((current) => current === RECONNECT_ERROR ? null : current);
        }
      };
      source.onmessage = (message) => applyEvent(parseRunEvent(message));
      source.onerror = () => {
        source.close();
        if (sourceRef.current === source) sourceRef.current = null;
        if (!isCurrentConnection() || terminalSeen) return;
        setReconnecting(true);
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
            let snapshot;
            try {
              snapshot = await api.getRun(nextRun.runId, after);
            } catch (cause) {
              if (!(cause instanceof ApiError) || cause.status !== 404) throw cause;
              snapshot = await api.getSessionRun(nextRun.sessionId);
            }
            if (!isCurrentConnection()) return;
            if (!snapshot.run) {
              finishRun("stopped");
              setError("未找到原任务的运行记录，已停止重连。已保存的对话仍可查看，可重新提交任务。");
              return;
            }
            if (snapshot.run.runId !== nextRun.runId) {
              batch.flush();
              setRun(snapshot.run);
              setEvents(snapshot.events);
              if (snapshot.run.eventHistoryAvailable) setTimeline(current => timelineWithEvents(current, snapshot.events));
              setHistoryRevision(value => value + 1);
              connectRun(snapshot.run, snapshot.events);
              return;
            }
            if (snapshot.run.restored && !restoredStream) {
              batch.flush();
              restoredStream = true;
              if (!snapshot.run.eventHistoryAvailable) lastEventSequenceRef.current = 0;
              setHistoryRevision(value => value + 1);
            }
            if (snapshot.run.eventHistoryAvailable) replayThrough = Math.max(replayThrough, snapshot.run.eventCount);
            [...snapshot.events]
              .sort((left, right) => left.sequence - right.sequence)
              .forEach(applyEvent);
            batch.flush();
            setTimeline(current => appendRunImages(current, snapshot.run!.images));
            setRun(snapshot.run);
            setRunStatus(snapshot.run.status);
            setSessionStatus(nextRun.sessionId, snapshot.run.status, snapshot.run.reviewPending ?? false);
            setError((current) => current === RECONNECT_ERROR ? null : current);
            if ((isActive(snapshot.run.status) || snapshot.hasMoreEvents) && !terminalSeen) {
              // A new host cannot attach to the old process's event stream.
              // Poll its ownership state until cancellation has really settled.
              if (snapshot.run.restored && !snapshot.hasMoreEvents) scheduleReconnect(1_500);
              else subscribe();
            }
            else if (!terminalSeen) finishRun(snapshot.run.status);
          } catch (cause) {
            if (isCurrentConnection()) {
              if (cause instanceof ApiError && [403, 404, 409, 410].includes(cause.status)) {
                finishRun("stopped");
                setError(cause.message);
                return;
              }
              setError(RECONNECT_ERROR);
              scheduleReconnect(1_500);
            }
          }
        }, delay);
    }

    if (isActive(nextRun.status) || lastEventSequenceRef.current < replayThrough) {
      if (nextRun.restored) scheduleReconnect(700);
      else subscribe();
    }
    else finishRun(nextRun.status);
  }

  async function sendRunInput(kind: RunInput["kind"]) {
    if (!run || !isActive(runStatus) || inputBusyRef.current || sessionLoading || sessionSyncing) return;
    const submittedKey = draft.key;
    const entry = draft.store.read(submittedKey);
    if (!entry.value.trim() && !entry.attachments.length) return;
    const target = run;
    const profile = kind !== "next_task" ? actualProfile || target.executionProfile : executionProfile;
    let selectedContext;
    try { selectedContext = kind === "next_task" ? contextSelection.selection() : undefined; }
    catch (cause) { setError((cause as Error).message); return; }
    const submissionKey = `${target.runId}:${submittedKey}:${entry.revision}:${kind}:${profile}:${JSON.stringify(selectedContext)}`;
    if (inputSubmissionRef.current?.key !== submissionKey) inputSubmissionRef.current = { key: submissionKey, id: crypto.randomUUID() };
    inputBusyRef.current = true;
    setInputBusy(true);
    setError(null);
    try {
      const result = await api.addRunInput(target.runId, { inputId: inputSubmissionRef.current.id, kind, text: composeMessage(entry.value, entry.attachments), executionProfile: profile,
        ...(target.queueProtocol === 1 ? { stage: true } : {}),
        ...(selectedContext ? { contextSettings: selectedContext } : {}) });
      draft.consume(submittedKey, entry.revision);
      setRun((current) => current?.runId === target.runId ? { ...current, inputs: mergeRunInput(current.inputs || [], result.input) } : current);
    } catch (cause) {
      if (selectedSessionIdRef.current === target.sessionId) setError(cause instanceof Error ? cause.message : "消息未确认接收，草稿仍保留。请重试。");
    } finally {
      inputBusyRef.current = false;
      setInputBusy(false);
    }
  }

  async function restoreRunInput(item: RunInput) {
    if (!run || inputBusyRef.current || sessionSyncing) return;
    const target = run;
    const targetKey = draft.key;
    inputBusyRef.current = true;
    setInputBusy(true);
    try {
      const restored = parseMessage(item.text);
      mergeAttachments(draft.store.read(targetKey).attachments, restored.attachments);
      const result = await api.cancelRunInput(target.runId, item.inputId);
      // Restore after withdrawal succeeds. Preserve any newer draft and the
      // originating conversation if the user switches while awaiting HTTP.
      const currentDraft = draft.store.read(targetKey);
      try {
        draft.store.write(targetKey, currentDraft.value ? `${currentDraft.value}\n\n${restored.text}` : restored.text,
          mergeAttachments(currentDraft.attachments, restored.attachments));
      } catch {
        // A newer draft may have filled the attachment limit while withdrawal was pending.
        // Keep the complete withdrawn message in the editor in that case.
        draft.store.write(targetKey, currentDraft.value ? `${currentDraft.value}\n\n${item.text}` : item.text);
      }
      setRun((current) => current?.runId === target.runId ? { ...current, inputs: mergeRunInput(current.inputs || [], result.input) } : current);
      if (selectedSessionIdRef.current === target.sessionId) setComposerFocusRequest((value) => value + 1);
    } catch (cause) {
      if (selectedSessionIdRef.current === target.sessionId) setError(cause instanceof Error ? cause.message : "无法撤回这条消息。");
    } finally { inputBusyRef.current = false; setInputBusy(false); }
  }

  async function changeQueuedInput(item: RunInput, action: "steer" | "delete") {
    if (!run || inputBusyRef.current || sessionLoading || sessionSyncing) return;
    const target = run;
    inputBusyRef.current = true;
    setInputBusy(true);
    setError(null);
    try {
      const result = action === "steer" ? await api.steerRunInput(target.runId, item.inputId)
        : await api.cancelRunInput(target.runId, item.inputId);
      setRun(current => current?.runId === target.runId ? { ...current,
        inputs: mergeRunInput(current.inputs || [], result.input) } : current);
    } catch (cause) {
      if (selectedSessionIdRef.current === target.sessionId) setError(cause instanceof Error ? cause.message : "队列操作未确认，请刷新后重试。");
    } finally { inputBusyRef.current = false; setInputBusy(false); }
  }

  async function startRun(promptOverride?: string, continuation?: { runId: string; artifactId: string }, compact = false, queuedFrom?: RunInfo) {
    const submittedDraftKey = draft.key;
    const submittedDraft = draft.store.read(submittedDraftKey);
    const queuedInput = queuedFrom?.inputs?.find(item => item.kind !== "steer" && ["accepted", "held"].includes(item.status));
    const submittedProfile = queuedInput?.executionProfile || executionProfile;
    let cleanPrompt = (queuedInput?.text ?? promptOverride ?? submittedDraft.value).trim();
    if (
      (!cleanPrompt && !submittedDraft.attachments.length)
      || startInFlightRef.current
      || sessionLoading
      || sessionSyncing
      || (conversationScope === "project" && !workspace)
      || isActive(runStatus)
    ) return;
    if (compact) {
      if (!selectedSessionId || submittedDraft.attachments.length) {
        setError(!selectedSessionId ? "请先打开已有对话再整理上下文。" : "请先发送或移除附件，再整理上下文。");
        return;
      }
      cleanPrompt = CONTEXT_ROTATION_REQUEST;
    }
    const submittedTask = taskFile.draft;
    const submittedBudget = runBudget.draft;
    let selectedTask;
    let selectedBudget;
    let selectedContext;
    const selectedLocation: ExecutionLocation = continuation ? "isolated" : executionLocation;
    try {
      if (conversationScope === "project") {
        const error = executionLocationError(settings, selectedLocation, submittedProfile);
        if (error) throw new Error(error);
      }
      selectedContext = queuedInput?.contextSettings || contextSelection.selection();
      if (!queuedFrom) cleanPrompt = composeMessage(cleanPrompt, promptOverride === undefined ? submittedDraft.attachments : []);
      selectedTask = !compact && conversationScope === "project" ? taskFileSelection(submittedTask) : undefined;
      selectedBudget = runBudgetSelection(submittedBudget, settings?.runBudgetSupported === true);
      if (selectedBudget?.unlimited && settings?.runUnlimitedBudgetSupported !== true) {
        throw new Error("当前后端不支持不限额执行，请升级并重启本机服务后重试。");
      }
      if (continuation && !selectedBudget) {
        selectedBudget = { maxTokens: settings?.runtimeMaxTokens ?? settings?.webRuntimePolicy?.defaultMaxTokens,
          durationSeconds: settings?.runtimeDeadline ? Math.max(0, settings.runtimeDeadline - Date.now() / 1000)
            : settings?.webRuntimePolicy?.defaultDurationSeconds };
        if (!selectedBudget.maxTokens || !selectedBudget.durationSeconds) throw new Error("请先在任务预算中填写本轮上限和时长，再继续。");
      }
    } catch (error) {
      setError(error instanceof Error ? error.message : "请重新确认任务契约。");
      return;
    }
    startInFlightRef.current = true;
    setStartingRun(true);
    const selectionRequest = selectionRequestRef.current;
    const submittedAt = Date.now();
    setError(null);
    // A pending HTTP submission is not a new run. Preserve the previous
    // receipt and retry contract until the backend acknowledges acceptance.
    try {
      let targetSession = selectedSessionId;
      let targetWorkspace = workspace;
      if (!targetSession && conversationScope === "project" && environment === "worktree") {
        if (worktreeCreateRef.current?.workspace !== workspace) {
          worktreeCreateRef.current = { workspace, requestId: crypto.randomUUID() };
        }
        const operation = await waitForWorktree((await api.createWorktreeSession(workspace, worktreeCreateRef.current.requestId)).operation);
        targetSession = operation.sessionId || operation.result?.sessionId || null;
        targetWorkspace = operation.result?.workspace || workspace;
        if (!targetSession) throw new Error("工作树会话创建结果缺失。");
        if (selectedContext) await api.saveContextSettings(targetSession, selectedContext);
        worktreeCreateRef.current = null;
        profileSelection.bindSession(submittedDraftKey,
          draftKey({ sessionId: targetSession, scope: conversationScope, workspace: targetWorkspace }), executionProfile, true);
        permissionSelection.bindSession(submittedDraftKey,
          draftKey({ sessionId: targetSession, scope: conversationScope, workspace: targetWorkspace }), permissionMode, true);
        if (selectedContext) contextSelection.consumeDraft(submittedDraftKey, selectedContext);
        runBudget.bindSession(submittedBudget,
          draftKey({ sessionId: targetSession, scope: conversationScope, workspace: targetWorkspace }), true);
        if (selectionRequestRef.current === selectionRequest) {
          // Keep the prepared child selected if starting its model run fails.
          // A retry must reuse this environment and its original project binding.
          setSelectedSessionId(targetSession); selectedSessionIdRef.current = targetSession;
          setExecutionLocations(current => ({ ...current,
            [draftKey({ sessionId: targetSession, scope: conversationScope, workspace: targetWorkspace })]: selectedLocation }));
          setWorkspace(targetWorkspace); workspaceRef.current = targetWorkspace;
          void api.getNavigation().then(setNavigation).catch(() => undefined);
          setWorkspaceStatus(await api.workspaceStatus(targetWorkspace));
        }
      }
      const result = await api.startRun({
        scope: conversationScope,
        ...(conversationScope === "project" ? { workspace: targetWorkspace } : {}),
        prompt: cleanPrompt,
        executionProfile: submittedProfile,
        ...(settings?.permissionModeSupported ? { permissionMode } : {}),
        ...(conversationScope === "project" ? { executionLocation: selectedLocation } : {}),
        ...(selectedContext ? { contextSettings: selectedContext } : {}),
        ...(selectedTask ? { taskFile: selectedTask } : {}),
        ...(selectedBudget ? { runBudget: selectedBudget } : {}),
        ...(continuation ? { continuation } : {}),
        ...(queuedFrom ? { resumeQueueFrom: queuedFrom.runId } : {}),
        ...(targetSession ? { sessionId: targetSession } : {}),
      });
      profileSelection.bindSession(submittedDraftKey,
        draftKey({ sessionId: result.run.sessionId, scope: conversationScope, workspace: targetWorkspace }), executionProfile, !selectedSessionId);
      permissionSelection.bindSession(submittedDraftKey,
        draftKey({ sessionId: result.run.sessionId, scope: conversationScope, workspace: targetWorkspace }), permissionMode, !selectedSessionId);
      setExecutionLocations(current => ({ ...current,
        [draftKey({ sessionId: result.run.sessionId, scope: conversationScope, workspace: targetWorkspace })]: selectedLocation }));
      if (!selectedSessionId && selectedContext) contextSelection.consumeDraft(submittedDraftKey, selectedContext);
      if (!compact) taskFile.consume(submittedTask);
      runBudget.bindSession(submittedBudget,
        draftKey({ sessionId: result.run.sessionId, scope: conversationScope, workspace: targetWorkspace }), !selectedSessionId);
      if (promptOverride === undefined && !queuedFrom) draft.consume(submittedDraftKey, submittedDraft.revision);
      if (selectionRequestRef.current !== selectionRequest) {
        void api.getNavigation().then(setNavigation).catch(() => undefined);
        return;
      }
      if (!queuedFrom) setTimeline((current) => [
        ...current,
        { id: `user-${submittedAt}`, type: "user", content: cleanPrompt, timestamp: submittedAt },
      ]);
      setEvents([]);
      if (selectedSessionIdRef.current !== result.run.sessionId) setSessionInspect(null);
      setSessionInspectLoading(false);
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
      if (selectedTask) taskFile.invalidate(submittedTask);
      if (reason instanceof WorktreeOperationFailure) worktreeCreateRef.current = null;
      if (selectionRequestRef.current !== selectionRequest) return;
      const message = reason instanceof Error ? reason.message : "任务启动失败。";
      setError(message);
      setTimeline((current) => [
        ...current,
        { id: `error-${Date.now()}`, type: "error", content: message, timestamp: Date.now() },
      ]);
    } finally {
      startInFlightRef.current = false;
      setStartingRun(false);
    }
  }

  async function stopRun() {
    if (!run || sessionSyncing) return;
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

  async function resumeRun() {
    if (sessionSyncing) return;
    if (selectedSessionId && !run && !sessionInspect) {
      setError("尚未读取到上次任务的验收信息。请重新打开会话后重试，避免遗漏原验收条件。");
      return;
    }
    const previousTrace = traceForRun(sessionInspect?.runtimeRuns || [], run?.runId);
    const previousContract = run?.taskFile || previousTrace?.taskFile;
    const previousRunId = previousTrace?.runId || run?.runId;
    let continuation: { runId: string; artifactId: string } | undefined;
    const identity = selectedSessionId;
    try {
      if (identity && conversationScope === "project") {
        const { candidates } = await api.candidateRecoveries(identity);
        if (selectedSessionIdRef.current !== identity) return;
        const candidate = candidates.find(item => item.nodeId === "primary" && item.runId === previousRunId);
        if (candidate) {
          if (settings?.runContinuationSupported !== true) throw new Error("服务尚未加载任务继续功能，请重启本机服务后继续；已保存进度保留。");
          continuation = { runId: candidate.runId, artifactId: candidate.artifactId };
        } else {
          const submitted = await api.submittedCandidates(identity);
          if (selectedSessionIdRef.current !== identity) return;
          const retained = submitted.candidates.find(item => item.available && item.artifactId
            && item.runId === previousRunId);
          if (retained?.artifactId) {
            if (settings?.runContinuationSupported !== true) throw new Error("服务尚未加载任务继续功能，请重启本机服务后继续；已保存进度保留。");
            continuation = { runId: retained.runId, artifactId: retained.artifactId };
          }
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取已保存进度失败，请稍后重试。");
      return;
    }
    if (previousContract && !taskFile.draft.path.trim() && !(continuation && previousTrace?.continuationContractSaved)) {
      setError("上次任务使用了显式验收契约。重试或继续前，请重新选择并确认该契约；不会静默丢弃原验收条件。");
      return;
    }
    const resumePrompt = continuation ? "按当前预算从已保存的主任务进度继续，保留原验收条件。"
      : resumeTaskPrompt(timeline, runStatus === "failed", history?.turns);
    if (!resumePrompt) {
      setError("尚未读取到完整的原任务要求。请重新打开会话或填写完整要求后继续，避免丢失实现目标与执行能力。");
      return;
    }
    void startRun(resumePrompt, continuation);
  }

  async function approve(requestId: string, decision: string, feedback?: string) {
    if (sessionSyncing) throw new Error("正在同步任务状态，请稍后重试。");
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
      <a className="skip-link" href="#main-content" onClick={(event) => {
        if (!conversationVisible) { event.preventDefault(); document.getElementById("main-content")?.focus(); }
      }}>跳到任务内容</a>
      <div
        className={`app-shell ${inspectorOpen && conversationVisible ? "has-inspector" : ""} ${railCollapsed ? "is-rail-collapsed" : ""}`}
        style={{ "--inspector-w": `${inspectorWidth}px` } as CSSProperties}
      >
        <button className={`mobile-scrim ${railOpen ? "is-visible" : ""}`} onClick={() => setRailOpen(false)} aria-label="关闭任务栏" />
        <AppRail
          workspace={selectedSummary?.projectPath || workspace}
          projects={navigation.projects}
          sessions={navigation.sessions}
          pinnedSessionIds={navigation.pinnedSessionIds}
          pinnedWorkspacePaths={navigation.pinnedWorkspacePaths}
          selectedSessionId={conversationVisible ? selectedSessionId : null}
          conversationScope={conversationScope}
          mobileOpen={railOpen}
          collapsed={railCollapsed}
          searchRequest={searchRequest}
          activePage={activePage}
          archivingSessionId={archivingSessionId}
          closingProjectPath={closingProjectPath}
          onToggleCollapse={() => setRailCollapsed((current) => !current)}
          onCloseMobile={() => setRailOpen(false)}
          onNewStandaloneTask={newStandaloneTask}
          onNewProjectTask={changeWorkspace}
          onSelectSession={(sessionId) => void selectSession(sessionId)}
          onArchiveSession={(sessionId) => void archiveSelectedSession(sessionId)}
          onOpenTrash={openTrash}
          onChangeWorkspace={changeWorkspace}
          onPickWorkspace={pickWorkspace}
          onTogglePin={(kind, target, pinned) => void togglePin(kind, target, pinned)}
          onRename={renameNavigationItem}
          onCloseProject={(path) => void closeProject(path)}
          onOpenSettings={() => { navigatePage("settings"); setSettingsSection("account"); }}
        />
        <div className="center-stack">
          {backgroundPage === "trash" ? <TrashPage
            onBack={showConversation}
            onOpenRail={openRail}
            railCollapsed={railCollapsed}
            revision={trashRevision}
            onChanged={(session, action) => {
              if (action === "delete") draft.remove(draftKey({ sessionId: session.sessionId, scope: session.scope, workspace: session.workspace }));
              void api.getNavigation().then(setNavigation).catch(() => setError("操作已完成，任务列表刷新失败，请刷新页面。"));
            }}
          /> : <><RunStream
            imageInputStatus={settings?.imageInput?.status}
            restoredView={restoredView}
            onProcessPage={storeProcessPage}
            onLoadProcessPage={loadProcessPage}
            captureViewRef={captureViewRef}
            onViewChange={view => { if (selectedSessionId) conversationCache.current.saveView(selectedSessionId, view); }}
            workspace={conversationScope === "project" ? workspace : undefined}
            savedTurnId={events.reduce<string | undefined>((id, event) => event.kind === "turn_saved" ? event.turnId
              : event.kind === "run_input" && event.runInput?.status === "applied" ? undefined : id, undefined)}
            history={history} historyError={historyError} historyLoading={historyLoading}
            onLoadEarlier={() => void loadEarlierTurns()} onFork={setForkTurn}
            session={selectedSummary} onSourceSession={(id) => void selectSession(id)}
            items={questionTimeline}
            runStatus={runStatus}
            run={run}
            execution={execution}
            actualProfile={actualProfile}
            reconnecting={reconnecting}
            onCompose={() => pendingQuestion ? setQuestionOpenRequest(value => value + 1) : setComposerFocusRequest(value => value + 1)}
            onAnswerQuestion={requestId => { setSelectedQuestionId(requestId); setQuestionOpenRequest(value => value + 1); }}
            activeQuestionId={pendingQuestion?.requestId}
            questionOpenRequest={questionOpenRequest}
            streamKey={selectedSessionId || `draft-${conversationScope}-${workspace}`}
            loading={sessionLoading}
            hasSession={Boolean(selectedSessionId)}
            standalone={conversationScope === "standalone"}
            contextLabel={conversationScope === "standalone" ? "独立对话" : workspaceName(workspace)}
            taskTitle={selectedSummary?.title || messagePreview(selectedSummary?.firstMessage || "")}
            railCollapsed={railCollapsed}
            runSummary={runSummary}
            runtimeRuns={sessionInspect?.runtimeRuns}
            summaryLoading={sessionInspectLoading}
            onOpenChanges={() => openInspectorTab("changes")}
            onOpenRuntime={() => openInspectorTab("run")}
            onOpenFile={openFileReference}
            recoveryAction={recoveryAction}
            onOpenSettings={() => navigatePage("settings")}
            agentPanelTarget={agentPanelTarget}
            onOpenAgentPanel={() => openInspectorTab("agents")}
            onCloseAgentPanel={() => setInspectorOpen(false)}
            onPromptSuggestion={(suggestion) => {
              setPrompt(suggestion);
              setComposerFocusRequest((current) => current + 1);
            }}
            onApproval={approve}
            onOpenRail={openRail}
          />
          <Composer
            skills={capabilities?.skills}
            skillsLoading={capabilitiesLoading}
            skillsError={capabilitiesError}
            onRefreshSkills={refreshComposerSkills}
            submitting={startingRun}
            reasoningSettings={<ConversationReasoningSelector selection={contextSelection}
              active={isActive(runStatus)} disabled={sessionLoading || startingRun} />}
            contextUsage={<ContextUsage key={`${draft.key}:${run?.runId || ""}`}
              selection={contextSelection} settingsDisabled={sessionLoading || startingRun}
              context={contextStats}
              hasSession={Boolean(selectedSessionId)} loading={sessionLoading || (!conversationViews && sessionInspectLoading)}
              active={isActive(runStatus)} refreshing={contextRefresh.refreshing} refreshError={contextRefresh.error}
              onRefresh={contextRefresh.refresh}
              onOpenDetails={() => openInspectorTab("context")} />}
            runBudget={<RunBudgetFields budget={runBudget} settings={settings} previous={previousRunBudget}
              executionProfile={isActive(runStatus) ? actualProfile || executionProfile : executionProfile}
              environment={conversationScope === "project" ? composerEnvironment : undefined}
              executionLocation={displayedLocation}
              disabled={sessionLoading || startingRun || isActive(runStatus)} active={isActive(runStatus)} />}
            taskContract={conversationScope === "project" ? <TaskFilePicker task={taskFile} disabled={sessionLoading || startingRun || isActive(runStatus)} /> : undefined}
            executionLocationSettings={conversationScope === "project" ? <ExecutionLocationFields
              value={displayedLocation} workspace={isActive(runStatus) ? run?.workspace || workspace : workspace}
              pendingWorktree={!selectedSessionId && environment === "worktree"}
              active={isActive(runStatus)} disabled={sessionLoading || startingRun || isActive(runStatus) || !settings?.executionLocationSupported}
              error={locationError} onChange={value => setExecutionLocations(current => ({ ...current, [draft.key]: value }))} /> : undefined}
            executionLocationLabel={conversationScope === "project"
              ? `${isActive(runStatus) ? "本轮" : "下次"}：${displayedLocation === "project" ? "直接修改当前目录" : displayedLocation === "isolated" ? "隔离副本" : "正在确认执行位置"}` : undefined}
            submissionBlocked={conversationScope === "project" && (taskFile.blocked || Boolean(locationError))}
            contextBlocked={contextSelection.blocked}
            environment={composerEnvironment}
            onEnvironmentChange={!selectedSessionId && conversationScope === "project" && workspaceStatus?.isGitRepository ? setEnvironment : undefined}
            value={prompt}
            draftKey={draft.key}
            attachments={draft.attachments}
            onAddAttachments={draft.addAttachments}
            onRemoveAttachment={draft.removeAttachment}
            hasSession={Boolean(selectedSessionId)}
            projects={navigation.projects}
            onSwitchProject={switchDraftProject}
            onPickProject={pickDraftProject}
            notice={composerNotice}
            draftStatus={draft.status}
            runStatus={runStatus}
            sessionLoading={sessionLoading || sessionSyncing || Boolean(selectedSummary?.worktreeId && selectedSummary.worktreeStatus !== "ready")}
            inputs={run?.inputs || []}
            inputBusy={inputBusy || startingRun}
            acceptsInput={Boolean(run?.inputProtocol && (run.acceptingInputs !== false || run.acceptingSteering) && runStatus !== "stopping")}
            acceptsSteering={Boolean(run?.inputProtocol === 2 && run.acceptingSteering && runStatus !== "stopping")}
            acceptsQueuedInput={Boolean(run?.inputProtocol && (run.acceptingInputs !== false || (run.queueProtocol === 1 && run.acceptingSteering)) && runStatus !== "stopping")}
            onRunInput={(kind) => void sendRunInput(kind)}
            onRestoreInput={(item) => void restoreRunInput(item)}
            onSteerInput={run?.queueProtocol === 1 ? item => void changeQueuedInput(item, "steer") : undefined}
            onDeleteInput={item => void changeQueuedInput(item, "delete")}
            onResumeQueue={run?.queueProtocol === 1 ? () => void startRun(undefined, undefined, false, run) : undefined}
            settings={settings}
            executionProfile={executionProfile}
            permissionMode={isActive(runStatus) ? actualPermissionMode : permissionMode}
            onPermissionModeChange={permissionSelection.change}
            standalone={conversationScope === "standalone"}
            workspaceLabel={workspaceName(workspace)}
            workspacePath={workspace}
            branch={workspaceStatus?.isGitRepository ? workspaceStatus.branch : undefined}
            placeholder={conversationScope === "standalone"
              ? "随便问问，或者聊聊你的想法…"
              : "描述任务，或者从上面选择一个起点…"}
            hint={permissionMode === "bypass"
              ? "Enter 发送 · Shift + Enter 换行 · 完全访问：文件、命令和项目外路径无需审批"
              : conversationScope === "standalone"
              ? "Enter 发送 · Shift + Enter 换行"
              : "Enter 发送 · Shift + Enter 换行 · 文件修改与命令执行需确认"}
            focusRequest={composerFocusRequest}
            onChange={setPrompt}
            onSubmit={() => void startRun()}
            onCompact={() => void startRun(undefined, undefined, true)}
            onNavigateCommand={navigateComposerCommand}
            onStop={() => void stopRun()}
            onOpenSettings={() => navigatePage("settings")}
            onOpenChanges={() => openInspectorTab("changes")}
            onExecutionProfileChange={profileSelection.change}
          /></>}
        </div>
        {!inspectorOpen && conversationVisible && (
          <button
            className="icon-button inspector-toggle workspace-inspector-toggle"
            onClick={() => setInspectorOpen(true)}
            aria-label="打开检查器"
            title="打开文件与检查器"
          >
            <PanelRight size={18} />
          </button>
        )}
        <button className={`inspector-scrim ${inspectorOpen && conversationVisible ? "is-visible" : ""}`} onClick={() => setInspectorOpen(false)} aria-label="关闭检查器" />
        {inspectorOpen && conversationVisible && (
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
          sessionId={selectedSessionId}
          worktreeId={selectedSummary?.worktreeId}
          open={inspectorOpen && conversationVisible}
          workspace={conversationScope === "project" ? workspace : ""}
          workspaceStatus={conversationScope === "project" ? workspaceStatus : null}
          runStatus={runStatus}
          runId={run?.runId}
          execution={execution}
          events={events}
          settings={settings}
          capabilities={capabilities}
          capabilitiesLoading={capabilitiesLoading}
          capabilitiesError={capabilitiesError}
          sessionInspect={sessionInspect}
          contextStats={contextStats}
          diff={diff}
          selectedFile={selectedFile}
          tabRequest={inspectorTabRequest}
          fileRequest={fileNavigation?.workspace === workspace && fileNavigation.sessionId === selectedSessionId
            ? fileNavigation.request : null}
          onAgentPanelTarget={setAgentPanelTarget}
          onSelectFile={setSelectedFile}
          onClose={() => setInspectorOpen(false)}
          onOpenSettings={() => navigatePage("settings")}
          onOpenTools={() => changeSettingsSection("tools")}
        />
      </div>
      {settingsOpen && <SettingsDialog section={settingsSection} onSectionChange={changeSettingsSection} onClose={closeSettings}>
        <div className="settings-dialog-settings" hidden={settingsSection === "extensions" || settingsSection === "tools" || settingsSection === "search"}>
          <SettingsPage embedded section={settingsSection === "extensions" || settingsSection === "tools" || settingsSection === "search" ? "models" : settingsSection}
            onSectionChange={changeSettingsSection} settings={settings} onBack={closeSettings}
            onOpenRail={openRail} railCollapsed={railCollapsed} onSave={saveSettings} onTest={api.testSettings} />
        </div>
        {settingsSection === "search" && <WebSearchSettings />}
        {settingsSection === "tools" && <ToolCatalog workspace={workspace} scope={conversationScope} projects={navigation.projects} />}
        {settingsSection === "extensions" && <ExtensionsPage embedded initialView={extensionView}
          workspace={workspace} scope={conversationScope} projects={navigation.projects}
          onBack={closeSettings} onOpenRail={openRail} railCollapsed={railCollapsed}
          onUseSkill={command => {
            setPrompt(current => applySkillCommand(current, command));
            showConversation();
            setComposerFocusRequest(current => current + 1);
          }}
          onUseContribution={useExtensionContribution}
          onChanged={() => {
            setCapabilityRevision(value => value + 1);
            void api.getSettings().then(setSettings).catch(() => undefined);
          }} />}
      </SettingsDialog>}
      {forkTurn && selectedSummary && <ForkDialog session={selectedSummary} turn={forkTurn}
        git={conversationScope === "project" && Boolean(workspaceStatus?.isGitRepository)}
        onClose={() => setForkTurn(null)} onCreated={async (id) => {
          setNavigation(await api.getNavigation()); await selectSession(id); setForkTurn(null);
        }} />}
      {error && (
        <div className="toast" role="alert">
          <CircleAlert size={17} />
          <span>{error}</span>
          <button onClick={() => setError(null)} aria-label="关闭错误提示"><X size={15} /></button>
        </div>
      )}
      {trashNotice && !error && <div className="toast trash-toast" role="status"><Trash2 size={17} /><span>{trashNotice}</span><button className="trash-toast-link" onClick={openTrash}>查看回收站</button><button onClick={() => setTrashNotice("")} aria-label="关闭回收站提示"><X size={15} /></button></div>}
      {railNotice && !error && !trashNotice && <div className="toast trash-toast" role="status"><Check size={17} /><span>{railNotice}</span><button onClick={() => setRailNotice("")} aria-label="关闭项目关闭提示"><X size={15} /></button></div>}
    </>
  );
}
