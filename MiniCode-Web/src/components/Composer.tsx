import { ArrowUp, KeyRound, RotateCcw, Square } from "lucide-react";
import { useEffect, useRef } from "react";

import type { ExecutionProfile, RunStatus, SettingsView } from "../types";
import { ExecutionProfileSelector } from "./ExecutionProfileSelector";

interface ComposerProps {
  value: string;
  runStatus: RunStatus;
  sessionLoading: boolean;
  recoveryMode: "resume" | "retry" | null;
  settings: SettingsView | null;
  executionProfile: ExecutionProfile;
  placeholder: string;
  hint: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  onResume: () => void;
  onOpenSettings: () => void;
  onExecutionProfileChange: (value: ExecutionProfile) => void;
}

export function Composer({
  value,
  runStatus,
  sessionLoading,
  recoveryMode,
  settings,
  executionProfile,
  placeholder,
  hint,
  onChange,
  onSubmit,
  onStop,
  onResume,
  onOpenSettings,
  onExecutionProfileChange,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const active = runStatus === "running" || runStatus === "queued" || runStatus === "stopping";
  const inputDisabled = active || sessionLoading;

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [value]);

  return (
    <div className="composer-wrap">
      {!settings?.apiKeyConfigured && (
        <button className="configuration-note" onClick={onOpenSettings}>
          <KeyRound size={15} />
          <span>未配置 API Key，当前任务会使用本地演示模型。</span>
          <strong>去配置</strong>
        </button>
      )}
      {recoveryMode && !active && (
        <button className="recovery-note" onClick={onResume} disabled={sessionLoading}>
          <RotateCcw size={15} />
          <span>
            <strong>{recoveryMode === "retry" ? "重试未完成任务" : "继续上次任务"}</strong>
            <small>{recoveryMode === "retry" ? "会先检查失败原因，再从未完成处继续" : "会读取当前会话和工作区状态，从中断处接着处理"}</small>
          </span>
        </button>
      )}
      <div className={`composer ${active ? "is-running" : ""}`}>
        <textarea
          ref={textareaRef}
          aria-label="任务内容"
          placeholder={sessionLoading ? "正在恢复会话…" : placeholder}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              if (!active && value.trim()) onSubmit();
            }
          }}
          rows={1}
          disabled={inputDisabled}
        />
        <div className="composer-footer">
          <span className="composer-model">
            {settings ? `${settings.provider} / ${settings.model}` : "正在读取模型"}
          </span>
          <ExecutionProfileSelector
            value={executionProfile}
            disabled={inputDisabled}
            loading={!settings}
            onChange={onExecutionProfileChange}
          />
          {active ? (
            <button className="send-button stop-button" onClick={onStop} aria-label="停止任务">
              <Square size={14} fill="currentColor" />
            </button>
          ) : (
            <button
              className="send-button"
              onClick={onSubmit}
              aria-label="发送任务"
              disabled={inputDisabled || !value.trim()}
            >
              <ArrowUp size={18} />
            </button>
          )}
        </div>
      </div>
      <p className="composer-hint">{hint}</p>
    </div>
  );
}
