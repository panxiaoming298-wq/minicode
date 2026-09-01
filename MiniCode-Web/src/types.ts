export type RunStatus =
  | "idle"
  | "queued"
  | "running"
  | "stopping"
  | "completed"
  | "failed"
  | "stopped";

export type ConversationScope = "project" | "standalone";

export interface ProviderOption {
  id: string;
  label: string;
  defaultBaseUrl: string;
  models: string[];
}

export interface SettingsView {
  provider: string;
  model: string;
  baseUrl: string;
  baseUrlSource: string;
  apiKeyConfigured: boolean;
  apiKeyMask: string;
  apiKeySource: string;
  thinkingMode: string;
  reasoningEffort: string;
  settingsPath: string;
  providers: ProviderOption[];
}

export interface WorkspaceFile {
  status: string;
  path: string;
}

export interface WorkspaceStatus {
  isGitRepository: boolean;
  branch: string;
  files: WorkspaceFile[];
  summary: string;
}

export interface RecentWorkspace {
  name: string;
  path: string;
}

export interface NavigationPayload {
  projects: RecentWorkspace[];
  sessions: SessionSummary[];
  pinnedSessionIds: string[];
  pinnedWorkspacePaths: string[];
}

export interface WorkspaceEntry {
  name: string;
  path: string;
  kind: "directory" | "file";
  size: number | null;
}

export interface WorkspaceDirectory {
  directory: string;
  entries: WorkspaceEntry[];
  truncated: boolean;
}

export interface WorkspaceFileContent {
  path: string;
  name: string;
  content: string;
  encoding: string;
  size: number;
  lineCount: number;
  truncated: boolean;
}

export interface SessionSummary {
  sessionId: string;
  createdAt: number;
  updatedAt: number;
  firstMessage: string;
  lastMessage: string;
  messageCount: number;
  workspace: string;
  scope: ConversationScope;
  projectPath: string | null;
  model?: string;
  status: string;
  toolCallCount: number;
}

export interface StoredMessage {
  role: string;
  content?: string;
  toolName?: string;
  input?: unknown;
  output?: string;
}

export interface RunInfo {
  runId: string;
  sessionId: string;
  workspace: string;
  promptPreview: string;
  status: RunStatus;
  createdAt: number;
  updatedAt: number;
  eventCount: number;
  scope?: ConversationScope;
}

export interface ApprovalChoice {
  key?: string;
  label: string;
  decision: string;
}

export interface ApprovalRequest {
  kind: string;
  summary: string;
  details: string[];
  scope?: string;
  choices: ApprovalChoice[];
}

export interface RunEvent {
  kind: string;
  sequence: number;
  timestamp: number;
  step?: number | null;
  content?: string;
  message?: string;
  status?: RunStatus;
  stopReason?: string;
  toolName?: string;
  input?: unknown;
  output?: string;
  isError?: boolean;
  durationMs?: number | null;
  requestId?: string;
  request?: ApprovalRequest;
  decision?: string;
  model?: string;
  provider?: string;
  mock?: boolean;
}

export type TimelineItem =
  | { id: string; type: "user"; content: string; timestamp: number }
  | { id: string; type: "assistant"; content: string; timestamp: number; streaming?: boolean }
  | {
      id: string;
      type: "tool";
      toolName: string;
      input: unknown;
      output: string;
      timestamp: number;
      status: "running" | "success" | "error";
      durationMs?: number | null;
      step?: number | null;
    }
  | {
      id: string;
      type: "progress";
      content: string;
      timestamp: number;
      status: "running" | "complete" | "failed" | "stopped";
    }
  | {
      id: string;
      type: "approval";
      requestId: string;
      request: ApprovalRequest;
      timestamp: number;
      resolved?: string;
    }
  | { id: string; type: "error"; content: string; timestamp: number };

export interface BootstrapPayload {
  workspace: string;
  settings: SettingsView;
  workspaceStatus: WorkspaceStatus;
  recentWorkspaces: RecentWorkspace[];
}

export interface WorkspaceOpenPayload {
  workspace: string;
  workspaceStatus: WorkspaceStatus;
  recentWorkspaces: RecentWorkspace[];
  sessions: SessionSummary[];
}
