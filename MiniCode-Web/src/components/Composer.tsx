import { ArrowUp, KeyRound, Square } from "lucide-react";
import { useEffect, useRef } from "react";

import type { RunStatus, SettingsView } from "../types";

interface ComposerProps {
  value: string;
  runStatus: RunStatus;
  settings: SettingsView | null;
  placeholder: string;
  hint: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  onOpenSettings: () => void;
}

export function Composer({
  value,
  runStatus,
  settings,
  placeholder,
  hint,
  onChange,
  onSubmit,
  onStop,
  onOpenSettings,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const active = runStatus === "running" || runStatus === "queued" || runStatus === "stopping";

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
      <div className={`composer ${active ? "is-running" : ""}`}>
        <textarea
          ref={textareaRef}
          aria-label="任务内容"
          placeholder={placeholder}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              if (!active && value.trim()) onSubmit();
            }
          }}
          rows={1}
          disabled={active}
        />
        <div className="composer-footer">
          <span>{settings ? `${settings.provider} / ${settings.model}` : "正在读取模型"}</span>
          {active ? (
            <button className="send-button stop-button" onClick={onStop} aria-label="停止任务">
              <Square size={14} fill="currentColor" />
            </button>
          ) : (
            <button
              className="send-button"
              onClick={onSubmit}
              aria-label="发送任务"
              disabled={!value.trim()}
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
