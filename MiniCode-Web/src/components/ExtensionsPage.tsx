import {
  ArrowLeft,
  ArrowUpRight,
  Box,
  Blocks,
  Check,
  CircleAlert,
  CircleCheck,
  ChevronDown,
  FolderOpen,
  Info,
  LoaderCircle,
  MessageSquare,
  Monitor,
  Package,
  Power,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Shield,
  ShieldAlert,
  Sparkles,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api/client";
import { SkillCatalog } from "./SkillCatalog";
import { WorkspacePage, type WorkspacePageNavigation } from "./WorkspacePage";
import { ExtensionInstallForm, type ExtensionImportKind as InstallKind } from "./ExtensionInstallForm";
import "./CapabilityManager.css";
import type {
  ConversationScope,
  ExtensionCatalog,
  ExtensionHealthCheck,
  ExtensionMutationResponse,
  ExtensionScope,
  ExtensionUseRequest,
  ManagedExtension,
  RecentWorkspace,
} from "../types";

interface ExtensionsPageProps extends WorkspacePageNavigation {
  embedded?: boolean;
  initialView?: "skills" | "extensions";
  workspace: string;
  scope: ConversationScope;
  projects: RecentWorkspace[];
  onUseSkill: (command: string) => void;
  onChanged?: () => Promise<void> | void;
  onUseContribution?: (request: ExtensionUseRequest) => void;
}

type Notice = { kind: "success" | "error"; text: string } | null;
type ScopeFilter = "all" | ExtensionScope;
type CapabilityFilter = "all" | CapabilityGroup;
type CapabilityGroup = "skills" | "tools" | "hooks" | "ui" | "other";
type CatalogView = "skills" | "extensions";

interface CapabilityListItem {
  id: string;
  group: CapabilityGroup;
  name: string;
  description: string;
  metadata: string[];
  availability: string;
  target: ExtensionUseRequest["target"];
  prompt?: string;
  kind: string;
}

const HEALTH_LABELS: Record<string, string> = {
  ready: "可用",
  invalid: "配置错误",
  disabled: "已停用",
  untrusted: "待信任",
  permission_required: "待授权",
  shadowed: "被覆盖",
  integrity_mismatch: "完整性异常",
};

const HEALTH_COPY: Record<string, string> = {
  ready: "完整性、依赖、信任和权限检查均已通过。",
  invalid: "扩展清单存在错误，修正后才能加载贡献。",
  disabled: "扩展保留在本机，但当前不会向 MiniCode 提供能力。",
  untrusted: "扩展尚未获信任，不会执行代码或读取受保护资源。",
  permission_required: "扩展缺少已声明权限，需要明确授权后才能运行。",
  shadowed: "同 ID 的项目扩展优先，当前版本不会被加载。",
  integrity_mismatch: "当前文件与安装记录不一致，请检查来源或回滚。",
};

const CAPABILITY_GROUPS: Array<{
  key: CapabilityGroup;
  label: string;
  title: string;
  description: string;
}> = [
  { key: "skills", label: "技能", title: "技能", description: "按需加载的工作流与领域方法" },
  { key: "tools", label: "工具", title: "工具", description: "由模型按任务需要调用的能力" },
  { key: "hooks", label: "钩子", title: "自动钩子", description: "在指定事件发生时自动运行" },
  { key: "ui", label: "界面", title: "界面入口", description: "通往 MiniCode 内置界面的入口" },
  { key: "other", label: "其他", title: "其他集成", description: "命令、Agent、模型提供商与 MCP 服务" },
];

const CONTRIBUTION_LABELS: Record<string, string> = {
  skills: "技能", tools: "工具", hooks: "钩子", ui: "界面入口",
  commands: "命令", agents: "Agent", providers: "模型提供商", mcpServers: "MCP 连接",
};

const EFFECT_LABELS: Record<string, string> = {
  read_only: "只读",
  workspace_write: "写入工作区",
  external_side_effect: "外部操作",
  privileged: "需授权",
};

const HOOK_EVENT_LABELS: Record<string, string> = {
  pre_tool_use: "工具调用前",
  post_tool_use: "工具调用后",
  agent_start: "Agent 启动时",
  agent_stop: "Agent 结束时",
  subagent_start: "子 Agent 启动时",
  subagent_stop: "子 Agent 结束时",
  session_save: "会话保存时",
  session_resume: "会话恢复时",
  user_input: "收到输入时",
  assistant_output: "生成回复时",
  startup: "应用启动时",
  shutdown: "应用关闭时",
};

const SURFACE_LABELS: Record<ExtensionUseRequest["target"], string> = {
  task: "任务输入框",
  capabilities: "能力检查器",
  runtime: "运行检查器",
  context: "上下文检查器",
  settings: "模型设置",
};

const PERMISSION_COPY: Record<string, string> = {
  "filesystem:read": "读取扩展允许范围内的文件。",
  "filesystem:write": "修改扩展允许范围内的文件。",
  "network:access": "向网络服务发送请求。",
  "process:execute": "启动本机进程或命令。",
  "process:spawn": "启动扩展声明的本机工具或钩子进程。",
  "secrets:read": "通过 secretRef 使用本机密钥，不返回明文。",
};

function workspaceName(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts.at(-1) || path || "当前工作区";
}

function scopeLabel(scope: ExtensionScope): string {
  return scope === "project" ? "项目" : "全局";
}

function healthLabel(health: string): string {
  return HEALTH_LABELS[health] || health || "未知";
}

function healthTone(health: string): string {
  if (health === "ready") return "ready";
  if (health === "disabled" || health === "shadowed") return "neutral";
  if (health === "untrusted" || health === "permission_required") return "warning";
  return "danger";
}

function checkLabel(status: string): string {
  if (status === "pass") return "通过";
  if (status === "blocked") return "受阻";
  if (status === "fail") return "失败";
  return status;
}

function samePermissions(left: string[], right: string[]): boolean {
  return [...left].sort().join("\u0000") === [...right].sort().join("\u0000");
}

function compactMetadata(...items: Array<string | false | null | undefined>): string[] {
  return items.filter((item): item is string => Boolean(item));
}

function CapabilityIcon({ group }: { group: CapabilityGroup }) {
  if (group === "skills") return <Sparkles size={16} />;
  if (group === "tools") return <Wrench size={16} />;
  if (group === "hooks") return <Power size={16} />;
  if (group === "ui") return <Box size={16} />;
  return <Package size={16} />;
}

function StatusIcon({ health }: { health: string }) {
  if (health === "ready") return <CircleCheck size={16} />;
  if (health === "disabled" || health === "shadowed") return <Power size={16} />;
  if (health === "untrusted" || health === "permission_required") {
    return <ShieldAlert size={16} />;
  }
  return <CircleAlert size={16} />;
}

function CheckIcon({ status }: { status: string }) {
  if (status === "pass") return <Check size={15} />;
  if (status === "blocked") return <ShieldAlert size={15} />;
  return <CircleAlert size={15} />;
}

export function ExtensionsPage(props: ExtensionsPageProps) {
  return <ExtensionManagement key={`${props.scope}:${props.workspace}`} {...props} />;
}

function ExtensionManagement(props: ExtensionsPageProps) {
  const [workspace, setWorkspace] = useState(props.scope === "project" ? props.workspace : "");
  const [view, setView] = useState<CatalogView>(props.initialView || "skills");
  const [skillFocus, setSkillFocus] = useState<{ name: string; extension: string } | null>(null);
  const [skillFocusVersion, setSkillFocusVersion] = useState(0);
  function focusSkill(focus: { name: string; extension: string } | null) {
    setSkillFocus(focus); setSkillFocusVersion(value => value + 1);
  }
  const projects = [...props.projects];
  if (props.workspace && !projects.some(project => project.path === props.workspace)) {
    projects.unshift({path: props.workspace, name: workspaceName(props.workspace)});
  }
  return <ExtensionsContext key={workspace} {...props} projects={projects} workspace={workspace}
    scope={workspace ? "project" : "standalone"} invocationWorkspace={props.workspace} invocationScope={props.scope}
    onScopeChange={(next, focus) => { focusSkill(focus || null); setWorkspace(next); }}
    skillFocus={skillFocus} skillFocusVersion={skillFocusVersion} onSkillFocusChange={focusSkill} view={view} onViewChange={setView} />;
}

interface ContextProps extends ExtensionsPageProps {
  invocationWorkspace: string;
  invocationScope: ConversationScope;
  onScopeChange: (workspace: string, focus?: { name: string; extension: string }) => void;
  skillFocus: { name: string; extension: string } | null;
  skillFocusVersion: number;
  onSkillFocusChange: (focus: { name: string; extension: string } | null) => void;
  view: CatalogView;
  onViewChange: (view: CatalogView) => void;
}

function ExtensionsContext({
  workspace,
  scope,
  projects,
  invocationWorkspace,
  invocationScope,
  onScopeChange,
  skillFocus,
  skillFocusVersion,
  onSkillFocusChange: setSkillFocus,
  view,
  onViewChange: setView,
  onUseSkill,
  onBack,
  onOpenRail,
  railCollapsed,
  embedded,
  onChanged,
  onUseContribution,
}: ContextProps) {
  const [catalog, setCatalog] = useState<ExtensionCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedKey, setSelectedKey] = useState("");
  const [search, setSearch] = useState("");
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>("all");
  const [capabilityFilter, setCapabilityFilter] = useState<CapabilityFilter>("all");
  const [installOpen, setInstallOpen] = useState(false);
  const [installKind, setInstallKind] = useState<InstallKind>("package");
  const [permissionDraft, setPermissionDraft] = useState<string[]>([]);
  const [busyAction, setBusyAction] = useState("");
  const [confirmUninstall, setConfirmUninstall] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [skillRevision, setSkillRevision] = useState(0);
  const [extensionDetailOpen, setExtensionDetailOpen] = useState(false);
  const extensionList = useRef<HTMLDivElement>(null);
  const extensionDetail = useRef<HTMLElement>(null);
  const accessSection = useRef<HTMLElement>(null);
  const diagnostics = useRef<HTMLDetailsElement>(null);
  const noInstalledExtensions = Boolean(!loading && catalog && !catalog.extensions.length);
  const managementLabel = scope === "standalone" ? "本机全局" : `项目：${projects.find(project => project.path === workspace)?.name || workspaceName(workspace)}`;
  const conversationLabel = invocationScope === "standalone" ? "独立对话" : projects.find(project => project.path === invocationWorkspace)?.name || workspaceName(invocationWorkspace);
  const suggestedProject = projects.find(project => project.path === invocationWorkspace) || projects[0];
  const installScope = scope === "standalone" ? "global" : "project";
  const outsideInvocationContext = scope !== invocationScope || (scope === "project" && workspace !== invocationWorkspace);

  function adoptCatalog(next: ExtensionCatalog, preferredKey?: string) {
    setCatalog(next);
    setSelectedKey((current) => {
      const candidate = preferredKey || current;
      if (candidate && next.extensions.some((item) => item.key === candidate)) return candidate;
      return next.extensions[0]?.key || "";
    });
  }

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setNotice(null);
    api.getExtensions(workspace, scope)
      .then((next) => {
        if (cancelled) return;
        adoptCatalog(next);
      })
      .catch((reason) => {
        if (!cancelled) {
          setCatalog(null);
          setNotice({
            kind: "error",
            text: reason instanceof Error ? reason.message : "无法读取扩展目录。",
          });
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspace, scope]);

  useEffect(() => { if (view === "extensions") searchRef.current?.focus(); }, [view]);

  const filteredExtensions = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return (catalog?.extensions || []).filter((extension) => {
      if (scopeFilter !== "all" && extension.scope !== scopeFilter) return false;
      if (!query) return true;
      return [extension.name, extension.id, extension.description, extension.publisher]
        .join(" ")
        .toLocaleLowerCase()
        .includes(query);
    });
  }, [catalog, scopeFilter, search]);

  const selected = useMemo(
    () => filteredExtensions.find(item => item.key === selectedKey) || filteredExtensions[0] || null,
    [filteredExtensions, selectedKey],
  );

  useEffect(() => {
    setPermissionDraft(selected?.grantedPermissions || []);
    setConfirmUninstall(false);
    setCapabilityFilter("all");
  }, [selected?.key, selected?.grantedPermissions.join("\u0000")]);

  const totals = useMemo(() => {
    const extensions = catalog?.extensions || [];
    return {
      all: extensions.length,
      active: extensions.filter((item) => item.enabled && item.health === "ready").length,
      attention: extensions.filter((item) => item.health !== "ready").length,
    };
  }, [catalog]);

  const related = useMemo(() => {
    if (!selected || !catalog) {
      return {
        skills: [], tools: [], hooks: [], ui: [], commands: [], agents: [], providers: [], mcp: [],
      };
    }
    return {
      skills: (catalog.skills || []).filter(
        (item) => item.extensionId === selected.id && item.scope === selected.scope,
      ),
      tools: (catalog.tools || []).filter(
        (item) => item.extensionId === selected.id && item.scope === selected.scope,
      ),
      hooks: (catalog.hooks || []).filter(
        (item) => item.extensionId === selected.id && item.scope === selected.scope,
      ),
      ui: (catalog.uiContributions || []).filter(
        (item) => item.extensionId === selected.id && item.scope === selected.scope,
      ),
      commands: catalog.commands.filter(
        (item) => item.extensionId === selected.id && item.scope === selected.scope,
      ),
      agents: catalog.agentProfiles.filter(
        (item) => item.extensionId === selected.id && item.scope === selected.scope,
      ),
      providers: catalog.providers.filter(
        (item) => item.extensionId === selected.id && item.scope === selected.scope,
      ),
      mcp: catalog.mcpDiagnostics.filter((item) => item.extension_id === selected.id),
    };
  }, [catalog, selected]);

  const capabilityItems = useMemo(() => {
    const groups: Record<CapabilityGroup, CapabilityListItem[]> = {
      skills: [], tools: [], hooks: [], ui: [], other: [],
    };
    if (!selected) return groups;

    groups.skills = related.skills.map((skill) => ({
      id: `skill-${skill.name}`,
      group: "skills",
      name: skill.name,
      description: skill.description || "按需加载此 Skill 提供的工作流。",
      metadata: compactMetadata("按需触发", skill.path),
      availability: skill.availability,
      target: "task",
      prompt: "",
      kind: "Skill",
    }));
    groups.tools = related.tools.map((tool) => ({
      id: `tool-${tool.runtimeName || tool.name}`,
      group: "tools",
      name: tool.name,
      description: tool.description || "模型可在任务执行中调用此工具。",
      metadata: compactMetadata(
        EFFECT_LABELS[tool.effect] || tool.effect,
        tool.inputFields.length ? `${tool.inputFields.length} 个输入` : "无需输入",
        tool.concurrencySafe && "可并发",
        tool.destructive && "破坏性操作",
      ),
      availability: tool.availability,
      target: "task",
      prompt: `请使用工具「${tool.runtimeName || tool.name}」完成：`,
      kind: "工具",
    }));
    groups.hooks = related.hooks.map((hook) => ({
      id: `hook-${hook.event}-${hook.name}`,
      group: "hooks",
      name: hook.name,
      description: hook.description || "在指定生命周期事件发生时自动运行。",
      metadata: compactMetadata(
        HOOK_EVENT_LABELS[hook.event] || hook.event,
        hook.errorsOnly && "仅失败时",
        hook.matchTools.length ? `${hook.matchTools.length} 个工具范围` : "全部工具",
        hook.timeoutSeconds ? `${hook.timeoutSeconds} 秒超时` : null,
      ),
      availability: hook.availability,
      target: "context",
      kind: "钩子",
    }));
    groups.ui = related.ui.map((ui) => ({
      id: `ui-${ui.name}`,
      group: "ui",
      name: ui.label || ui.name,
      description: ui.description || "打开扩展声明的 MiniCode 界面入口。",
      metadata: compactMetadata("声明式界面", SURFACE_LABELS[ui.surface], ui.path),
      availability: ui.availability,
      target: ui.surface,
      prompt: ui.surface === "task" ? `使用界面入口「${ui.label || ui.name}」完成：` : undefined,
      kind: "界面入口",
    }));
    groups.other = [
      ...related.commands.map((command) => ({
        id: `command-${command.name}`,
        group: "other" as const,
        name: command.name,
        description: command.description || "在任务输入框中使用此命令。",
        metadata: compactMetadata(command.usage),
        availability: selected.health,
        target: "task" as const,
        prompt: `${command.usage} `,
        kind: "命令",
      })),
      ...related.agents.map((agent) => ({
        id: `agent-${agent.name}`,
        group: "other" as const,
        name: agent.name,
        description: agent.description || "使用预设的 Agent 配置处理任务。",
        metadata: compactMetadata(agent.model, agent.provider, agent.executionProfile),
        availability: selected.health,
        target: "task" as const,
        prompt: `使用 Agent 配置「${agent.name}」完成：`,
        kind: "Agent 预设",
      })),
      ...related.providers.map((provider) => ({
        id: `provider-${provider.id}`,
        group: "other" as const,
        name: provider.label,
        description: "在模型设置中选择此提供商及其可用模型。",
        metadata: compactMetadata(
          provider.transport,
          provider.models.length > 0 && `${provider.models.length} 个模型`,
        ),
        availability: selected.health,
        target: "settings" as const,
        kind: "模型提供商",
      })),
      ...related.mcp.map((diagnostic, index) => ({
        id: `mcp-${diagnostic.contribution_name}-${index}`,
        group: "other" as const,
        name: diagnostic.contribution_name,
        description: diagnostic.detail || "通过 MCP 向模型提供外部工具或数据。",
        metadata: compactMetadata("MCP 服务", diagnostic.status),
        availability: diagnostic.status === "ready" ? "ready" : "permission_required",
        target: "capabilities" as const,
        kind: "MCP 服务",
      })),
    ];
    return groups;
  }, [related, selected]);

  const capabilityCounts = useMemo(() => ({
    all: CAPABILITY_GROUPS.reduce((sum, group) => sum + capabilityItems[group.key].length, 0),
    skills: capabilityItems.skills.length,
    tools: capabilityItems.tools.length,
    hooks: capabilityItems.hooks.length,
    ui: capabilityItems.ui.length,
    other: capabilityItems.other.length,
  }), [capabilityItems]);

  const visibleCapabilityGroups = CAPABILITY_GROUPS.filter((group) => (
    (capabilityFilter === "all" || capabilityFilter === group.key)
      && capabilityItems[group.key].length > 0
  ));

  async function refreshCatalog() {
    setBusyAction("refresh-catalog");
    setNotice(null);
    try {
      const next = await api.getExtensions(workspace, scope);
      adoptCatalog(next);
      setSkillRevision(value => value + 1);
      setNotice({ kind: "success", text: "扩展目录已刷新。" });
    } catch (reason) {
      setNotice({
        kind: "error",
        text: reason instanceof Error ? reason.message : "刷新扩展目录失败。",
      });
    } finally {
      setBusyAction("");
    }
  }

  async function mutate(
    action: string,
    operation: () => Promise<ExtensionMutationResponse>,
    successText: string,
  ) {
    setBusyAction(action);
    setNotice(null);
    try {
      const next = await operation();
      adoptCatalog(next, next.extension?.key || selectedKey);
      setSkillRevision(value => value + 1);
      setNotice({ kind: "success", text: successText });
      await onChanged?.();
    } catch (reason) {
      setNotice({
        kind: "error",
        text: reason instanceof Error ? reason.message : "扩展操作失败。",
      });
    } finally {
      setBusyAction("");
    }
  }

  async function refreshHealth(extension: ManagedExtension) {
    setBusyAction("health");
    setNotice(null);
    try {
      const result = await api.getExtensionHealth(
        extension.scope,
        extension.id,
        workspace,
        scope,
      );
      setCatalog((current) => current && ({
        ...current,
        extensions: current.extensions.map((item) => (
          item.key === result.extension.key
            ? { ...result.extension, checks: result.checks }
            : item
        )),
      }));
      setNotice({ kind: "success", text: "健康检查已完成。" });
    } catch (reason) {
      setNotice({
        kind: "error",
        text: reason instanceof Error ? reason.message : "健康检查失败。",
      });
    } finally {
      setBusyAction("");
    }
  }

  function togglePermission(permission: string) {
    setPermissionDraft((current) => (
      current.includes(permission)
        ? current.filter((item) => item !== permission)
        : [...current, permission]
    ));
  }

  const permissionChanged = Boolean(
    selected && !samePermissions(permissionDraft, selected.grantedPermissions),
  );
  const busy = Boolean(busyAction);

  function openInstall(kind: InstallKind) {
    setInstallKind(kind);
    setInstallOpen(true);
  }

  function showAccess() {
    accessSection.current?.scrollIntoView({ block: "start" });
    accessSection.current?.focus({ preventScroll: true });
  }

  function showDiagnostics() {
    if (!diagnostics.current) return;
    diagnostics.current.open = true;
    diagnostics.current.scrollIntoView({ block: "start" });
    diagnostics.current.querySelector("summary")?.focus({ preventScroll: true });
  }

  const importForm = installOpen ? <ExtensionInstallForm workspace={workspace} contextScope={scope}
    destinationLabel={managementLabel} destinationRoot={catalog?.installRoots?.[installScope]} kind={installKind}
    onKindChange={setInstallKind} onCancel={() => setInstallOpen(false)} onBusy={value => setBusyAction(value ? "install" : "")}
    onInstalled={(next, kind) => {
      adoptCatalog(next, next.extension?.key);
      setBusyAction(""); setInstallOpen(false); setSearch(""); setScopeFilter("all");
      setNotice({kind: "success", text: `${kind === "mcp" ? "MCP 已添加" : "导入完成"}，保存到${managementLabel}。请检查内容、信任与权限后使用。`});
      if (kind === "skill") {
        const skill = next.skills.find(item => item.extensionId === next.extension?.id && item.scope === next.extension?.scope);
        setSkillFocus(next.extension ? {name: skill?.name || "", extension: next.extension.key} : null);
        setSkillRevision(value => value + 1); setView("skills");
      } else { setExtensionDetailOpen(true); setView("extensions"); }
      void onChanged?.();
    }} /> : null;

  return (
    <WorkspacePage className={`extensions-page ${view === "extensions" ? "is-extension-view" : ""} ${installOpen ? "is-adding-capability" : ""}`} title="扩展与能力" icon={Blocks} headingId="extensions-title"
      description="选择技能用于对话，管理扩展的安装、信任与权限。"
      onBack={onBack} onOpenRail={onOpenRail} railCollapsed={railCollapsed} embedded={embedded}>
        <div className="capability-catalog-heading">
          <div className="capability-view-tabs" role="tablist" aria-label="能力目录" onKeyDown={event => {
            if (busy || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === "Home" ? "skills" : event.key === "End" ? "extensions" : view === "skills" ? "extensions" : "skills";
            setInstallOpen(false); setView(next);
            event.currentTarget.querySelector<HTMLButtonElement>(`#capability-${next}-tab`)?.focus();
          }}>
            <button id="capability-skills-tab" role="tab" aria-selected={view === "skills"} aria-controls="capability-skills-panel" tabIndex={view === "skills" ? 0 : -1} disabled={busy} onClick={() => { setInstallOpen(false); setView("skills"); }}><Sparkles size={16} />技能</button>
            <button id="capability-extensions-tab" role="tab" aria-selected={view === "extensions"} aria-controls="capability-extensions-panel" tabIndex={view === "extensions" ? 0 : -1} disabled={busy} onClick={() => { setInstallOpen(false); setView("extensions"); }}><Package size={16} />扩展包</button>
          </div>
          <div className="capability-management-scope">
            <label><span>查看范围</span><span className="capability-scope-select">
              {scope === "project" ? <FolderOpen size={16} aria-hidden="true" /> : <Monitor size={16} aria-hidden="true" />}
              <select aria-label="查看范围" aria-describedby="capability-scope-help" value={workspace} disabled={busy || installOpen}
                onChange={event => onScopeChange(event.target.value)}>
                <option value="">本机全局</option>
                {projects.map(project => <option key={project.path} value={project.path}>项目：{project.name}</option>)}
              </select><ChevronDown size={14} aria-hidden="true" /></span></label>
            <details className="capability-scope-info">
              <summary aria-label="查看范围说明" title="查看范围说明"><Info size={15} aria-hidden="true" /></summary>
              <div className="capability-scope-description">
                <span className="capability-conversation-scope"><MessageSquare size={14} aria-hidden="true" /><span>当前对话：{conversationLabel}</span></span>
                <p id="capability-scope-help" className="capability-scope-help">{scope === "project" ? "包含本项目与继承的全局能力。" : "全局能力可供项目和独立对话使用。"}添加能力时保存到此范围。</p>
              </div>
            </details>
          </div>
          <button className="button button-primary capability-add-button" disabled={busy || installOpen} onClick={() => openInstall(view === "skills" ? "skill" : "package")}><Plus size={16} />{view === "skills" ? "添加技能" : "添加扩展"}</button>
        </div>

        {outsideInvocationContext && view === "skills" && !installOpen && <div className="capability-context-note"><Info size={16} /><p>正在查看{managementLabel}，技能能否添加以「{conversationLabel}」对话为准。</p>
          <button disabled={busy || installOpen} onClick={() => onScopeChange(invocationScope === "project" ? invocationWorkspace : "")}>查看当前对话范围<ArrowUpRight size={14} /></button></div>}

        {notice && (
          <div className={`extension-notice notice-${notice.kind}`} role="status">
            {notice.kind === "success" ? <CircleCheck size={16} /> : <CircleAlert size={16} />}
            <span>{notice.text}</span>
            <button onClick={() => setNotice(null)} aria-label="关闭提示"><X size={14} /></button>
          </div>
        )}

        <section id="capability-skills-panel" className="capability-tab-panel" role="tabpanel" aria-labelledby="capability-skills-tab" hidden={view !== "skills"}>
          <SkillCatalog key={`${workspace}:${scope}`} workspace={workspace} scope={scope}
          initialName={skillFocus?.name} initialExtension={skillFocus?.extension} focusVersion={skillFocusVersion} refreshVersion={skillRevision}
          invocationWorkspace={invocationWorkspace} invocationScope={invocationScope}
          importForm={view === "skills" ? importForm : null} onUse={onUseSkill} onRefresh={() => { void onChanged?.(); }}
          projectLabel={suggestedProject?.name} onChooseProject={suggestedProject ? () => onScopeChange(suggestedProject.path) : undefined}
          onManage={key => { setSelectedKey(key); setExtensionDetailOpen(true); setInstallOpen(false); setSearch(""); setScopeFilter("all"); setView("extensions"); }}
          onViewConversation={name => onScopeChange(invocationScope === "project" ? invocationWorkspace : "", { name, extension: "" })}
          onImport={() => openInstall("skill")} />
        </section>

        <section id="capability-extensions-panel" className="capability-tab-panel" role="tabpanel" aria-labelledby="capability-extensions-tab" hidden={view !== "extensions"}>
        {totals.all > 0 && <div className="extensions-summary" aria-label="扩展状态汇总">
          <span><strong>{totals.all}</strong> 全部</span>
          <span><strong>{totals.active}</strong> 可用</span>
          <span className={totals.attention ? "has-attention" : ""}><strong>{totals.attention}</strong> 待处理</span>
        </div>}
        <div className={`extensions-body ${extensionDetailOpen ? "is-detail-open" : ""} ${installOpen ? "is-installing" : ""} ${!catalog?.extensions.length ? "is-empty" : ""} ${noInstalledExtensions && !installOpen ? "is-onboarding" : ""}`}>
          {Boolean(catalog?.extensions.length) && <section
            className="extensions-sidebar"
            aria-label="扩展目录"
          >
            <div className="extension-toolbar">
              <div className="extension-search">
                <Search size={15} aria-hidden="true" />
                <input
                  ref={searchRef}
                  aria-label="搜索扩展"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="搜索名称、ID 或发布者"
                />
                {search && <button type="button" className="capability-search-clear" aria-label="清除扩展搜索" onClick={() => { setSearch(""); searchRef.current?.focus(); }}><X size={14} /></button>}
              </div>
              <button
                className="icon-button"
                onClick={() => void refreshCatalog()}
                disabled={busy}
                aria-label="刷新扩展目录"
                title="刷新扩展目录"
              >
                <RefreshCw size={16} className={busyAction === "refresh-catalog" ? "spin" : ""} />
              </button>
            </div>

            <div className="extension-filters" role="group" aria-label="扩展来源">
              <span className="capability-filter-label">来源</span>
              {scope === "project" && (["all", "project", "global"] as const).map((scope) => (
                <button
                  key={scope}
                  className={scopeFilter === scope ? "is-active" : ""}
                  onClick={() => setScopeFilter(scope)}
                  aria-pressed={scopeFilter === scope}
                >
                  {scope === "all" ? "全部" : scope === "project" ? "本项目" : "全局"}
                </button>
              ))}
              {scope === "standalone" && <span className="capability-source-label">全局来源</span>}
            </div>

            <div ref={extensionList} className="extension-list" role="listbox" aria-label="已安装扩展" onKeyDown={event => {
              if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) || !filteredExtensions.length) return;
              event.preventDefault();
              const index = filteredExtensions.findIndex(item => item.key === selected?.key);
              const next = event.key === "Home" ? 0 : event.key === "End" ? filteredExtensions.length - 1
                : (index + (event.key === "ArrowDown" ? 1 : -1) + filteredExtensions.length) % filteredExtensions.length;
              setSelectedKey(filteredExtensions[next].key);
              extensionList.current?.querySelectorAll<HTMLButtonElement>('[role="option"]')[next]?.focus();
            }}>
              {loading && !catalog ? (
                <div className="extension-skeletons" aria-label="正在读取扩展">
                  {[0, 1, 2].map((item) => <span key={item} />)}
                </div>
              ) : filteredExtensions.length > 0 ? (
                filteredExtensions.map((extension) => (
                  <button
                    key={extension.key}
                    className={`extension-row ${selected?.key === extension.key ? "is-selected" : ""}`}
                    onClick={() => {
                      setSelectedKey(extension.key); setExtensionDetailOpen(true);
                      requestAnimationFrame(() => {
                        if (extensionList.current && !extensionList.current.getClientRects().length) extensionDetail.current?.focus({ preventScroll: true });
                      });
                    }}
                    role="option"
                    aria-selected={selected?.key === extension.key}
                    tabIndex={selected?.key === extension.key ? 0 : -1}
                  >
                    <span className={`extension-row-status tone-${healthTone(extension.health)}`}>
                      <StatusIcon health={extension.health} />
                    </span>
                    <span className="extension-row-copy">
                      <span><strong>{extension.name}</strong><code>v{extension.version}</code></span>
                      <small>{scopeLabel(extension.scope)} · {healthLabel(extension.health)}</small>
                    </span>
                    <span className="extension-row-count" title="包内声明的能力数量">
                      {Object.values(extension.contributionCounts).reduce((sum, value) => sum + value, 0)} 项
                    </span>
                  </button>
                ))
              ) : (
                <div className="extension-list-empty">
                  <Box size={22} />
                  <strong>{catalog?.extensions.length ? "没有匹配的扩展" : "此范围没有扩展"}</strong>
                  <span>{catalog?.extensions.length ? "调整搜索词或来源筛选。" : "导入后将在这里显示。"}</span>
                </div>
              )}
            </div>
          </section>}

          <section ref={extensionDetail} tabIndex={-1} className="extension-detail" aria-label="扩展详情">
            {(view === "extensions" ? importForm : null) || (selected ? (
              <>
                <button className="skill-list-back" onClick={() => {
                  setExtensionDetailOpen(false);
                  requestAnimationFrame(() => extensionList.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus());
                }}><ArrowLeft size={16} />返回扩展列表</button>
                <div className="extension-detail-head">
                  <div>
                    <div className="extension-title-line">
                      <h2>{selected.name}</h2>
                      <span className={`extension-status tone-${healthTone(selected.health)}`}>
                        <StatusIcon health={selected.health} />{healthLabel(selected.health)}
                      </span>
                    </div>
                    <p>{selected.description || "此扩展未提供说明。"}</p>
                    <div className="extension-identity">
                      <code>{selected.id}</code>
                      <span>{scopeLabel(selected.scope)}</span>
                      <span>版本 {selected.version}</span>
                      {selected.publisher && <span>{selected.publisher}</span>}
                    </div>
                  </div>
                  <button
                    className="button button-secondary"
                    onClick={() => void refreshHealth(selected)}
                    disabled={busy}
                  >
                    <RefreshCw size={15} className={busyAction === "health" ? "spin" : ""} />
                    检查状态
                  </button>
                </div>

                <div className={`extension-health tone-${healthTone(selected.health)}`}>
                  <StatusIcon health={selected.health} />
                  <div>
                    <strong>{healthLabel(selected.health)}</strong>
                    <span>{HEALTH_COPY[selected.health] || selected.healthDetail || "请查看下方检查结果。"}</span>
                  </div>
                  {(selected.health === "untrusted" || selected.health === "permission_required") ? (
                    <button className="button button-secondary" onClick={showAccess} disabled={busy}>
                      {selected.health === "untrusted" ? "查看信任设置" : "设置权限"}<ArrowUpRight size={14} />
                    </button>
                  ) : selected.health === "disabled" ? (
                    <button className="button button-secondary" onClick={() => void mutate(
                      "toggle", () => api.updateExtension(selected.scope, selected.id, {
                        workspace, contextScope: scope, enabled: true,
                      }), "扩展已启用。",
                    )} disabled={busy}>
                      {busyAction === "toggle" ? <LoaderCircle size={15} className="spin" /> : <Power size={15} />}启用扩展
                    </button>
                  ) : selected.health !== "ready" && (
                    <button className="button button-secondary" onClick={showDiagnostics} disabled={busy}>查看诊断<ArrowUpRight size={14} /></button>
                  )}
                </div>

                {(selected.validationErrors.length > 0 || selected.shadowedBy) && (
                  <div className="extension-problems" role="alert">
                    <CircleAlert size={16} />
                    <div>
                      {selected.shadowedBy && <p>当前扩展被 {selected.shadowedBy} 覆盖。</p>}
                      {selected.validationErrors.map((item) => <p key={item}>{item}</p>)}
                    </div>
                  </div>
                )}

                <section className="extension-section extension-capabilities">
                  <div className="extension-section-heading">
                    <div><Wrench size={17} /><span><strong>提供的能力</strong><small>先看扩展包含什么，再决定是否信任和使用</small></span></div>
                  </div>
                  <div className="extension-capability-intro">
                    <Sparkles size={15} />
                    <p>一个扩展包可以提供技能、工具与连接。技能在「技能」页中查看和添加；信任与权限在这里统一管理。</p>
                  </div>
                  {Object.values(selected.contributionCounts).some(count => count > 0) && (
                    <div className="extension-declared-capabilities" aria-label="包内声明的能力">
                      <span>包内声明</span>
                      {Object.entries(selected.contributionCounts).filter(([, count]) => count > 0).map(([kind, count]) => (
                        <span key={kind}>{CONTRIBUTION_LABELS[kind] || kind}<strong>{count}</strong></span>
                      ))}
                    </div>
                  )}
                  {capabilityCounts.all > 0 && <div className="extension-capability-filters" role="group" aria-label="筛选扩展能力">
                    <button
                      className={capabilityFilter === "all" ? "is-active" : ""}
                      onClick={() => setCapabilityFilter("all")}
                      aria-pressed={capabilityFilter === "all"}
                    >
                      全部 <strong>{capabilityCounts.all}</strong>
                    </button>
                    {CAPABILITY_GROUPS.map((group) => (
                      <button
                        key={group.key}
                        className={capabilityFilter === group.key ? "is-active" : ""}
                        onClick={() => setCapabilityFilter(group.key)}
                        aria-pressed={capabilityFilter === group.key}
                      >
                        {group.label} <strong>{capabilityCounts[group.key]}</strong>
                      </button>
                    ))}
                  </div>}

                  {visibleCapabilityGroups.length > 0 ? (
                    <div className="extension-capability-groups">
                      {visibleCapabilityGroups.map((group) => (
                        <section className="extension-capability-group" key={group.key} aria-label={group.title}>
                          <header className="extension-capability-group-head">
                            <CapabilityIcon group={group.key} />
                            <span><strong>{group.title}</strong><small>{group.description}</small></span>
                            <em>{capabilityItems[group.key].length}</em>
                          </header>
                          <div className="extension-capability-list">
                            {capabilityItems[group.key].map((item) => {
                              const available = item.availability === "ready";
                              const outsideTaskScope = item.group !== "skills" && item.target === "task" && outsideInvocationContext;
                              return (
                                <article className="extension-capability-row" key={item.id}>
                                  <span className={`extension-capability-icon capability-${item.group}`}>
                                    <CapabilityIcon group={item.group} />
                                  </span>
                                  <div className="extension-capability-copy">
                                    <div><strong>{item.name}</strong><span>{item.kind}</span></div>
                                    <p>{item.description}</p>
                                    {item.metadata.length > 0 && (
                                      <div className="extension-capability-meta">
                                        {item.metadata.map((metadata) => <span key={metadata}>{metadata}</span>)}
                                      </div>
                                    )}
                                  </div>
                                  <div className="extension-capability-action">
                                    <span className={`extension-capability-state tone-${healthTone(item.availability)}`}>
                                      {healthLabel(item.availability)}
                                    </span>
                                    <button
                                      className="button extension-use-button"
                                      onClick={() => {
                                        if (item.group === "skills") { setSkillFocus({ name: item.name, extension: selected.key }); setView("skills"); }
                                        else onUseContribution?.({ target: item.target, prompt: item.prompt });
                                      }}
                                      disabled={outsideTaskScope || (item.group !== "skills" && (!available || !onUseContribution))}
                                      title={outsideTaskScope ? "切换到对应范围的对话后使用" : item.group === "skills" ? "查看技能来源与调用方式" : available ? `前往${SURFACE_LABELS[item.target]}` : "先完成信任、授权并启用扩展"}
                                      aria-label={`${item.group === "skills" ? "查看技能" : "前往使用"} ${item.name}`}
                                    >
                                      {item.group === "skills" ? "查看技能" : "前往使用"} <ArrowUpRight size={14} />
                                    </button>
                                  </div>
                                </article>
                              );
                            })}
                          </div>
                        </section>
                      ))}
                    </div>
                  ) : (
                    <p className="extension-section-empty">
                      {capabilityCounts.all > 0 ? "当前分类没有能力。" : selected.health === "ready" ? "此扩展没有可展示的能力明细。" : "扩展尚未可用，可先查看包内声明和上方状态提示。"}
                    </p>
                  )}
                </section>

                <section ref={accessSection} tabIndex={-1} className="extension-section extension-access" aria-label="信任与权限">
                  <div className="extension-section-heading">
                    <div><Shield size={17} /><span><strong>信任与权限</strong><small>未授权的能力不会被调用</small></span></div>
                    <span className={`trust-state ${selected.trusted ? "is-trusted" : ""}`}>
                      {selected.trusted ? "已信任" : "未信任"}
                    </span>
                  </div>

                  {selected.permissions.length > 0 ? (
                    <div className="extension-permissions">
                      {selected.permissions.map((permission) => (
                        <label key={permission} className={permissionDraft.includes(permission) ? "is-granted" : ""}>
                          <input
                            type="checkbox"
                            checked={permissionDraft.includes(permission)}
                            onChange={() => togglePermission(permission)}
                            disabled={busy}
                          />
                          <span>
                            <code>{permission}</code>
                            <small>{PERMISSION_COPY[permission] || "允许扩展使用这项已声明能力。"}</small>
                          </span>
                        </label>
                      ))}
                    </div>
                  ) : (
                    <p className="extension-section-empty">此扩展未声明受保护权限。</p>
                  )}

                  <div className="extension-access-actions">
                    <p><ShieldAlert size={15} />密钥仅通过 secretRef 读取，扩展目录和此页面都不返回明文。</p>
                    <div>
                      {selected.trusted ? (
                        <>
                          {permissionChanged && (
                            <button
                              className="button button-primary"
                              onClick={() => void mutate(
                                "permissions",
                                () => api.updateExtension(selected.scope, selected.id, {
                                  workspace, contextScope: scope,
                                  trusted: true,
                                  grantedPermissions: permissionDraft,
                                }),
                                "权限已更新。",
                              )}
                              disabled={busy}
                            >
                              {busyAction === "permissions" && <LoaderCircle size={15} className="spin" />}
                              保存权限
                            </button>
                          )}
                          <button
                            className="button button-secondary"
                            onClick={() => void mutate(
                              "untrust",
                              () => api.updateExtension(selected.scope, selected.id, {
                                workspace, contextScope: scope,
                                trusted: false,
                                grantedPermissions: [],
                              }),
                              "扩展信任已撤销，已授予权限同时清除。",
                            )}
                            disabled={busy}
                          >
                            撤销信任
                          </button>
                        </>
                      ) : (
                        <button
                          className="button button-primary"
                          onClick={() => void mutate(
                            "trust",
                            () => api.updateExtension(selected.scope, selected.id, {
                              workspace, contextScope: scope,
                              trusted: true,
                              grantedPermissions: permissionDraft,
                            }),
                            "扩展已信任，并应用所选权限。",
                          )}
                          disabled={busy}
                        >
                          {busyAction === "trust" && <LoaderCircle size={15} className="spin" />}
                          信任并应用权限
                        </button>
                      )}
                    </div>
                  </div>
                </section>

                <details key={selected.key} ref={diagnostics} className="extension-diagnostics">
                  <summary><Info size={16} aria-hidden="true" /><span>安装信息与诊断</span><ChevronDown size={14} className="capability-discovery-chevron" aria-hidden="true" /></summary>
                <section className="extension-section">
                  <div className="extension-section-heading">
                    <div><CircleCheck size={17} /><span><strong>健康证据</strong><small>检查安装状态、依赖、完整性和授权</small></span></div>
                  </div>
                  {selected.checks.length > 0 ? (
                    <div className="extension-checks">
                      {selected.checks.map((check: ExtensionHealthCheck) => (
                        <div key={`${check.name}-${check.status}`} className={`check-${check.status}`}>
                          <CheckIcon status={check.status} />
                          <span><strong>{check.name}</strong><small>{check.detail}</small></span>
                          <em>{checkLabel(check.status)}</em>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="extension-section-empty">暂无检查记录，请运行一次状态检查。</p>
                  )}
                </section>

                <section className="extension-section extension-metadata">
                  <div className="extension-section-heading">
                    <div><Package size={17} /><span><strong>安装信息</strong><small>用于审计来源和解析优先级</small></span></div>
                  </div>
                  <dl>
                    {selected.healthDetail && <div><dt>检查说明</dt><dd>{selected.healthDetail}</dd></div>}
                    <div><dt>来源</dt><dd title={selected.source}>{selected.source || "本机"}</dd></div>
                    <div><dt>清单</dt><dd title={selected.manifestPath}>{selected.manifestPath}</dd></div>
                    <div><dt>完整性</dt><dd><code title={selected.integrity}>{selected.integrity || "未记录"}</code></dd></div>
                    <div><dt>API 版本</dt><dd>{selected.apiVersion}</dd></div>
                    <div><dt>激活条件</dt><dd>{selected.activationEvents.join("、") || "默认"}</dd></div>
                    <div>
                      <dt>依赖</dt>
                      <dd>{Object.entries(selected.dependencies).map(([id, version]) => `${id} ${version}`).join("、") || "无"}</dd>
                    </div>
                  </dl>
                </section>

                </details>

                <div className="extension-lifecycle">
                  <div>
                    <strong>扩展管理</strong>
                    <span>停用会保留配置，卸载后可从恢复区找回。</span>
                  </div>
                  <div className="extension-lifecycle-actions">
                    <button
                      className="button button-secondary"
                      onClick={() => void mutate(
                        "toggle",
                        () => api.updateExtension(selected.scope, selected.id, {
                          workspace, contextScope: scope,
                          enabled: !selected.enabled,
                        }),
                        selected.enabled ? "扩展已停用。" : "扩展已启用。",
                      )}
                      disabled={busy}
                    >
                      {busyAction === "toggle" && <LoaderCircle size={15} className="spin" />}
                      <Power size={15} />{selected.enabled ? "停用扩展" : "启用扩展"}
                    </button>
                    <button
                      className="button button-secondary"
                      onClick={() => void mutate(
                        "rollback",
                        () => api.rollbackExtension(selected.scope, selected.id, workspace, scope),
                        "扩展已回滚到上一版本。",
                      )}
                      disabled={busy}
                    >
                      {busyAction === "rollback" && <LoaderCircle size={15} className="spin" />}
                      <RotateCcw size={15} />回滚版本
                    </button>
                    {confirmUninstall ? (
                      <span className="extension-uninstall-confirm">
                        <span>确认移入可恢复区？</span>
                        <button
                          className="button button-danger"
                          onClick={() => void mutate(
                            "uninstall",
                            () => api.uninstallExtension(selected.scope, selected.id, workspace, scope),
                            "扩展已移入可恢复区。",
                          ).then(() => setConfirmUninstall(false))}
                          disabled={busy}
                        >
                          {busyAction === "uninstall" && <LoaderCircle size={15} className="spin" />}
                          确认卸载
                        </button>
                        <button className="button button-secondary" onClick={() => setConfirmUninstall(false)} disabled={busy}>取消</button>
                      </span>
                    ) : (
                      <button className="button button-quiet-danger" onClick={() => setConfirmUninstall(true)} disabled={busy}>
                        <Trash2 size={15} />卸载扩展
                      </button>
                    )}
                  </div>
                </div>
              </>
            ) : (<>
              <div className="extension-detail-empty capability-empty-detail">
                <span className="capability-empty-icon"><Package size={28} strokeWidth={1.5} aria-hidden="true" /></span>
                <h2>{loading ? "正在读取扩展" : !catalog ? "暂时无法读取扩展" : catalog.extensions.length ? "没有匹配的扩展" : "尚未安装扩展包"}</h2>
                <p>{loading ? "正在扫描扩展目录…" : !catalog ? "未能读取扩展目录，请重试。" : catalog.extensions.length ? "调整搜索词或来源筛选后查看。" : "扩展包可以提供技能、工具和 MCP 连接，在这里统一管理信任、权限和更新。"}</p>
                {noInstalledExtensions && <>
                  <p className="capability-empty-skill-note">本地技能可在<button onClick={() => { setView("skills"); document.getElementById("capability-skills-tab")?.focus(); }}>「技能」</button>中查看和使用。</p>
                  <p className="capability-empty-target">{scope === "project" ? <FolderOpen size={15} aria-hidden="true" /> : <Monitor size={15} aria-hidden="true" />}<span>保存到：<strong>{managementLabel}</strong></span></p>
                </>}
                {!loading && catalog && (catalog.extensions.length ? <button className="button button-secondary" onClick={() => { setSearch(""); setScopeFilter("all"); }}>清除筛选</button>
                  : <div className="capability-empty-actions"><button className="button button-primary" onClick={() => openInstall("package")} disabled={busy}><Plus size={15} />添加扩展</button>
                    <button className="button button-secondary" onClick={() => void refreshCatalog()} disabled={busy} title="重新扫描本地扩展目录"><RefreshCw size={15} className={busyAction === "refresh-catalog" ? "spin" : ""} />刷新目录</button></div>)}
                {!loading && !catalog && <button className="button button-secondary" onClick={() => void refreshCatalog()}>重试</button>}
              </div>
              {noInstalledExtensions && catalog?.installRoots && <details className="capability-discovery-roots"><summary><FolderOpen size={15} aria-hidden="true" /><span>保存目录</span><ChevronDown size={14} className="capability-discovery-chevron" aria-hidden="true" /></summary><code>{catalog.installRoots[installScope]}</code></details>}
            </>))}
          </section>
        </div>
        </section>
    </WorkspacePage>
  );
}
