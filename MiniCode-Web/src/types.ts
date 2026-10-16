export type RunStatus =
  | "idle"
  | "queued"
  | "running"
  | "stopping"
  | "completed"
  | "failed"
  | "stopped";

export type ConversationScope = "project" | "standalone";

export type ExecutionProfile = "standard" | "deep" | "ultra";
export type PermissionMode = "default" | "bypass";

export interface DevelopmentService {
  serviceId: string;
  command: string[];
  cwd: string;
  workspace: string;
  sessionIds: string[];
  url: string;
  status: "starting" | "running" | "stopping" | "stopped" | "failed";
  pid: number | null;
  startedAt: number;
  readyAt: number | null;
  exitCode: number | null;
  error: string;
}

export interface DevelopmentServiceLogs {
  service: DevelopmentService;
  output: string;
  truncated: boolean;
}

export interface RunBudget {
  unlimited?: boolean;
  maxTokens?: number;
  durationSeconds?: number;
}

export interface TaskFileSummary {
  sha256: string;
  criteriaCount: number;
  expectedOutputCount: number;
  verificationCommandCount: number;
  requiresWorkspaceWrite: boolean;
}

export interface TaskFileSelection {
  path: string;
  sha256: string;
  commandsTrusted: true;
}

export interface TaskFilePreview extends TaskFileSummary {
  path: string;
  taskConstraints: string[];
  acceptanceCriteria: string[];
  expectedOutputs: string[];
  runtimeVerificationCommands: ({
    name: string; kind: string; timeout: number; covers: number[];
  } & ({
    command: string[]; browser?: never;
  } | {
    command?: never;
    browser: {
      scenario: string; root?: string; url?: string; views: ("desktop" | "mobile")[];
      screenshots: "all" | "failure" | "none"; readySelector?: string;
      allowedOrigins?: string[]; allowedWebSockets?: string[];
    };
  }))[];
  warning: string;
}

export interface ProviderOption {
  id: string;
  label: string;
  defaultBaseUrl: string;
  models: string[];
}

export interface SavedConnection {
  provider: string;
  model: string;
  baseUrl: string;
}

export interface RuntimeModelPricing {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
}

export interface RuntimeSettings {
  maxOutputTokens?: number | null;
  runtimeMaxTokens?: number | null;
  runtimeMaxCost?: number | null;
  /** Absolute Unix time in seconds. */
  runtimeDeadline?: number | null;
  runtimeNetworkAccess?: boolean;
  runtimeModelPricing?: Record<string, RuntimeModelPricing | null>;
}

export interface WebAgentSettings {
  mode: "off" | "async" | "teams";
  teamWrites: boolean;
  primaryRole: "worker" | "coordinator";
  fork: boolean;
  promptCache: "off" | "auto" | "stable-prefix";
}

export interface SettingsUpdate extends RuntimeSettings {
  imageInputMode?: "auto" | "supported" | "unsupported";
  webAgentSettings?: WebAgentSettings;
  provider: string;
  model: string;
  baseUrl: string;
  apiKey?: string;
  clearApiKey?: boolean;
  thinkingMode?: string;
  reasoningEffort?: string;
}

export type ReasoningEffort = "low" | "high" | "max";

export interface ConversationContext {
  maxTokens: number | null;
  reasoningEffort?: ReasoningEffort;
}

export interface ContextBudgetPolicy {
  version: "total-window-v1";
  outputMode: "auto" | "manual";
  outputLimit: number;
  inputLimit: number;
  pressureWindow: number;
  noteAtTokens: number;
  compactAtTokens: number;
  protectAtTokens: number;
}

export interface ConversationContextView extends ConversationContext {
  budgetPolicy?: ContextBudgetPolicy | null;
  reasoningEffortSupported?: boolean;
  model: string;
  modelMaxTokens: number;
  modelCapacityKnown?: boolean;
  availableMaxTokens: number;
  effectiveMaxTokens: number;
  reservedTokens: number;
  outputReserveTokens?: number | null;
  safetyReserveTokens?: number | null;
  effectiveInputTokens: number;
  capacityError: string;
}

export interface SettingsView extends RuntimeSettings {
  connectionHistory?: SavedConnection[];
  imageInput?: {
    status: "supported" | "unsupported" | "unknown";
    source: "catalog" | "manual" | "unknown";
    mode: "auto" | "supported" | "unsupported";
    formats: string[];
    maxBytes: number;
    maxCount: number;
  };
  balanceQuerySupported?: boolean;
  permissionModeSupported?: boolean;
  executionLocationSupported?: boolean;
  conversationContext?: ConversationContextView;
  webAgentSettings?: WebAgentSettings;
  webAgentSettingsAvailable?: boolean;
  webAgentSettingsError?: string | null;
  runBudgetSupported?: boolean;
  runUnlimitedBudgetSupported?: boolean;
  runContinuationSupported?: boolean;
  webRuntimePolicy?: {
    enabled: boolean; orchestration: "legacy" | "primary"; modes: ExecutionProfile[];
    durable: boolean; defaultMaxTokens: number; defaultDurationSeconds: number; standardRequiresWorktree: boolean;
    agentTeams?: boolean; agentTeamWrites?: boolean; primaryRole?: "worker" | "coordinator";
  };
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

export type WebSearchProvider = "auto" | "public" | "brave" | "tavily";

export interface SearchUsageMetric {
  id: string;
  label: string;
  unit: "credits" | "requests";
  used: number | null;
  limit: number | null;
  remaining: number | null;
  unlimited: boolean;
  resetAt: number | null;
}

export interface SearchUsageView {
  status: "unknown" | "ok" | "unavailable" | "error";
  source: "search_headers" | "usage_api";
  checkedAt: number | null;
  attemptedAt: number | null;
  nextRefreshAt: number | null;
  stale: boolean;
  errorCode: string;
  message: string;
  metrics: SearchUsageMetric[];
}

export interface SearchKeyView {
  id: string;
  name: string;
  enabled: boolean;
  apiKeyMask: string;
  usage?: SearchUsageView;
}

export interface SearchKeyUpdate {
  id?: string;
  name: string;
  enabled: boolean;
  apiKey?: string;
}

export interface SearchServiceCredential {
  apiKeyConfigured: boolean;
  apiKeyMask: string;
  apiKeySource: "environment" | "settings" | "none";
  keys: SearchKeyView[];
}

export interface WebSearchSettingsView {
  provider: WebSearchProvider;
  providerSource: "environment" | "settings" | "default";
  configurationError: string;
  revision: string;
  maxKeysPerService: number;
  brave: SearchServiceCredential;
  tavily: SearchServiceCredential;
}

export interface WebSearchSettingsUpdate {
  provider?: WebSearchProvider;
  revision?: string;
  braveKeys?: SearchKeyUpdate[];
  tavilyKeys?: SearchKeyUpdate[];
  braveApiKey?: string;
  tavilyApiKey?: string;
  clearBraveApiKey?: boolean;
  clearTavilyApiKey?: boolean;
}

export interface ProviderBalance {
  provider: "deepseek";
  isAvailable: boolean;
  checkedAt: number;
  balances: {
    currency: "CNY" | "USD";
    totalBalance: string;
    grantedBalance: string;
    toppedUpBalance: string;
  }[];
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

export interface ProjectClosePayload extends NavigationPayload {
  /** The project entry that was removed from the rail list. */
  closedWorkspace: string;
  /** The workspace navigation authority now points at. */
  workspace: string;
  /** True when the closed project had been the active one. */
  activeWorkspaceChanged: boolean;
  recentWorkspaces: RecentWorkspace[];
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
  image?: { url: string; mediaType: string; width: number; height: number };
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
  title?: string;
  createdAt: number;
  updatedAt: number;
  firstMessage: string;
  lastMessage: string;
  messageCount: number;
  workspace: string;
  scope: ConversationScope;
  projectPath: string | null;
  projectId?: string;
  worktreeId?: string | null;
  worktreeStatus?: string | null;
  forkedFromId?: string | null;
  forkedAtTurnId?: string | null;
  model?: string;
  status: string;
  toolCallCount: number;
  runtimeSummary?: string;
  reviewPending?: boolean;
  checkpointCount?: number;
  instructionSummary?: string;
  hookSummary?: string;
  delegationSummary?: string;
  extensionSummary?: string;
  readinessSummary?: string;
  memorySummary?: string;
}

export interface TrashedSession extends SessionSummary {
  archivedAt: number;
  deletionPending: boolean;
}

export interface ToolInputFeedback {
  status: "success" | "invalid" | "schema_required" | "other";
  step: number;
  correctsCallIds: string[];
  corrects: Array<{ callId: string; step: number }>;
  issues: Array<{ path: Array<string | number>; expected: string; actual: string; example?: unknown }>;
  argumentError?: { classification: string; message: string; line?: number; column?: number;
    position?: number; rawArguments?: string; truncated?: boolean };
  recoveryElapsedMs?: number;
  /** Exact definition supplied in a later answered request; not tool execution. */
  schemaLoadedAtStep?: number;
  metrics?: Record<string, number | string | null>;
}

export interface ToolRecoveryFeedback {
  status: "retrying" | "agent_action" | "recovered" | "canceled" | "interrupted";
  kind: string;
  step: number;
  attempt?: number;
  maxAttempts?: number;
  retryDelayMs?: number;
  retryAfterSeconds?: number;
  elapsedMs?: number;
  attempts?: Array<{ attempt: number; output: string; timedOut: boolean; delayMs?: number }>;
  resolves?: Array<{ callId: string; step: number }>;
}

export interface CommandResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
  settled: boolean;
}

export interface CommandCheckReport {
  path: string;
  status: "passed" | "failed" | "unverified";
  reason?: string;
  failedChecks?: string[];
}

export interface StoredMessage {
  commandResult?: CommandResult;
  commandCheckReport?: CommandCheckReport;
  _userInputId?: string;
  event?: Omit<RunEvent, "sequence" | "timestamp">;
  images?: RunImage[];
  displayIndex?: number;
  truncatedFields?: string[];
  role: string;
  phase?: "commentary" | "final_answer";
  messageId?: string;
  deliveryState?: "pending" | "accepted" | "rejected";
  content?: string;
  toolName?: string;
  toolUseId?: string;
  toolInput?: ToolInputFeedback;
  toolRecovery?: ToolRecoveryFeedback;
  executionId?: string;
  nodeId?: string;
  attemptId?: string;
  input?: unknown;
  output?: string;
  isError?: boolean;
}

export interface TurnChangedFile {
  path: string;
  status: "A" | "M" | "D";
  additions: number | null;
  deletions: number | null;
  beforeSize: number;
  afterSize: number;
  binary: boolean;
  truncated: boolean;
  notice: string;
  /** Included only when the user opens the review. */
  diff?: string;
}

export interface TurnFileChanges {
  turnId: string;
  revision: number;
  files: TurnChangedFile[];
  additions: number;
  deletions: number;
  unmeasuredFiles: number;
  partial: boolean;
  complete: boolean;
  notice: string;
}

export interface TurnUndoState {
  turnId: string;
  state: "applied" | "undone" | "unavailable";
  revision: number;
  canUndo: boolean;
  canRestore: boolean;
  fileCount: number;
  reason: string;
}

export interface SessionTurn {
  displayRevision?: string;
  processMessages?: StoredMessage[];
  nextProcessAfter?: number | null;
  processTotal?: number;
  viewOnly?: boolean;
  processAvailable?: boolean;
  processMessageCount?: number;
  toolCallCount?: number;
  answerIndex?: number | null;
  fileChanges?: TurnFileChanges | null;
  turnId: string;
  prompt: string;
  status: string;
  /** Persisted turn timestamps are Unix seconds, unlike RunInfo's milliseconds. */
  createdAt?: number;
  completedAt?: number;
  canFork: boolean;
  reason: string;
  displayMessages: StoredMessage[];
}

export interface TurnsPage {
  revision?: string;
  turns: SessionTurn[];
  nextBefore: number | null;
  total: number;
}

export interface ConversationPage extends TurnsPage {
  session: SessionSummary;
}

export interface TurnMessagesPage {
  messages: StoredMessage[];
  nextAfter: number | null;
  total: number;
}

export interface WorktreeOperation {
  operationId: string;
  status: "preparing" | "ready" | "failed";
  kind: string;
  sessionId?: string;
  worktreeId?: string;
  error?: string;
  files?: string[];
  result?: { sessionId?: string; workspace?: string; worktree?: { includedFiles: string[] } };
}

export interface WorktreeChanges {
  worktree?: { includedFiles: string[] };
  previewId?: string;
  conflicts: string[];
  outsideProject: string[];
  files: Array<{ path: string; status: string; binary: boolean; beforeSize: number; afterSize: number;
    diff: string; truncated: boolean; conflict: boolean; outsideProject: boolean }>;
}

export interface CapabilitySkill {
  name: string;
  description: string;
  source: string;
  userInvocable?: boolean;
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

export interface ToolCatalogCapability extends ToolCapability {
  availability: "registered" | "conditional";
  activation: string;
}

export interface InstructionLayerView {
  name: string;
  scope: string;
  kind: string;
  exists: boolean;
  path?: string;
  status?: string;
  preview?: string;
  contentHash?: string;
  updatedAt?: number | null;
  error?: string;
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

export type ExtensionScope = "project" | "global";

export interface ExtensionHealthCheck {
  name: string;
  status: string;
  detail: string;
}

export interface ManagedExtension {
  id: string;
  key: string;
  name: string;
  scope: ExtensionScope;
  version: string;
  apiVersion: string;
  description: string;
  publisher: string;
  enabled: boolean;
  trusted: boolean;
  permissions: string[];
  grantedPermissions: string[];
  activationEvents: string[];
  dependencies: Record<string, string>;
  contributionKinds: string[];
  contributionCounts: Record<string, number>;
  source: string;
  manifestPath: string;
  integrity: string;
  health: string;
  healthDetail: string;
  shadowedBy: string | null;
  validationErrors: string[];
  checks: ExtensionHealthCheck[];
}

export interface ExtensionCommandContribution {
  name: string;
  usage: string;
  description: string;
  extensionId: string;
  scope: ExtensionScope;
}

export interface ExtensionAgentProfile {
  name: string;
  description: string;
  extensionId: string;
  scope: ExtensionScope;
  model: string | null;
  provider: string | null;
  executionProfile: string | null;
  toolAllow: string[];
  toolDeny: string[];
}

export interface ExtensionProviderContribution {
  id: string;
  label: string;
  extensionId: string;
  scope: ExtensionScope;
  transport: string;
  models: string[];
}

export interface ExtensionMcpDiagnostic {
  extension_id: string;
  contribution_kind: string;
  contribution_name: string;
  status: string;
  detail: string;
}

export interface ExtensionSkillContribution {
  name: string;
  description: string;
  path: string;
  extensionId: string;
  scope: ExtensionScope;
  availability: string;
}

export interface ExtensionToolContribution {
  name: string;
  runtimeName: string;
  description: string;
  effect: "read_only" | "workspace_write" | "external_side_effect" | "privileged" | string;
  replayPolicy: string;
  concurrencySafe: boolean;
  destructive: boolean;
  inputFields: string[];
  requiredInputs: string[];
  tags: string[];
  extensionId: string;
  scope: ExtensionScope;
  availability: string;
}

export interface ExtensionHookContribution {
  name: string;
  description: string;
  event: string;
  matchTools: string[];
  errorsOnly: boolean;
  timeoutSeconds: number | null;
  extensionId: string;
  scope: ExtensionScope;
  availability: string;
}

export type ExtensionUseTarget = "task" | "capabilities" | "runtime" | "context" | "settings";

export interface ExtensionUiContribution {
  name: string;
  label: string;
  description: string;
  path: string;
  surface: ExtensionUseTarget;
  extensionId: string;
  scope: ExtensionScope;
  availability: string;
}

export interface ExtensionUseRequest {
  target: ExtensionUseTarget;
  prompt?: string;
}

export interface ExtensionCatalog {
  installRoots?: Partial<Record<ExtensionScope, string>>;
  extensions: ManagedExtension[];
  commands: ExtensionCommandContribution[];
  agentProfiles: ExtensionAgentProfile[];
  providers: ExtensionProviderContribution[];
  skills: ExtensionSkillContribution[];
  tools: ExtensionToolContribution[];
  hooks: ExtensionHookContribution[];
  uiContributions: ExtensionUiContribution[];
  mcpDiagnostics: ExtensionMcpDiagnostic[];
}

export interface SkillInventoryItem {
  key: string;
  name: string;
  description: string;
  path: string;
  source: string;
  scope: ExtensionScope;
  extensionId: string;
  extensionKey: string | null;
  version: string;
  status: string;
  reason: string;
  error: string;
  effective: boolean;
  shadowedBy: string | null;
  userInvocable: boolean;
  modelInvocable: boolean;
  canInvoke: boolean;
  canInvokeInConversation: boolean;
  invocationReason: string;
  canPreview: boolean;
}

export interface SkillInventory {
  skills: SkillInventoryItem[];
  discoveryRoots: Array<{ path: string; source: string; scope: ExtensionScope; exists: boolean }>;
}

export interface SkillPreview {
  key: string;
  path: string;
  content: string;
  truncated: boolean;
  bytes: number;
}

export interface ExtensionMutationResponse extends ExtensionCatalog {
  extension?: ManagedExtension;
  result?: unknown;
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
  status?: string;
  entries?: MemoryEntryView[];
  taskHistory?: MemoryEntryView[];
  workspaceScope?: "project" | "standalone";
  scopes: Array<{
    name: string;
    entries: number;
    sizeBytes: number;
    categories: string[];
    status?: string;
    path?: string;
    error?: string;
  }>;
}

export interface MemoryEntryView {
  id: string; scope: string; category: string; content: string; origin: string; sources: string[];
  updatedAt: number; archived: boolean; revision: string; contentHash: string; path: string;
  title?: string; createdAt?: number; autoRetrieval?: boolean;
  retrievalStatus?: "eligible" | "legacy" | "archived" | "source_archived" | "source_missing" | "source_unavailable";
  sourceSessions?: Array<{ sessionId: string; status: "active" | "archived" | "missing" | "unavailable" }>;
  sourceTurns?: string[];
  learningStatus?: "eligible" | "archived" | "legacy_unverified" | "expired" | "source_changed" | "source_archived" | "source_missing" | "source_unavailable" | "source_unverified" | "confirmation_changed";
  reviewAfter?: number | null; retentionReason?: string; sourceVersions?: Record<string, string>;
}

export interface ContextSourceReceipt {
  requestId: string; runId: string; turnId: string; capturedAt: number;
  rulesKnown: boolean; memoryKnown: boolean; taskMemoryKnown: boolean;
  rules: Array<{ name: string; path: string; contentHash: string; status: string }>;
  memories: Array<{ id: string; scope: string; contentHash: string; kind?: string }>;
  notes: Array<{ id: string; type: string; text: string; sourceEventId: string; contentHash: string; truncated: boolean }>;
  taskReferences: Array<{ id: string; type: string; sourceEventId: string; contentHash: string }>;
}

export interface ContextKnowledgeView {
  rules: InstructionLayerView[];
  memory: MemoryView;
  sources?: ContextSourceReceipt | null;
  taskNotes?: TaskNoteInventory;
}

export interface SavedTaskNote {
  sourceEventId: string; contentHash: string; owner: string; revision: number;
  savedAt: number | null; reason: string; origin: "task_store" | "session_history"; latest: boolean;
  fields: {
    confirmed_findings: Array<{ text: string; source_event_ids: string[] }>;
    ruled_out: Array<{ text: string; source_event_ids: string[] }>;
    open_questions: string[]; next_steps: string[];
  };
}

export interface TaskNoteInventory {
  status: "ready" | "partial" | "unavailable";
  notes: SavedTaskNote[];
  truncated: boolean;
  error: string;
}

export interface CapabilitySnapshot {
  source: string;
  skills: CapabilitySkill[];
  mcpServers: McpServerCapability[];
  tools: ToolCapability[];
  toolCatalog?: ToolCatalogCapability[];
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
  budgetPolicy?: ContextBudgetPolicy | null;
  totalTokens: number;
  estimatedTokens: number;
  providerUsageTokens: number;
  contextWindow: number;
  effectiveInput: number;
  admissionInputTokens?: number | null;
  contextGuardTokens?: number | null;
  reservedTokens?: number | null;
  outputReserveTokens?: number | null;
  safetyReserveTokens?: number | null;
  capacityError?: string;
  utilization: number;
  warningLevel: string;
  request?: {
    requestId: string;
    nodeId: string;
    model: string;
    capturedAt: number;
    method: string;
    hasUnmeasuredMedia: boolean;
  } | null;
  breakdown?: Array<{ key: "system" | "tools" | "conversation" | "memory" | "other"; estimatedTokens: number }>;
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

export interface SubmittedCandidate {
  runId: string;
  artifactId?: string;
  revision?: string;
  workspace?: string;
  submitted: boolean;
  integrated: boolean;
  available: boolean;
  verificationStatus: string;
}

export interface CandidateRecovery {
  status: "preparing" | "needs_attention" | "restored";
  workspace?: string;
  submitted?: false;
  verified?: false;
  integrated?: false;
}

export interface RecoverableCandidate {
  runId: string;
  nodeId: string;
  attemptId: string;
  artifactId: string;
  contentHash: string;
  submitted: false;
  verified: false;
  integrated: false;
  recovery: CandidateRecovery | null;
}

export interface VerificationView {
  projectCheck?: {
    scope?: "build-only" | "browser-scenario" | "build-and-browser-smoke";
    criteria?: Array<{ number: number; text: string; status: "passed" | "failed" | "not_checked" }>;
    candidateRevision: string;
    execution: "executed" | "reused" | "not-started";
    failure: string;
    stages: Array<{ stage: string; exitCode: number | null; output: string }>;
    browserErrors: string[];
    artifacts: Array<{ name: string; artifactId: string }>;
  };
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
    file?: string | null;
  }>;
  deterministic: boolean;
  createdAt: number;
  coveredCriteriaIds?: string[];
}

export interface RuntimeCacheTotals {
  inputTokens: number | null;
  cacheReadTokens: number | null;
  hitRate: number | null;
  reportedCalls: number | null;
  modelCalls: number | null;
  complete: boolean;
}

export interface RuntimeCacheUsageView extends RuntimeCacheTotals {
  groups: (RuntimeCacheTotals & { key: "primary" | "agents" | "memory_rerank" | "other" })[];
}

export interface RuntimeUsageView {
  cache?: RuntimeCacheUsageView | null;
  tokens: number | null;
  cost: number | null;
  knownCost: number | null;
  tokensKnown: boolean;
  costKnown: boolean;
  modelCalls: number | null;
  reservedTokens: number | null;
  reservedCost: number | null;
  unsettledCalls: number | null;
}

export interface RuntimeMetricsView {
  elapsedSeconds: number | null;
  modelCalls: number | null;
  modelSeconds: number | null;
  toolCalls: number | null;
  toolSeconds: number | null;
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
  reviewPolicy?: ReviewPolicyView;
  reviewStatus?: "unavailable";
  criticality?: "required" | "advisory";
  diagnosis?: NodeFailureDiagnosis;
  advisoryReason?: string;
  attemptCount: number;
  recovery?: ChildRecovery | null;
  rejectionReasons: string[];
  result: null | {
    status: string;
    outputSummary: string;
    changedResources: string[];
    artifacts: ArtifactView[];
    verificationRecords: VerificationView[];
    warnings: string[];
    usage?: RuntimeUsageView | null;
    metrics?: RuntimeMetricsView | null;
    failure: null | {
      kind: string;
      message: string;
      retryable: boolean;
      ambiguousSideEffect: boolean;
    };
  };
}

export interface TaskMemoryView {
  status: "stored" | "already_recorded" | "skipped" | "failed" | "invalid";
  label: string;
  detail: string;
  reason?: string;
  runId?: string;
  entryId?: string;
  scope?: "local";
  contentSha256?: string;
  evidenceSha256?: string;
}

export interface AgentTeamView {
  team_id: string;
  lead_id: string;
  state: string;
  member_limit: number;
  members: { session_id: string; current_task_id: string; task_ids?: string[]; generation: number; role: string; state: string; worker_exited: boolean }[];
}

export interface AgentMessageView {
  messageId: string;
  senderId: string;
  recipientId: string;
  kind: string;
  state: string;
  sequence: number;
  content?: string;
  reason?: string;
  replyTo?: string;
}

export interface AgentTaskView {
  localCheckStatus?: "passed" | "failed" | "pending" | "not_requested";
  taskId: string;
  sessionId: string;
  generation: number;
  sessionOriginRunId?: string;
  recoveredFromRunId?: string;
  inputRevision: string;
  workspaceMode?: "shared" | "snapshot" | "worktree";
  state: string;
  objective: string;
  name?: string;
  continuationAvailable?: boolean;
  sessionState?: string;
  model?: { model?: string; provider?: string };
  writeScope?: string[];
  acceptanceCriteria?: string[];
  resultHandling?: "delivered" | "handled" | null;
  checks?: { id: string; operationId: string; state: string; passed: boolean; exitCode?: number | null }[];
}

export interface AgentCollaborationPolicy {
  mode: "off" | "async" | "teams";
  primaryRole: "worker" | "coordinator";
  teamWrites: boolean;
  fork: boolean;
}

export interface ReviewPolicyView {
  scope: "basic" | "focused" | "full";
  required: boolean;
}

export interface RuntimeTraceView {
  reviewPending?: boolean;
  reviewCloseout?: string;
  continuationContractSaved?: boolean;
  continuation?: { sourceRunId: string; retainedTokens: number; retainedTokensKnown?: boolean; unsettledCalls?: number | null; notice: string } | null;
  agentCollaboration?: {
    waits?: AgentWaitView[];
    team: AgentTeamView | null;
    agents: AgentTaskView[];
    messages: AgentMessageView[];
    error?: string;
  };
  runBudget?: RunBudget | null;
  taskFile?: TaskFileSummary | null;
  schemaVersion: string;
  runId: string;
  webRunId: string;
  closureBudget?: {
    primaryTokens: number | null;
    verificationTokens: number | null;
    primaryCost: number | null;
    verificationCost: number | null;
  };
  profile: {
    collaboration?: AgentCollaborationPolicy;
    requestedMode: string;
    selectedRoute: string;
    reasoningBudget: string;
    maxActiveWorkers: number;
    verificationLevel: string;
    primaryRuntimeProfile?: string;
    reviewPolicy?: ReviewPolicyView;
    writeIsolation: string;
    maxTokens?: number | null;
    maxCost?: number | null;
    deadline?: number | null;
  };
  topology: {
    selectedRoute: string;
    expectedParallelGain: number;
    writeConflictRisk: string;
    deterministicVerifiability: boolean;
    requiresWorkspaceWrite?: boolean;
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
    usage?: RuntimeUsageView | null;
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
    retryFeedback?: string[];
  }>;
  taskMemory?: TaskMemoryView | null;
  acceptanceCriteria?: Array<{ id: string; text: string }>;
  expectedOutputs?: Array<string | null>;
  reasoningBudgetDecisions?: Array<{
    nodeId: string;
    model: string;
    provider: string;
    requested: string;
    applied: string;
    degraded: boolean;
    reason: string;
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
  contextSources?: ContextSourceReceipt | null;
  taskNotes?: TaskNoteInventory;
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

export interface NodeFailureDiagnosis {
  category?: string;
  failureKind?: string;
  fallbackAllowed?: boolean;
  acknowledged?: boolean;
}

export interface ExecutionNode {
  nodeId: string;
  objective: string;
  role: string;
  dependencies: string[];
  state: string;
  reviewPolicy?: ReviewPolicyView;
  reviewStatus?: "unavailable";
  criticality?: "required" | "advisory";
  diagnosis?: NodeFailureDiagnosis;
  advisoryReason?: string;
  attemptId?: string | null;
  attemptCount: number;
  recovery?: ChildRecovery | null;
  continuationCount?: number;
  resumeCredits?: number;
  repairCount?: number;
  summary: string;
  failure?: { kind: string; message: string } | null;
}

export interface ChildRecovery {
  status: "retrying" | "recovered" | "exhausted";
  failedAttemptIds: string[];
}

export interface AgentWaitView {
  waitId: string;
  nodeId: string;
  status: string;
}

export interface ExecutionSnapshot {
  agentWaits?: AgentWaitView[];
  collaboration?: AgentCollaborationPolicy;
  agentTeam?: AgentTeamView;
  agentTasks?: AgentTaskView[];
  agentMessages?: { message_id: string; sender_id: string; recipient_id: string; kind: string; state: string; sequence: number; reply_to?: string }[];
  executionId: string;
  planVersion: number;
  profile: ExecutionProfile;
  route: string;
  phase: string;
  detail?: string;
  phaseStartedAt?: number;
  routeReasons: string[];
  nodes: ExecutionNode[];
  consultationMode?: "durable-batch";
  maxActiveWorkers?: number;
  repairCount?: number;
  repairLimit?: number;
  workspaceDelivery?: "candidate-first";
  integrationStatus?: string;
}

export interface RunInput {
  contextSettings?: ConversationContext;
  inputId: string;
  kind: "steer" | "supplement" | "next_task";
  text: string;
  executionProfile: ExecutionProfile;
  status: "accepted" | "starting" | "applied" | "completed" | "failed" | "held" | "canceled";
  updatedAt?: number;
  sequence?: number;
  version?: number;
  queueMode?: "automatic" | "manual";
  task_revision?: number;
  resolution?: { action: "continue" | "amend"; response: string };
  reason?: string;
}

export type ExecutionLocation = "project" | "isolated";

export interface RunInfo {
  reviewCloseout?: string;
  reviewPending?: boolean;
  images?: RunImage[];
  restored?: boolean;
  permissionMode?: PermissionMode;
  executionLocation?: ExecutionLocation | null;
  contextSettings?: ConversationContext | null;
  fileChanges?: TurnFileChanges | null;
  runBudget?: RunBudget | null;
  taskFile?: TaskFileSummary | null;
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
  execution?: ExecutionSnapshot | null;
  inputs?: RunInput[];
  inputProtocol?: number;
  queueProtocol?: number;
  acceptingInputs?: boolean;
  acceptingSteering?: boolean;
  waitingModels?: string[];
  pendingApprovals?: RunEvent[];
  pendingQuestions?: RunEvent[];
  historySequence?: number;
  eventHistoryAvailable?: boolean;
  historyCount?: number;
}

export interface RunSnapshot {
  hasMoreEvents?: boolean;
  run: RunInfo | null;
  events: RunEvent[];
  historyMessages?: StoredMessage[];
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
  commandResult?: CommandResult;
  commandCheckReport?: CommandCheckReport;
  questionGroupId?: string;
  questionIndex?: number;
  questionCount?: number;
  question?: { question: string; options?: string[]; recommendedOption?: string };
  answer?: string | null;
  resolution?: "answered" | "canceled";
  images?: RunImage[];
  acceptingSteering?: boolean;
  toolInput?: ToolInputFeedback;
  toolRecovery?: ToolRecoveryFeedback;
  fileChanges?: TurnFileChanges;
  turnId?: string;
  kind: string;
  phase?: "commentary" | "final_answer";
  messageId?: string;
  replacesMessageId?: string;
  deliveryState?: "pending" | "accepted" | "rejected";
  sequence: number;
  timestamp: number;
  step?: number | null;
  executionId?: string;
  nodeId?: string;
  attemptId?: string;
  toolCallId?: string;
  execution?: ExecutionSnapshot;
  runInput?: RunInput;
  modelState?: "waiting" | "received" | "interrupted";
  content?: string;
  message?: string;
  status?: RunStatus;
  /** Immutable outcome of the containing saved turn, separate from this event. */
  turnStatus?: RunStatus;
  progressStatus?: "running" | "complete" | "failed" | "stopped";
  stopReason?: string;
  reviewPending?: boolean;
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

export interface TimelineOwner {
  executionId?: string;
  nodeId?: string;
  attemptId?: string;
}

export type ImageAvailability = "available" | "cleared" | "missing" | "corrupt";
export interface ImageStatus { id: string; status: ImageAvailability; clearedAt?: number }
export interface ImageStorageSession {
  sessionId: string;
  revision: string;
  title: string;
  workspaceName: string;
  updatedAt: number;
  archived: boolean;
  running: boolean;
  canClear: boolean;
  error: string;
  storedCount: number | null;
  storedBytes: number | null;
}
export interface ImageStorageView {
  sessions: ImageStorageSession[];
  storedBytes: number;
  unknownSessionCount: number;
  retention: "until-cleared-or-session-deleted";
}

export interface RunImage {
  availability?: ImageAvailability;
  clearedAt?: number;
  capture?: {
    schema: "minicode.browser-capture.v1";
    imageSha256: string;
    url: string;
    scenario: string;
    view: string;
    viewport: { width: number; height: number };
    deviceScaleFactor: number;
    browser: string;
    browserVersion: string;
    sourceRevision: string;
    sourceBinding: string;
    capturedAt: number;
    region: string;
    ready: boolean;
    readyCondition: string;
  };
  sourceImageId?: string;
  sourceCreatedAt?: number;
  id: string;
  sessionId: string;
  name: string;
  caption: string;
  mediaType: string;
  size: number;
  width: number;
  height: number;
  createdAt: number;
  runId?: string;
  webRunId?: string;
  turnId?: string;
  nodeId?: string;
  attemptId?: string;
  toolCallId?: string;
  toolName?: string;
}

export type TimelineItem = TimelineOwner & (
  | { id: string; type: "images"; images: RunImage[]; timestamp: number; toolCallId?: string; toolName?: string }
  | { id: string; type: "user"; content: string; timestamp: number; steeringInputId?: string }
  | { id: string; type: "assistant"; content: string; timestamp: number; streaming?: boolean; phase?: "commentary" | "final_answer"; messageId?: string; deliveryState?: "pending" | "accepted" | "rejected"; lastSequence?: number }
  | {
      id: string;
      type: "tool";
      commandResult?: CommandResult;
      commandCheckReport?: CommandCheckReport;
      toolName: string;
      input: unknown;
      output: string;
      timestamp: number;
      status: "running" | "success" | "error" | "unknown";
      durationMs?: number | null;
      step?: number | null;
      executionId?: string;
      nodeId?: string;
      attemptId?: string;
      toolCallId?: string;
      startOrder?: number;
      resultOrder?: number;
      toolInput?: ToolInputFeedback;
      toolRecovery?: ToolRecoveryFeedback;
    }
  | {
      id: string;
      type: "progress";
      runtimeProgressEcho?: { step: number; content: string };
      stopReason?: string;
      questionId?: string;
      questionTurnId?: string;
      questionRunId?: string;
      questionGroupId?: string;
      questionIndex?: number;
      questionCount?: number;
      question?: RunEvent["question"];
      questionAnswer?: string | null;
      questionResolution?: RunEvent["resolution"];
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
  | { id: string; type: "error"; content: string; timestamp: number });

export interface BootstrapPayload {
  conversationViewVersion?: number;
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
