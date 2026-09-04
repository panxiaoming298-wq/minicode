import {
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Code2,
  Copy,
  File,
  FileCode2,
  Folder,
  GitBranch,
  LoaderCircle,
  MessageSquare,
  PanelRight,
  RefreshCw,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../api/client";
import { CapabilityPanel } from "./CapabilityPanel";
import { ContextPanel } from "./ContextPanel";
import { RuntimePanel } from "./RuntimePanel";
import type {
  CapabilitySnapshot,
  RunEvent,
  RunStatus,
  SessionInspect,
  SettingsView,
  WorkspaceEntry,
  WorkspaceFileContent,
  WorkspaceStatus,
} from "../types";

type InspectorTab = "files" | "changes" | "run" | "capabilities" | "context";
type ChangeView = "diff" | "source";

interface InspectorProps {
  open: boolean;
  workspace: string;
  workspaceStatus: WorkspaceStatus | null;
  runStatus: RunStatus;
  events: RunEvent[];
  settings: SettingsView | null;
  capabilities: CapabilitySnapshot | null;
  capabilitiesLoading: boolean;
  capabilitiesError: string;
  sessionInspect: SessionInspect | null;
  diff: string;
  selectedFile: string | null;
  onSelectFile: (path: string) => void;
  onClose: () => void;
  onOpenSettings: () => void;
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

function SourceView({
  file,
  loading,
  error,
}: {
  file: WorkspaceFileContent | null;
  loading: boolean;
  error: string;
}) {
  const [copied, setCopied] = useState(false);
  const lines = useMemo(() => file?.content.split("\n") || [], [file]);

  useEffect(() => setCopied(false), [file?.path]);

  if (loading) {
    return <div className="inspector-empty"><LoaderCircle size={19} className="spin" /><p>正在读取文件原文…</p></div>;
  }
  if (error) {
    return <div className="inspector-empty is-error"><CircleAlert size={19} /><p>{error}</p></div>;
  }
  if (!file) {
    return <div className="inspector-empty"><FileCode2 size={19} /><p>从文件树中选择一个文件查看原文。</p></div>;
  }

  return (
    <div className="source-document">
      <div className="source-toolbar">
        <div>
          <strong title={file.path}>{file.path}</strong>
          <span>{file.lineCount} 行 · {formatFileSize(file.size)} · {file.encoding}</span>
        </div>
        <button
          className="icon-button"
          onClick={() => {
            void navigator.clipboard.writeText(file.content).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1_500);
            });
          }}
          aria-label={`复制 ${file.name} 原文`}
          title={copied ? "已复制" : "复制原文"}
        >
          {copied ? <Check size={15} /> : <Copy size={15} />}
        </button>
      </div>
      {file.truncated && <div className="source-warning">文件较大，目前只显示前 {formatFileSize(1_500_000)}。</div>}
      <pre className="source-view" aria-label={`${file.name} 原文`}>
        {lines.map((line, index) => (
          <span className="source-line" key={`${index}-${line.slice(0, 24)}`}>
            <i>{index + 1}</i>
            <code>{line || " "}</code>
          </span>
        ))}
      </pre>
    </div>
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
              {directoryEntry ? <Folder size={15} /> : <File size={14} />}
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
  open,
  workspace,
  workspaceStatus,
  runStatus,
  events,
  settings,
  capabilities,
  capabilitiesLoading,
  capabilitiesError,
  sessionInspect,
  diff,
  selectedFile,
  onSelectFile,
  onClose,
  onOpenSettings,
}: InspectorProps) {
  const [tab, setTab] = useState<InspectorTab>("files");
  const [changeView, setChangeView] = useState<ChangeView>("diff");
  const [entriesByDirectory, setEntriesByDirectory] = useState<Record<string, WorkspaceEntry[]>>({});
  const [expandedDirectories, setExpandedDirectories] = useState(new Set<string>());
  const [loadingDirectories, setLoadingDirectories] = useState(new Set<string>());
  const [treeError, setTreeError] = useState("");
  const [selectedSourceFile, setSelectedSourceFile] = useState<string | null>(null);
  const [sourceFileContent, setSourceFileContent] = useState<WorkspaceFileContent | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState("");

  const loadDirectory = useCallback(async (directory: string) => {
    if (!workspace) return;
    setLoadingDirectories((current) => new Set(current).add(directory));
    setTreeError("");
    try {
      const result = await api.workspaceFiles(workspace, directory);
      setEntriesByDirectory((current) => ({ ...current, [directory]: result.entries }));
      if (result.truncated) setTreeError(`“${directory || "项目根目录"}”文件较多，目前显示前 500 项。`);
    } catch (reason) {
      setTreeError(reason instanceof Error ? reason.message : "无法读取项目文件。 ");
    } finally {
      setLoadingDirectories((current) => {
        const next = new Set(current);
        next.delete(directory);
        return next;
      });
    }
  }, [workspace]);

  const loadSourceFile = useCallback(async (path: string) => {
    if (!workspace) return;
    const readablePath = sourcePath(path);
    setSelectedSourceFile(readablePath);
    setSourceLoading(true);
    setSourceError("");
    try {
      setSourceFileContent(await api.workspaceFile(workspace, readablePath));
    } catch (reason) {
      setSourceFileContent(null);
      setSourceError(reason instanceof Error ? reason.message : "无法读取文件原文。");
    } finally {
      setSourceLoading(false);
    }
  }, [workspace]);

  useEffect(() => {
    setEntriesByDirectory({});
    setExpandedDirectories(new Set());
    setTreeError("");
    setSelectedSourceFile(null);
    setSourceFileContent(null);
    setSourceError("");
  }, [workspace]);

  useEffect(() => {
    if (open && tab === "files" && !("" in entriesByDirectory) && !loadingDirectories.has("")) {
      void loadDirectory("");
    }
  }, [entriesByDirectory, loadDirectory, loadingDirectories, open, tab]);

  useEffect(() => {
    if (events.some((event) => event.kind === "approval_requested")) setTab("run");
  }, [events]);

  useEffect(() => {
    if (tab === "changes" && changeView === "source" && selectedFile) void loadSourceFile(selectedFile);
  }, [changeView, loadSourceFile, selectedFile, tab]);

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

  return (
    <aside id="workspace-inspector" className={`inspector ${open ? "is-open" : ""}`} aria-label="文件与任务检查器" aria-hidden={!open} inert={!open}>
      <header className="inspector-header">
        <div className="inspector-tabs" role="tablist" aria-label="检查器分类">
          <button role="tab" aria-selected={tab === "files"} onClick={() => setTab("files")}>文件</button>
          <button role="tab" aria-selected={tab === "changes"} onClick={() => setTab("changes")}>变更</button>
          <button role="tab" aria-selected={tab === "run"} onClick={() => setTab("run")}>运行</button>
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

      {tab === "files" && (
        !workspace ? (
          <div className="inspector-body standalone-inspector-state">
            <div className="inspector-empty"><MessageSquare size={20} /><p>这是独立对话，没有绑定项目文件。请从左侧项目下新建对话后浏览源码。</p></div>
          </div>
        ) : <div className="inspector-body files-panel">
          <div className="file-explorer">
            <div className="file-explorer-header">
              <div><Folder size={15} /><strong title={workspace}>{workspace.split(/[\\/]/).at(-1) || "项目文件"}</strong></div>
              <button className="icon-button" onClick={() => void loadDirectory("")} aria-label="刷新项目文件" title="刷新"><RefreshCw size={14} /></button>
            </div>
            {treeError && <p className="tree-message">{treeError}</p>}
            {loadingDirectories.has("") && !("" in entriesByDirectory) ? (
              <div className="tree-loading"><LoaderCircle size={15} className="spin" />正在读取项目文件</div>
            ) : (
              <div className="file-tree" role="tree" aria-label="项目文件">
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
            )}
          </div>
          <div className="source-pane">
            <SourceView file={sourceFileContent} loading={sourceLoading} error={sourceError} />
          </div>
        </div>
      )}

      {tab === "changes" && (
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
                  <SourceView file={sourceFileContent} loading={sourceLoading} error={sourceError} />
                )}
              </div>
            </>
          )}
        </div>
      )}

      {tab === "run" && (
        <RuntimePanel runStatus={runStatus} events={events} inspect={sessionInspect} />
      )}

      {tab === "capabilities" && (
        <CapabilityPanel
          capabilities={capabilities}
          inspect={sessionInspect}
          loading={capabilitiesLoading}
          error={capabilitiesError}
        />
      )}

      {tab === "context" && (
        <ContextPanel
          workspace={workspace}
          settings={settings}
          capabilities={capabilities}
          inspect={sessionInspect}
          onOpenSettings={onOpenSettings}
        />
      )}
    </aside>
  );
}
