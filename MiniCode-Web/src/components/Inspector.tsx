import {
  Check,
  ChevronDown,
  ChevronRight,
  Code2,
  File,
  FileCode2,
  FileText,
  Folder,
  GitBranch,
  LoaderCircle,
  MessageSquare,
  PanelRight,
  RefreshCw,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import { traceForRun } from "../lib/runtime";
import { workspaceRelativeFile } from "../lib/fileReferences";
import { isMarkdownFile } from "../lib/filePreview";
import type { ComposerInspectorSection } from "../lib/composerCommands";
import type { FileNavigationRequest, FileReference } from "../lib/fileReferences";
import { CapabilityPanel } from "./CapabilityPanel";
import { ContextPanel } from "./ContextPanel";
import { RuntimePanel } from "./RuntimePanel";
import { WorktreeChanges } from "./WorktreeChanges";
import { FilePreview } from "./FilePreview";
import { RuleFilePreview } from "./RuleFilePreview";
import "./InspectorFiles.css";
import type {
  CapabilitySnapshot,
  ContextStatsView,
  ExecutionSnapshot,
  InstructionLayerView,
  RunEvent,
  RunStatus,
  SessionInspect,
  SubmittedCandidate,
  SettingsView,
  WorkspaceEntry,
  WorkspaceFileContent,
  WorkspaceStatus,
} from "../types";

export type InspectorTab = "files" | "changes" | "run" | "capabilities" | "context" | "agents";
export interface InspectorTabRequest {
  tab: InspectorTab;
  section?: ComposerInspectorSection;
  revision: number;
}
type ChangeView = "diff" | "source";

interface InspectorProps {
  sessionId?: string | null;
  worktreeId?: string | null;
  open: boolean;
  workspace: string;
  workspaceStatus: WorkspaceStatus | null;
  runStatus: RunStatus;
  runId?: string | null;
  execution?: ExecutionSnapshot | null;
  events: RunEvent[];
  settings: SettingsView | null;
  capabilities: CapabilitySnapshot | null;
  capabilitiesLoading: boolean;
  capabilitiesError: string;
  sessionInspect: SessionInspect | null;
  contextStats?: ContextStatsView | null;
  diff: string;
  selectedFile: string | null;
  tabRequest?: InspectorTabRequest | null;
  fileRequest?: FileNavigationRequest | null;
  onAgentPanelTarget: (element: HTMLDivElement | null) => void;
  onSelectFile: (path: string) => void;
  onClose: () => void;
  onOpenSettings: () => void;
  onOpenTools?: () => void;
}

interface FileTreeLevelProps {
  directory: string;
  depth: number;
  entriesByDirectory: Record<string, WorkspaceEntry[]>;
  expandedDirectories: Set<string>;
  loadingDirectories: Set<string>;
  selectedSourceFile: string | null;
  onToggleDirectory: (path: string) => void;
  onSelectSourceFile: (path: string) => void;
}

function fileLabel(status: string): string {
  if (status.includes("?")) return "U";
  if (status.includes("A")) return "A";
  if (status.includes("D")) return "D";
  if (status.includes("R")) return "R";
  return "M";
}

function sourcePath(path: string): string {
  const renamed = path.split(" -> ");
  return renamed.at(-1) || path;
}

function formatFileSize(size: number): string {
  if (size < 1_024) return `${size} B`;
  if (size < 1_048_576) return `${(size / 1_024).toFixed(1)} KB`;
  return `${(size / 1_048_576).toFixed(1)} MB`;
}

function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) {
    return <div className="inspector-empty"><Code2 size={19} /><p>该文件暂无可显示的未提交差异。可以切换到“原文”查看当前内容。</p></div>;
  }
  return (
    <pre className="diff-view" aria-label="Git 差异">
      {diff.split("\n").map((line, index) => {
        const kind = line.startsWith("+") && !line.startsWith("+++")
          ? "addition"
          : line.startsWith("-") && !line.startsWith("---")
            ? "deletion"
            : line.startsWith("@@")
              ? "hunk"
              : "context";
        return <span className={`diff-line diff-${kind}`} key={`${index}-${line}`}>{line || " "}</span>;
      })}
    </pre>
  );
}

function FileTreeLevel({
  directory,
  depth,
  entriesByDirectory,
  expandedDirectories,
  loadingDirectories,
  selectedSourceFile,
  onToggleDirectory,
  onSelectSourceFile,
}: FileTreeLevelProps) {
  const entries = entriesByDirectory[directory] || [];
  return (
    <div role="group">
      {entries.map((entry) => {
        const directoryEntry = entry.kind === "directory";
        const expanded = directoryEntry && expandedDirectories.has(entry.path);
        return (
          <div key={entry.path}>
            <button
              className={`file-tree-row ${selectedSourceFile === entry.path ? "is-active" : ""}`}
              style={{ paddingLeft: `${10 + depth * 14}px` }}
              onClick={() => directoryEntry ? onToggleDirectory(entry.path) : onSelectSourceFile(entry.path)}
              aria-expanded={directoryEntry ? expanded : undefined}
              title={entry.path}
            >
              <span className="tree-chevron">
                {directoryEntry
                  ? loadingDirectories.has(entry.path)
                    ? <LoaderCircle size={13} className="spin" />
                    : expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />
                  : null}
              </span>
              {directoryEntry ? <Folder size={15} /> : isMarkdownFile(entry.path) ? <FileText size={14} /> : <File size={14} />}
              <span>{entry.name}</span>
              {!directoryEntry && entry.size !== null && <small>{formatFileSize(entry.size)}</small>}
            </button>
            {expanded && (
              <FileTreeLevel
                directory={entry.path}
                depth={depth + 1}
                entriesByDirectory={entriesByDirectory}
                expandedDirectories={expandedDirectories}
                loadingDirectories={loadingDirectories}
                selectedSourceFile={selectedSourceFile}
                onToggleDirectory={onToggleDirectory}
                onSelectSourceFile={onSelectSourceFile}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

export function Inspector({
  sessionId,
  worktreeId,
  open,
  workspace,
  workspaceStatus,
  runStatus,
  runId,
  execution,
  events,
  settings,
  capabilities,
  capabilitiesLoading,
  capabilitiesError,
  onOpenTools,
  sessionInspect,
  contextStats,
  diff,
  selectedFile,
  tabRequest,
  fileRequest,
  onAgentPanelTarget,
  onSelectFile,
  onClose,
  onOpenSettings,
}: InspectorProps) {
  const [tab, setTab] = useState<InspectorTab>("files");
  const inspectorRef = useRef<HTMLElement>(null);
  const handledSectionRequest = useRef<InspectorTabRequest | null>(null);
  const [changeView, setChangeView] = useState<ChangeView>("diff");
  const [entriesByDirectory, setEntriesByDirectory] = useState<Record<string, WorkspaceEntry[]>>({});
  const [expandedDirectories, setExpandedDirectories] = useState(new Set<string>());
  const [loadingDirectories, setLoadingDirectories] = useState(new Set<string>());
  const [treeError, setTreeError] = useState("");
  const [treeCollapsed, setTreeCollapsed] = useState(false);
  const [selectedSourceFile, setSelectedSourceFile] = useState<string | null>(null);
  const [sourceFileContent, setSourceFileContent] = useState<WorkspaceFileContent | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState("");
  const [sourceLocation, setSourceLocation] = useState<FileNavigationRequest | null>(null);
  const [ruleFile, setRuleFile] = useState<{ sessionId: string; workspace: string; rule: InstructionLayerView } | null>(null);
  const activeRuleFile = ruleFile && ruleFile.sessionId === sessionId && ruleFile.workspace === workspace ? ruleFile : null;
  const sourceRequestRevision = useRef(0);
  const handledFileRequest = useRef<FileNavigationRequest | null>(null);
  const currentFileRequest = useRef(fileRequest);
  currentFileRequest.current = fileRequest;
  const [candidates, setCandidates] = useState<SubmittedCandidate[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [candidateError, setCandidateError] = useState("");
  const [fileSource, setFileSource] = useState("project");
  const retainedRunId = traceForRun(sessionInspect?.runtimeRuns || [], runId)?.runId || runId;
  const candidate = candidates.find(value => value.runId === fileSource);
  const sourceIdentity = `${sessionId}:${workspace}:${fileSource}:${tab}`;
  const currentSource = useRef(sourceIdentity);
  currentSource.current = sourceIdentity;

  useEffect(() => {
    let canceled = false;
    const controller = new AbortController();
    setCandidates([]);
    setFileSource("project");
    setCandidateError("");
    setCandidatesLoading(Boolean(open && sessionId));
    if (open && sessionId) void api.submittedCandidates(sessionId, controller.signal).then(result => {
      if (canceled) return;
      setCandidates(result.candidates);
      const latest = result.candidates.find(value => value.available && value.runId === retainedRunId);
      if (latest && !currentFileRequest.current) setFileSource(latest.runId);
    }).catch(reason => {
      if (!canceled) setCandidateError(reason instanceof Error ? reason.message : "候选状态读取失败");
    }).finally(() => { if (!canceled) setCandidatesLoading(false); });
    return () => { canceled = true; controller.abort(); };
  }, [sessionId, retainedRunId, runStatus, open]);

  const loadDirectory = useCallback(async (directory: string) => {
    if (!workspace) return;
    const identity = currentSource.current;
    setLoadingDirectories((current) => new Set(current).add(directory));
    setTreeError("");
    try {
      const result = fileSource !== "project" && sessionId
        ? await api.candidateFiles(sessionId, fileSource, directory)
        : await api.workspaceFiles(workspace, directory);
      if (currentSource.current !== identity) return;
      setEntriesByDirectory((current) => ({ ...current, [directory]: result.entries }));
      if (result.truncated) setTreeError(`“${directory || "项目根目录"}”文件较多，目前显示前 500 项。`);
    } catch (reason) {
      if (currentSource.current !== identity) return;
      setTreeError(reason instanceof Error ? reason.message : "无法读取项目文件。 ");
    } finally {
      if (currentSource.current === identity) setLoadingDirectories((current) => {
        const next = new Set(current);
        next.delete(directory);
        return next;
      });
    }
  }, [workspace, sessionId, fileSource]);

  const loadSourceFile = useCallback(async (path: string, location: FileNavigationRequest | null = null) => {
    if (!workspace) return;
    const identity = currentSource.current;
    const revision = ++sourceRequestRevision.current;
    const readablePath = sourcePath(path);
    setSelectedSourceFile(readablePath);
    setSourceLoading(true);
    setSourceError("");
    setSourceLocation(location);
    try {
      const result = tab === "files" && fileSource !== "project" && sessionId
        ? await api.candidateFile(sessionId, fileSource, readablePath)
        : await api.workspaceFile(workspace, readablePath);
      if (currentSource.current === identity && sourceRequestRevision.current === revision) setSourceFileContent(result);
    } catch (reason) {
      if (currentSource.current !== identity || sourceRequestRevision.current !== revision) return;
      setSourceFileContent(null);
      setSourceError(reason instanceof Error ? reason.message : "无法读取文件原文。");
    } finally {
      if (currentSource.current === identity && sourceRequestRevision.current === revision) setSourceLoading(false);
    }
  }, [workspace, sessionId, fileSource, tab]);

  useEffect(() => setTreeCollapsed(false), [workspace, sessionId, fileSource]);
  useEffect(() => setRuleFile(null), [workspace, sessionId]);

  useEffect(() => {
    sourceRequestRevision.current += 1;
    setEntriesByDirectory({});
    setExpandedDirectories(new Set());
    setTreeError("");
    setSelectedSourceFile(null);
    setSourceFileContent(null);
    setSourceError("");
    setLoadingDirectories(new Set());
    setSourceLoading(false);
    setSourceLocation(null);
  }, [workspace, sessionId, fileSource, tab]);

  useEffect(() => {
    if (open && tab === "files" && !activeRuleFile && !treeError && !("" in entriesByDirectory) && !loadingDirectories.has("")) {
      void loadDirectory("");
    }
  }, [entriesByDirectory, loadDirectory, loadingDirectories, open, tab, treeError, activeRuleFile]);

  useEffect(() => {
    if (events.some((event) => event.kind === "approval_requested")) setTab(current => current === "agents" ? current : "run");
  }, [events]);

  useEffect(() => {
    if (tabRequest) setTab(tabRequest.tab);
  }, [tabRequest]);

  useEffect(() => {
    if (!open || !tabRequest || tab !== tabRequest.tab || handledSectionRequest.current === tabRequest) return;
    const target = inspectorRef.current?.querySelector<HTMLElement>(tabRequest.section
      ? `[data-inspector-section="${tabRequest.section}"]` : '[role="tab"][aria-selected="true"]');
    if (!target) return;
    handledSectionRequest.current = tabRequest;
    target.scrollIntoView({ block: "nearest" });
    target.focus({ preventScroll: true });
  }, [open, tab, tabRequest, capabilities, sessionInspect]);

  useEffect(() => {
    if (!open || !fileRequest || handledFileRequest.current === fileRequest) return;
    if (tab !== "files") { setTab("files"); return; }
    if (activeRuleFile) { setRuleFile(null); return; }
    let relative = workspaceRelativeFile(fileRequest.path, workspace);
    let targetSource = "project";
    if (!relative) {
      const target = candidates.find(value => value.available && value.workspace && workspaceRelativeFile(fileRequest.path, value.workspace));
      if (target?.workspace) {
        relative = workspaceRelativeFile(fileRequest.path, target.workspace);
        targetSource = target.runId;
      } else if (candidatesLoading) return;
    }
    if (fileSource !== targetSource) { setFileSource(targetSource); return; }
    handledFileRequest.current = fileRequest;
    if (!relative) {
      sourceRequestRevision.current += 1;
      setSelectedSourceFile(null);
      setSourceFileContent(null);
      setSourceLocation(null);
      setSourceLoading(false);
      setSourceError("该引用不在当前项目或可用候选目录内。请核对文件路径。");
      return;
    }
    const parts = relative.split("/");
    const parents = parts.slice(0, -1).map((_, index) => parts.slice(0, index + 1).join("/"));
    setExpandedDirectories(current => new Set([...current, ...parents]));
    for (const directory of parents) void loadDirectory(directory);
    void loadSourceFile(relative, fileRequest);
  }, [open, fileRequest, workspace, candidates, candidatesLoading, fileSource, tab, loadDirectory, loadSourceFile, activeRuleFile]);

  useEffect(() => {
    if (open && tab === "changes" && changeView === "source" && selectedFile) void loadSourceFile(selectedFile);
  }, [changeView, loadSourceFile, selectedFile, tab, open]);

  function toggleDirectory(path: string) {
    if (expandedDirectories.has(path)) {
      setExpandedDirectories((current) => {
        const next = new Set(current);
        next.delete(path);
        return next;
      });
      return;
    }
    setExpandedDirectories((current) => new Set(current).add(path));
    if (!(path in entriesByDirectory)) void loadDirectory(path);
  }

  function openDocumentFile(reference: FileReference) {
    const parts = reference.path.split("/");
    const parents = parts.slice(0, -1).map((_, index) => parts.slice(0, index + 1).join("/"));
    setExpandedDirectories(current => new Set([...current, ...parents]));
    for (const directory of parents) if (!(directory in entriesByDirectory)) void loadDirectory(directory);
    void loadSourceFile(reference.path, reference.line ? { ...reference, revision: sourceRequestRevision.current + 1 } : null);
  }

  return (
    <aside ref={inspectorRef} id="workspace-inspector" className={`inspector ${open ? "is-open" : ""} ${tab === "agents" ? "has-agent-panel" : ""}`} aria-label="文件与任务检查器" aria-hidden={!open} inert={!open}>
      <header className="inspector-header">
        <div className="inspector-tabs" role="tablist" aria-label="检查器分类">
          <button role="tab" aria-selected={tab === "files"} onClick={() => setTab("files")}>文件</button>
          <button role="tab" aria-selected={tab === "changes"} onClick={() => setTab("changes")}>变更</button>
          <button role="tab" aria-selected={tab === "run"} onClick={() => setTab("run")}>运行</button>
          <button role="tab" aria-selected={tab === "agents"} onClick={() => setTab("agents")}>子代理</button>
          <button role="tab" aria-selected={tab === "capabilities"} onClick={() => setTab("capabilities")}>能力</button>
          <button role="tab" aria-selected={tab === "context"} onClick={() => setTab("context")}>上下文</button>
        </div>
        {open && (
          <button
            className="icon-button inspector-close workspace-inspector-toggle"
            onClick={onClose}
            aria-label="关闭检查器"
            title="关闭文件与检查器"
          >
            <PanelRight size={18} />
          </button>
        )}
      </header>

      {open && tab === "agents" && <div id="agent-panel-host" ref={onAgentPanelTarget} />}

      {tab === "files" && (
        activeRuleFile ? <RuleFilePreview key={`${activeRuleFile.sessionId}:${activeRuleFile.rule.name}:${activeRuleFile.rule.path}`}
          sessionId={activeRuleFile.sessionId} rule={activeRuleFile.rule}
          onBack={() => { setRuleFile(null); setTab("context"); }}
          onBrowseProject={workspace ? () => { setRuleFile(null); setFileSource("project"); } : undefined} /> : !workspace ? (
          <div className="inspector-body standalone-inspector-state">
            <div className="inspector-empty"><MessageSquare size={20} /><p>这是独立对话，没有绑定项目文件。请从左侧项目下新建对话后浏览源码。</p></div>
          </div>
        ) : <div className={`inspector-body files-panel ${selectedSourceFile ? "has-open-file" : ""}`}>
          {(candidates.length > 0 || candidateError) && <div className="candidate-file-source">
              <label>文件位置 <select aria-label="文件位置" value={fileSource} onChange={event => setFileSource(event.target.value)}>
                <option value="project">项目目录</option>
                {candidates.map(value => <option key={value.runId} value={value.runId} disabled={!value.available}>
                  候选文件（未交付） · {value.runId.slice(0, 8)}{value.available ? "" : " · 不可用"}
                </option>)}
              </select></label>
              {candidate && <>
                <p role="status">已提交候选，尚未写回项目。{candidate.verificationStatus === "FAILED_TERMINAL" ? "最终核验未完成。" : "等待核验与整合。"}</p>
                <code title={candidate.workspace}>{candidate.workspace}</code>
                {sessionId && <a href={api.candidateArchiveUrl(sessionId, candidate.runId)} download>下载候选源码 ZIP</a>}
                <small>源码可在下方预览；下载不包含依赖和缓存目录。</small>
              </>}
              {candidateError && <p role="alert">{candidateError}</p>}
              {candidates.some(value => !value.available) && <p>部分旧候选目录已不可用，可在“运行”中检查恢复记录。</p>}
          </div>}
          <div className={`file-explorer ${treeCollapsed ? "is-collapsed" : ""}`}>
            <div className="file-explorer-header">
              <button type="button" className="file-explorer-toggle" onClick={() => setTreeCollapsed(value => !value)}
                aria-expanded={!treeCollapsed} aria-controls="inspector-file-tree" aria-label={treeCollapsed ? "展开文件树" : "收起文件树"}
                title={treeCollapsed ? "展开文件树" : "收起文件树，扩大阅读区域"}>
                {treeCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}<Folder size={15} />
                <strong title={candidate?.workspace || workspace}>{candidate ? "候选文件 · 未交付" : workspace.split(/[\\/]/).at(-1) || "项目文件"}</strong>
              </button>
              <button className="icon-button" onClick={() => void loadDirectory("")} aria-label="刷新项目文件" title="刷新"><RefreshCw size={14} /></button>
            </div>
            {!treeCollapsed && treeError && <p className="tree-message">{treeError}</p>}
            {!treeCollapsed && (loadingDirectories.has("") && !("" in entriesByDirectory) ? (
              <div className="tree-loading"><LoaderCircle size={15} className="spin" />正在读取项目文件</div>
            ) : (
              <div id="inspector-file-tree" className="file-tree" role="tree" aria-label="项目文件">
                <FileTreeLevel
                  directory=""
                  depth={0}
                  entriesByDirectory={entriesByDirectory}
                  expandedDirectories={expandedDirectories}
                  loadingDirectories={loadingDirectories}
                  selectedSourceFile={selectedSourceFile}
                  onToggleDirectory={toggleDirectory}
                  onSelectSourceFile={(path) => void loadSourceFile(path)}
                />
              </div>
            ))}
          </div>
          <div className="source-pane">
            <FilePreview key={`${sourceIdentity}:${selectedSourceFile}`} workspace={candidate?.workspace || workspace}
              file={sourceFileContent} loading={sourceLoading} error={sourceError} location={sourceLocation} onOpenFile={openDocumentFile} />
          </div>
        </div>
      )}

      {open && tab === "changes" && worktreeId && <WorktreeChanges key={worktreeId} id={worktreeId} runStatus={runStatus} />}
      {tab === "changes" && !worktreeId && (
        !workspace ? (
          <div className="inspector-body standalone-inspector-state">
            <div className="inspector-empty"><GitBranch size={20} /><p>独立对话不属于 Git 项目，因此没有项目变更。</p></div>
          </div>
        ) : <div className="inspector-body changes-panel">
          <div className="panel-summary">
            <div><GitBranch size={16} /><strong>{workspaceStatus?.branch || "工作区"}</strong></div>
            <span>{workspaceStatus?.summary || "正在读取"}</span>
          </div>
          {!workspaceStatus?.isGitRepository ? (
            <div className="inspector-empty"><GitBranch size={19} /><p>当前路径不是 Git 工作区。仍然可以在“文件”中查看原文。</p></div>
          ) : workspaceStatus.files.length === 0 ? (
            <div className="inspector-empty"><Check size={19} /><p>没有未提交变更。可以切换到“文件”浏览项目内容。</p></div>
          ) : (
            <>
              <div className="changed-files">
                {workspaceStatus.files.map((file) => (
                  <button className={selectedFile === file.path ? "is-active" : ""} key={`${file.status}-${file.path}`} onClick={() => onSelectFile(file.path)}>
                    <span className={`file-status file-${fileLabel(file.status).toLowerCase()}`}>{fileLabel(file.status)}</span>
                    <span title={file.path}>{file.path}</span>
                    <ChevronRight size={14} />
                  </button>
                ))}
              </div>
              <div className="change-view-bar" role="tablist" aria-label="文件查看方式">
                <button role="tab" aria-selected={changeView === "diff"} onClick={() => setChangeView("diff")}>差异</button>
                <button role="tab" aria-selected={changeView === "source"} onClick={() => setChangeView("source")}>原文</button>
              </div>
              <div className="change-view-content">
                {!selectedFile ? (
                  <div className="inspector-empty"><FileCode2 size={19} /><p>选择文件查看差异或原文。</p></div>
                ) : changeView === "diff" ? (
                  <DiffView diff={diff} />
                ) : (
                  <FilePreview key={`${sourceIdentity}:${selectedSourceFile}`} workspace={workspace} allowPreview={false}
                    file={sourceFileContent} loading={sourceLoading} error={sourceError} location={sourceLocation} />
                )}
              </div>
            </>
          )}
        </div>
      )}

      {open && tab === "run" && (
        <RuntimePanel sessionId={sessionId} runStatus={runStatus} runId={runId} execution={execution} events={events} inspect={sessionInspect} />
      )}

      {tab === "capabilities" && (
        <CapabilityPanel
          capabilities={capabilities}
          inspect={sessionInspect}
          loading={capabilitiesLoading}
          error={capabilitiesError}
          onOpenTools={onOpenTools}
        />
      )}

      {tab === "context" && (
        <ContextPanel
          sessionId={sessionId || undefined}
          workspace={workspace}
          settings={settings}
          capabilities={capabilities}
          inspect={sessionInspect}
          contextStats={contextStats}
          onOpenSettings={onOpenSettings}
          onOpenRule={rule => {
            if (!sessionId || !rule.path) return;
            setRuleFile({ sessionId, workspace, rule });
            setTab("files");
          }}
        />
      )}
    </aside>
  );
}
