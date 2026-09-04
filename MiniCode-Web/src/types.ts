export type RunStatus =
  | "idle"
  | "queued"
  | "running"
  | "stopping"
  | "completed"
  | "failed"
  | "stopped";

export type ConversationScope = "project" | "standalone";

export type ExecutionProfile = "standard" | "ultra";

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
  executionProfile: ExecutionProfile;
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
  runtimeSummary?: string;
  checkpointCount?: number;
  instructionSummary?: string;
  hookSummary?: string;
  delegationSummary?: string;
  extensionSummary?: string;
  readinessSummary?: string;
  memorySummary?: string;
}

export interface StoredMessage {
  role: string;
  content?: string;
  toolName?: string;
  toolUseId?: string;
  input?: unknown;
  output?: string;
  isError?: boolean;
}

export interface CapabilitySkill {
  name: string;
  description: string;
  source: string;
}

export interface McpServerCapability {
  name: string;
  status: string;
  toolCount: number;
  resourceCount: number;
  promptCount: number;
  protocol: string;
  error: string;
}

export interface ToolCapability {
  name: string;
  description: string;
  effect: string;
  replayPolicy: string;
  source: "core" | "skill" | "mcp" | string;
}

export interface InstructionLayerView {
  name: string;
  scope: string;
  kind: string;
  exists: boolean;
}

export interface HookStatusView {
  totalHooks: number;
  enabledHooks: number;
  totalCalls: number;
  totalDurationMs: number;
  summary: string;
}

export interface DelegationStatusView {
  runningTasks: number;
  totalTracked: number;
  maxSlots: number;
  availableSlots: number;
  activeLabels: string[];
  summary: string;
}

export interface ExtensionView {
  name: string;
  scope: string;
  version: string;
  description: string;
  enabled: boolean;
}

export interface ReadinessView {
  status: string;
  provider: string;
  providerReady: boolean;
  providerChannel: string;
  fallbackReady: boolean;
  fallbackCandidates: string[];
  viableFallbacks: string[];
  riskScope: string;
  nextActions: string[];
  issues: string[];
  preflightChecks: Array<{ name: string; status: string; detail: string }>;
  summary: string;
}

export interface MemoryView {
  schema: string;
  capturedAt: string;
  scopes: Array<{
    name: string;
    entries: number;
    sizeBytes: number;
    categories: string[];
  }>;
}

export interface CapabilitySnapshot {
  source: string;
  skills: CapabilitySkill[];
  mcpServers: McpServerCapability[];
  tools: ToolCapability[];
  instructionLayers: InstructionLayerView[];
  instructionSummary: string;
  hookStatus: HookStatusView;
  delegationStatus: DelegationStatusView;
  extensions: ExtensionView[];
  extensionSummary: string;
  readiness: ReadinessView;
  readinessSummary: string;
  memory: MemoryView;
}

export interface ContextStatsView {
  totalTokens: number;
  estimatedTokens: number;
  providerUsageTokens: number;
  contextWindow: number;
  effectiveInput: number;
  utilization: number;
  warningLevel: string;
  accounting: {
    source: string;
    isExact: boolean;
    stale: boolean;
    reason: string;
  };
}

export interface ArtifactView {
  artifactId: string;
  contentHash: string;
  size: number;
  mediaType: string;
}

export interface VerificationView {
  verifier: string;
  passed: boolean;
  scope: string;
  details: string;
  evidence: Array<{
    evidenceType: string;
    contentHash: string;
    claim: string;
    producerNodeId: string;
    baseRevision: string;
    lineStart: number | null;
    lineEnd: number | null;
    page: number | null;
  }>;
  deterministic: boolean;
  createdAt: number;
}

export interface RuntimeNodeView {
  nodeId: string;
  objective: string;
  role: string;
  dependencies: string[];
  allowedEffects: string[];
  acceptanceCriteria: string[];
  maxAttempts: number;
  state: string;
  attemptCount: number;
  rejectionReasons: string[];
  result: null | {
    status: string;
    outputSummary: string;
    changedResources: string[];
    artifacts: ArtifactView[];
    verificationRecords: VerificationView[];
    warnings: string[];
    failure: null | {
      kind: string;
      message: string;
      retryable: boolean;
      ambiguousSideEffect: boolean;
    };
  };
}

export interface RuntimeTraceView {
  schemaVersion: string;
  runId: string;
  webRunId: string;
  profile: {
    requestedMode: string;
    selectedRoute: string;
    reasoningBudget: string;
    maxActiveWorkers: number;
    verificationLevel: string;
    writeIsolation: string;
  };
  topology: {
    selectedRoute: string;
    expectedParallelGain: number;
    writeConflictRisk: string;
    deterministicVerifiability: boolean;
    reasons: string[];
  };
  plan: {
    planVersion: number;
    contractRevision: number;
    nodes: RuntimeNodeView[];
  };
  scheduler: {
    completed: boolean;
    canceled: boolean;
    claimedAttempts: number;
    peakActiveWorkers: number;
    peakActiveWriters: number;
    reasons: string[];
  };
  delivery: {
    completed: boolean;
    outputSummary: string;
    artifacts: ArtifactView[];
    verificationRecords: VerificationView[];
    reasons: string[];
  };
  contextManifests: Array<{
    nodeId: string;
    baseRevision: string;
    includedFileCount: number;
    dependencyCount: number;
    artifactCount: number;
    toolCapabilityScope: string[];
  }>;
}

export interface CheckpointView {
  checkpointId: string;
  createdAt: number;
  file: string;
  existed: boolean;
  kind: string;
  groupId: string;
}

export interface SessionInspect {
  contextStats: ContextStatsView;
  permissions: { configured: boolean; itemCount: number };
  skills: CapabilitySkill[];
  mcpServers: McpServerCapability[];
  instructionLayers: InstructionLayerView[];
  hookStatus: HookStatusView;
  delegationStatus: DelegationStatusView;
  extensions: ExtensionView[];
  readiness: ReadinessView;
  memory: MemoryView;
  runtimeRuns: RuntimeTraceView[];
  checkpoints: CheckpointView[];
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
  retainedEventCount?: number;
  eventsDiscardedThrough?: number;
  executionProfile: ExecutionProfile;
  scope?: ConversationScope;
}

export interface RunSnapshot {
  run: RunInfo | null;
  events: RunEvent[];
}

export interface ApprovalChoice {
  key?: string;
  label: string;
  decision: string;
}

export interface ApprovalTechnicalDetail {
  label: string;
  value: string;
}

export interface ApprovalRequest {
  kind: string;
  summary: string;
  details: string[];
  scope?: string;
  choices: ApprovalChoice[];
  title?: string;
  action?: string;
  impact?: string;
  reason?: string;
  target?: string;
  targetLabel?: string;
  operationType?: string;
  riskLevel?: "low" | "medium" | "high" | string;
  timeoutPolicy?: string;
  technicalDetails?: ApprovalTechnicalDetail[];
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
  executionProfile?: ExecutionProfile;
  webRunId?: string;
  capabilities?: CapabilitySnapshot;
  runtime?: {
    category?: string;
    message?: string;
    step?: number | null;
    profile?: string;
    phase?: string;
    verification_focus?: string;
    stop_reason?: string;
    widening_reason?: string;
    evidence_summary?: string;
  } | null;
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
      source?: "progress" | "thinking" | "runtime";
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
