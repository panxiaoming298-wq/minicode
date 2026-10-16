import { AlertCircle, Check, ChevronDown, LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { ExecutionProfile } from "../types";

interface ExecutionProfileSelectorProps {
  value: ExecutionProfile;
  disabled?: boolean;
  loading?: boolean;
  error?: string;
  onChange: (value: ExecutionProfile) => void;
}

const PROFILE_OPTIONS: Array<{
  value: ExecutionProfile;
  label: string;
  detail: string;
}> = [
  { value: "standard", label: "Standard", detail: "日常执行 · 单 Agent 优先" },
  { value: "deep", label: "Deep", detail: "深入探索、验证与修复 · 单主 Agent" },
  { value: "ultra", label: "Ultra", detail: "深度执行 · 按需并行协作" },
];

export function ExecutionProfileSelector({
  value,
  disabled = false,
  loading = false,
  error,
  onChange,
}: ExecutionProfileSelectorProps) {
  const [open, setOpen] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const confirmationTimerRef = useRef<number | null>(null);
  const unavailable = disabled || loading;
  const selected = PROFILE_OPTIONS.find((option) => option.value === value) ?? PROFILE_OPTIONS[0];
  const state = error ? "error" : loading ? "loading" : confirmed ? "success" : "default";

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  useEffect(() => () => {
    if (confirmationTimerRef.current !== null) {
      window.clearTimeout(confirmationTimerRef.current);
    }
  }, []);

  function focusOption(index: number) {
    const options = rootRef.current?.querySelectorAll<HTMLButtonElement>("[role='option']");
    options?.[Math.max(0, Math.min(index, options.length - 1))]?.focus();
  }

  function openMenu() {
    if (unavailable) return;
    setOpen(true);
    window.setTimeout(() => {
      focusOption(PROFILE_OPTIONS.findIndex((option) => option.value === value));
    }, 0);
  }

  function selectProfile(profile: ExecutionProfile) {
    setOpen(false);
    triggerRef.current?.focus();
    if (profile === value) return;
    onChange(profile);
    setConfirmed(true);
    if (confirmationTimerRef.current !== null) {
      window.clearTimeout(confirmationTimerRef.current);
    }
    confirmationTimerRef.current = window.setTimeout(() => setConfirmed(false), 900);
  }

  return (
    <div
      className={`execution-profile-selector ${open ? "is-open" : ""}`}
      data-state={state}
      ref={rootRef}
    >
      <button
        ref={triggerRef}
        type="button"
        className="execution-profile-trigger"
        aria-label={`执行模式：${selected.label}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls="execution-profile-menu"
        aria-invalid={Boolean(error)}
        aria-describedby={error ? "execution-profile-error" : undefined}
        disabled={unavailable}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            openMenu();
          }
          if (event.key === "Escape") setOpen(false);
        }}
      >
        <span className="execution-profile-status" aria-hidden="true">
          {loading ? (
            <LoaderCircle className="execution-profile-spinner" size={14} />
          ) : error ? (
            <AlertCircle size={14} />
          ) : confirmed ? (
            <Check size={14} />
          ) : null}
        </span>
        <span className="execution-profile-label">{loading ? "读取模式" : selected.label}</span>
        <ChevronDown className="execution-profile-chevron" size={14} aria-hidden="true" />
      </button>

      {open && (
        <div
          id="execution-profile-menu"
          className="execution-profile-menu"
          role="listbox"
          aria-label="选择执行模式"
          onKeyDown={(event) => {
            const options = Array.from(
              rootRef.current?.querySelectorAll<HTMLButtonElement>("[role='option']") ?? [],
            );
            const currentIndex = options.indexOf(document.activeElement as HTMLButtonElement);
            if (event.key === "ArrowDown") {
              event.preventDefault();
              options[(currentIndex + 1 + options.length) % options.length]?.focus();
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              options[(currentIndex - 1 + options.length) % options.length]?.focus();
            } else if (event.key === "Home") {
              event.preventDefault();
              options[0]?.focus();
            } else if (event.key === "End") {
              event.preventDefault();
              options.at(-1)?.focus();
            } else if (event.key === "Escape") {
              event.preventDefault();
              setOpen(false);
              triggerRef.current?.focus();
            } else if (event.key === "Tab") {
              setOpen(false);
            }
          }}
        >
          <div className="execution-profile-menu-heading">执行模式</div>
          {PROFILE_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === value}
              className="execution-profile-option"
              onClick={() => selectProfile(option.value)}
            >
              <span className="execution-profile-option-copy">
                <strong>{option.label}</strong>
                <small>{option.detail}</small>
              </span>
              <Check
                className="execution-profile-option-check"
                size={16}
                aria-hidden="true"
              />
            </button>
          ))}
          <p className="execution-profile-note">
            三档按任务风险选择复核范围；Deep 加强主 Agent 的执行深度，Ultra 支持按需并行协作。
          </p>
        </div>
      )}

      {error && (
        <span id="execution-profile-error" className="execution-profile-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
