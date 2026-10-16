import { ArrowUp, ChevronDown, GitBranch, KeyRound, LoaderCircle, Monitor, Play, SlidersHorizontal, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import type { CapabilitySkill, ExecutionProfile, PermissionMode, RecentWorkspace, RunInput, RunStatus, SettingsView } from "../types";
import { ExecutionProfileSelector } from "./ExecutionProfileSelector";
import { useSkillSuggestions } from "./SkillSuggestions";
import { ComposerInput } from "./ComposerInput";
import { skillPrefix } from "../lib/skillText";
import { ComposerProjectPicker } from "./ComposerProjectPicker";
import { AttachmentList, ComposerAttachments } from "./ComposerAttachments";
import type { Attachment } from "../lib/attachments";
import { RunInputQueue } from "./RunInputQueue";
import { COMPOSER_COMMANDS, exactComposerCommand, type ComposerCommandId, type ComposerNavigation } from "../lib/composerCommands";

const NO_SKILLS: readonly CapabilitySkill[] = [];

interface ComposerProps {
  skills?: readonly CapabilitySkill[];
  skillsLoading?: boolean;
  skillsError?: string;
  onRefreshSkills?: () => void;
  submitting?: boolean;
  taskContract?: ReactNode;
  executionLocationSettings?: ReactNode;
  executionLocationLabel?: string;
  runBudget?: ReactNode;
  contextUsage?: ReactNode;
  reasoningSettings?: ReactNode;
  submissionBlocked?: boolean;
  contextBlocked?: boolean;
  environment?: "local" | "worktree";
  onEnvironmentChange?: (mode: "local" | "worktree") => void;
  value: string;
  draftKey: string;
  attachments: Attachment[];
  onAddAttachments: (files: Attachment[]) => void;
  onRemoveAttachment: (id: string) => void;
  hasSession: boolean;
  projects: RecentWorkspace[];
  onSwitchProject: (path: string | null) => Promise<void>;
  onPickProject: () => Promise<void>;
  notice?: string;
  draftStatus: "saved" | "memory" | "empty";
  runStatus: RunStatus;
  sessionLoading: boolean;
  inputs: RunInput[];
  inputBusy: boolean;
  acceptsInput: boolean;
  acceptsSteering?: boolean;
  acceptsQueuedInput?: boolean;
  onRunInput: (kind: RunInput["kind"]) => void;
  onRestoreInput: (item: RunInput) => void;
  onSteerInput?: (item: RunInput) => void;
  onDeleteInput: (item: RunInput) => void;
  onResumeQueue?: () => void;
  settings: SettingsView | null;
  executionProfile: ExecutionProfile;
  permissionMode: PermissionMode;
  onPermissionModeChange: (value: PermissionMode) => void;
  placeholder: string;
  hint: string;
  standalone: boolean;
  workspaceLabel: string;
  workspacePath: string;
  branch?: string;
  focusRequest?: number;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onCompact: () => void;
  onNavigateCommand: (target: ComposerNavigation) => void;
  onStop: () => void;
  onOpenSettings: () => void;
  onOpenChanges: () => void;
  onExecutionProfileChange: (value: ExecutionProfile) => void;
}

export function Composer({
  skills = NO_SKILLS, skillsLoading = false, skillsError = "", onRefreshSkills,
  submitting = false,
  taskContract, executionLocationSettings, executionLocationLabel, runBudget, contextUsage, reasoningSettings, submissionBlocked = false, contextBlocked = false,
  environment, onEnvironmentChange,
  value,
  draftKey, attachments, onAddAttachments, onRemoveAttachment,
  hasSession, projects, onSwitchProject, onPickProject, notice,
  draftStatus,
  runStatus,
  sessionLoading,
  inputs,
  inputBusy,
  acceptsInput,
  acceptsSteering = false,
  acceptsQueuedInput = acceptsInput,
  onRunInput,
  onRestoreInput,
  onSteerInput, onDeleteInput, onResumeQueue,
  settings,
  executionProfile,
  permissionMode, onPermissionModeChange,
  placeholder,
  hint,
  standalone,
  workspaceLabel,
  workspacePath,
  branch,
  focusRequest,
  onChange,
  onSubmit,
  onCompact,
  onNavigateCommand,
  onStop,
  onOpenSettings,
  onOpenChanges,
  onExecutionProfileChange,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const prefix = skillPrefix(value);
  // A complete explicit command stays a token while its catalog refreshes.
  const selectedSkill = prefix && value.length > prefix.text.length ? prefix : null;
  const permissionRef = useRef<HTMLSelectElement>(null);
  const taskSettingsRef = useRef<HTMLDetailsElement>(null);
  const [attachmentBusy, setAttachmentBusy] = useState(false);
  const images = attachments.filter(file => file.kind === "image");
  const imageBlocked = images.length > 0 && settings?.imageInput?.status !== "supported";
  const hasContent = Boolean(value.trim() || attachments.length);
  const active = runStatus === "running" || runStatus === "queued" || runStatus === "stopping";
  const inputDisabled = sessionLoading;
  const compactDisabled = !hasSession ? "开始对话后可以整理上下文"
    : active ? "请等待当前任务结束后整理上下文"
    : submitting || inputBusy ? "正在提交，请稍候"
    : attachmentBusy || attachments.length ? "请先发送或移除附件，再整理上下文"
    : contextBlocked ? "请先确认上下文设置" : submissionBlocked ? "请先处理任务设置中的待确认项" : "";
  const commands = COMPOSER_COMMANDS.map(command => ({ ...command,
    disabledReason: command.id === "compact" ? compactDisabled
      : command.requires === "session" && !hasSession ? "开始对话后可以使用此命令"
      : command.requires === "project" && standalone ? "绑定项目后可以使用此命令"
      : command.id === "permissions" && (active || submitting) ? "请等待当前任务结束后调整权限模式"
      : command.id === "permissions" && !settings?.permissionModeSupported ? "请更新并重启后端以选择权限模式" : "" }));
  const selectedCommand = commands.find(command => command.id === exactComposerCommand(value));
  const commandDisabledReason = selectedCommand?.disabledReason;
  const commandBlocked = inputDisabled || Boolean(commandDisabledReason);
  function executeCommand(id: ComposerCommandId) {
    const command = commands.find(command => command.id === id);
    if (!command || command.disabledReason || inputDisabled) return;
    const action = command.action;
    if (action.kind === "compact") { onCompact(); return; }
    if (action.kind === "help") { skillSuggestions.openCommands(); return; }
    onChange("");
    if (action.kind === "permissions") permissionRef.current?.focus();
    else if (action.kind === "task-settings") {
      if (taskSettingsRef.current) {
        taskSettingsRef.current.open = true;
        taskSettingsRef.current.querySelector("summary")?.focus();
      }
    } else onNavigateCommand(action);
  }
  function submitInput() {
    const command = exactComposerCommand(value);
    if (command) { executeCommand(command); return; }
    if (imageBlocked) return;
    if (resumeQueue && !submitting && !inputBusy && !inputDisabled && !submissionBlocked && !contextBlocked && !attachmentBusy) {
      resumeQueue(); return;
    }
    if (hasContent && !attachmentBusy) {
      if (!active && !submitting && !submissionBlocked && !contextBlocked) onSubmit();
      else if (active && acceptsInput && acceptsQueuedInput && !inputBusy && !contextBlocked) onRunInput("next_task");
    }
  }
  const skillSuggestions = useSkillSuggestions({ value, skills, loading: skillsLoading, error: skillsError,
    scope: draftKey, disabled: inputDisabled || Boolean(selectedSkill), textarea: textareaRef, onChange, onOpen: onRefreshSkills,
    commands, onCommand: executeCommand });
  const pendingQueue = inputs.some(item => item.kind !== "steer" && ["accepted", "held"].includes(item.status));
  const resumeQueue = !active && pendingQueue && !hasContent && onResumeQueue;

  useEffect(() => {
    if (!focusRequest) return;
    const textarea = textareaRef.current;
    if (!textarea || textarea.disabled) return;
    textarea.focus({ preventScroll: true });
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  }, [focusRequest]);

  useEffect(() => {
    if (submissionBlocked && taskSettingsRef.current) taskSettingsRef.current.open = true;
  }, [submissionBlocked]);

  useEffect(() => {
    function dismiss(event: PointerEvent) {
      const settings = taskSettingsRef.current;
      if (settings?.open && !settings.contains(event.target as Node)) settings.open = false;
    }
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, []);

  return (
    <div className="composer-wrap">
      {!settings?.apiKeyConfigured && (
        <button className="configuration-note" onClick={onOpenSettings}>
          <KeyRound size={15} />
          <span>当前模型尚未配置 API Key，请先完成模型配置。</span>
          <strong>去配置</strong>
        </button>
      )}
      <div className={`composer-context ${hasSession ? "is-bound" : ""}`} aria-label="当前任务环境">
        {!hasSession && <>
          <ComposerProjectPicker workspace={workspacePath} label={workspaceLabel} standalone={standalone} projects={projects}
            disabled={inputDisabled || submitting || active || attachmentBusy} onSelect={onSwitchProject} onPick={onPickProject} />
          <span className="composer-context-item context-location"><Monitor size={15} />{environment === "worktree" ? "Worktree" : "本地"}</span>
          {!standalone && branch && (
            <button className="composer-context-item context-branch" onClick={onOpenChanges} title={`查看 ${branch} 的变更`}>
              <GitBranch size={15} /><span>{branch}</span>
            </button>
          )}
          {standalone && <span className="composer-context-note">不读取项目文件</span>}
        </>}
        {executionLocationLabel && <span className="composer-context-item" title={workspacePath}><Monitor size={15} />{executionLocationLabel}</span>}
        <select className={`permission-mode-select ${permissionMode === "bypass" ? "is-full-access" : ""}`}
          ref={permissionRef}
          aria-label="权限模式" value={permissionMode}
          disabled={active || submitting || sessionLoading || !settings?.permissionModeSupported}
          title={active ? "本轮及排队任务沿用启动时的权限模式；结束后可切换"
            : !settings?.permissionModeSupported ? "请更新并重启后端以选择权限模式"
            : "按对话保存。完全访问允许修改文件、执行命令和访问项目外路径，无需逐项审批"}
          onChange={(event) => onPermissionModeChange(event.target.value as PermissionMode)}>
          <option value="default">默认审批</option>
          <option value="bypass">完全访问</option>
        </select>
        {(taskContract || runBudget || executionLocationSettings) && <details className={`composer-task-settings ${submissionBlocked ? "needs-attention" : ""}`} ref={taskSettingsRef}
          onKeyDown={(event) => {
            if (event.key === "Escape" && taskSettingsRef.current?.open) {
              event.stopPropagation();
              taskSettingsRef.current.open = false;
              taskSettingsRef.current.querySelector("summary")?.focus();
            }
          }}>
          <summary><SlidersHorizontal size={14} /><span>任务设置</span>{submissionBlocked && <i aria-label="需要处理" />}</summary>
          <div className="task-settings-popover">
            <div className="task-settings-heading"><strong>{active ? "正在执行的任务设置" : "下次发送的任务设置"}</strong><span>执行位置、预算与验收</span></div>
            {executionLocationSettings}{taskContract}{runBudget}
          </div>
        </details>}
      </div>
      {notice && <p className="composer-file-notice" role="status">{notice}</p>}
      <div className="composer-stack">
      <RunInputQueue inputs={inputs} runStatus={runStatus} busy={inputBusy || sessionLoading}
        acceptsSteering={acceptsSteering} resumeBlocked={submissionBlocked || contextBlocked || attachmentBusy}
        onSteer={onSteerInput} onDelete={onDeleteInput} onEdit={onRestoreInput} onResume={onResumeQueue} />
      <div className={`composer ${active ? "is-running" : ""}`}>
        {skillSuggestions.menu}
        <AttachmentList files={attachments} onRemove={onRemoveAttachment} disabled={inputDisabled} />
        {images.length > 0 && <div className={`composer-image-note ${imageBlocked ? "is-blocked" : ""}`} role={imageBlocked ? "alert" : "status"}>
          <span>{!settings?.imageInput ? "请重启 MiniCode 本机服务以启用图片输入。"
            : settings.imageInput.status === "unsupported" ? `当前模型「${settings.model}」不支持图片输入，请切换模型或移除图片。`
            : settings.imageInput.status === "unknown" ? "尚未确认当前模型及接口是否支持图片输入，请在模型设置中确认，或切换模型。"
            : `将向 ${settings.model} 发送 ${images.length} 张图片`}</span>
          {imageBlocked && <div><button onClick={onOpenSettings}>模型设置</button><button onClick={() => images.forEach(file => onRemoveAttachment(file.id))}>移除图片</button></div>}
        </div>}
        <ComposerInput
          textareaRef={textareaRef}
          skill={selectedSkill}
          {...skillSuggestions.inputProps}
          aria-label="任务内容"
          placeholder={sessionLoading ? "正在恢复会话…" : active ? "随时补充想法，发送后先排队…" : placeholder}
          value={value}
          onChange={onChange}
          onKeyDown={(event) => {
            if (skillSuggestions.onKeyDown(event)) return;
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
              event.preventDefault();
              submitInput();
            }
          }}
          rows={2}
          disabled={inputDisabled}
        />
        <div className="composer-footer">
          <ComposerAttachments key={draftKey} workspace={workspacePath} standalone={standalone}
            disabled={inputDisabled || submitting || inputBusy} onAdd={onAddAttachments} onBusyChange={setAttachmentBusy} />
          {onEnvironmentChange && <select className="environment-select" aria-label="新任务执行环境" value={environment || "local"} disabled={active || submitting || sessionLoading} onChange={(event) => onEnvironmentChange(event.target.value as "local" | "worktree")}><option value="local">Local</option><option value="worktree">Worktree</option></select>}
          <button className="composer-model" onClick={onOpenSettings} disabled={inputDisabled || active || submitting || !settings}
            aria-label="配置当前模型" title={settings ? `${settings.provider} / ${settings.model} · 点击配置模型` : "正在读取模型"}>
            <span>{settings?.model || "正在读取模型"}</span><ChevronDown size={14} />
          </button>
          <span className="next-profile-label">下次发送</span>
          <ExecutionProfileSelector
            value={executionProfile}
            disabled={inputDisabled || submitting}
            loading={!settings}
            onChange={onExecutionProfileChange}
          />
          {reasoningSettings}
          {contextUsage}
          {active ? (
            <>
            {(selectedCommand || acceptsQueuedInput && hasContent) && <button className="send-button queue-send-button" onClick={submitInput}
              title={commandDisabledReason || (selectedCommand ? "执行命令" : "加入消息队列")}
              disabled={selectedCommand ? commandBlocked : inputDisabled || inputBusy || attachmentBusy || imageBlocked || contextBlocked}
              aria-label={selectedCommand ? "执行命令" : "排队下一条任务"}>{inputBusy ? <LoaderCircle size={18} className="spin" /> : <ArrowUp size={18} />}</button>}
            <button className="send-button stop-button" onClick={onStop} aria-label="停止任务">
              <Square size={14} fill="currentColor" />
            </button>
            </>
          ) : (
            <button
              className="send-button"
              onClick={resumeQueue || submitInput}
              aria-label={selectedCommand ? "执行命令" : submitting ? "正在提交任务" : resumeQueue ? "继续队列" : "发送任务"}
              title={commandDisabledReason || undefined}
              disabled={selectedCommand ? commandBlocked : inputDisabled || submitting || inputBusy || submissionBlocked || contextBlocked || attachmentBusy || imageBlocked || (!hasContent && !resumeQueue)}
            >
              {submitting ? <LoaderCircle size={18} className="spin" /> : resumeQueue ? <Play size={15} fill="currentColor" /> : <ArrowUp size={18} />}
            </button>
          )}
        </div>
      </div>
      </div>
      <div className="composer-caption">
        {executionProfile === "ultra" && settings?.webAgentSettingsAvailable && !active && <button
          className="composer-agent-settings" onClick={onOpenSettings} disabled={submitting}
          aria-label="设置多 Agent 协作">
          {settings.webAgentSettings?.mode === "teams" ? "Agent Teams 已开启" : settings.webAgentSettings?.mode === "async" ? "异步子 Agent 已开启" : "基础协作 · 更改"}
        </button>}
        <p className="composer-hint" role={submitting ? "status" : undefined}>{submitting ? "正在提交任务；确认启动后开始执行。你可以继续编辑草稿。" : active ? acceptsQueuedInput
          ? onSteerInput ? "Enter 排队 · 点击消息上的「引导」发送给当前任务 · Shift + Enter 换行"
            : "Enter 排队 · 当前任务结束后发送 · Shift + Enter 换行"
          : "可以继续写草稿；本轮暂不接收新消息" : hint}</p>
        {draftStatus !== "empty" && <span className={`draft-status ${draftStatus === "memory" ? "is-volatile" : ""}`} role="status">
          {draftStatus === "saved" ? "草稿已保存到本机" : "草稿仅保留在当前页面"}
        </span>}
      </div>
    </div>
  );
}
