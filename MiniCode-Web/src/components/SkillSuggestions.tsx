import { useEffect, useId, useMemo, useState } from "react";
import type { FormEvent, KeyboardEvent, RefObject } from "react";
import type { CapabilitySkill } from "../types";
import { matchingSkills, skillCommand, skillQuery } from "../lib/skillCompletion";
import { matchingComposerCommands, type ComposerCommand, type ComposerCommandId } from "../lib/composerCommands";
import { Archive, ArrowUpRight, CircleGauge, Package } from "lucide-react";
import "./SkillSuggestions.css";

interface Props {
  value: string;
  skills: readonly CapabilitySkill[];
  loading: boolean;
  error: string;
  scope: string;
  disabled: boolean;
  textarea: RefObject<HTMLTextAreaElement | null>;
  onChange: (value: string) => void;
  onOpen?: () => void;
  commands?: readonly ComposerCommand[];
  onCommand?: (id: ComposerCommandId) => void;
}

const NO_COMMANDS: readonly ComposerCommand[] = [];

export function useSkillSuggestions({ value, skills, loading, error, scope, disabled, textarea, onChange,
  onOpen, commands = NO_COMMANDS, onCommand }: Props) {
  const id = useId();
  const [focused, setFocused] = useState(false);
  const [caret, setCaret] = useState(value.length);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const query = skillQuery(value, caret);
  const skillsOnly = /^\/skill(?:\s|$)/i.test(value);
  const matches = useMemo(() => [
    ...matchingComposerCommands(commands, value).map(command => ({ kind: "command" as const, command })),
    ...(!error ? matchingSkills(skills, query ?? "").map(skill => ({ kind: "skill" as const, skill })) : []),
  ], [commands, value, skills, query, loading, error]);
  const open = focused && !disabled && query !== null && dismissed !== value;
  const index = Math.min(selected, Math.max(0, matches.length - 1));
  const activeId = open && matches.length ? `${id}-${index}` : undefined;

  // Refresh once per opening so new Skills appear without reloading the page.
  useEffect(() => { if (open) onOpen?.(); }, [open, onOpen]);
  useEffect(() => { setSelected(0); }, [query, scope]);
  useEffect(() => { setDismissed(current => current === value ? current : null); }, [value]);
  useEffect(() => { setDismissed(null); }, [scope]);
  useEffect(() => {
    if (activeId) document.getElementById(activeId)?.scrollIntoView({ block: "nearest" });
  }, [activeId]);

  function choose(skill: CapabilitySkill) {
    const next = skillCommand(skill.name);
    setDismissed(next);
    onChange(next);
    requestAnimationFrame(() => {
      textarea.current?.focus();
      textarea.current?.setSelectionRange(next.length, next.length);
      setCaret(next.length);
    });
  }

  function openCommands() {
    setDismissed(null);
    setSelected(0);
    setCaret(1);
    onChange("/");
    requestAnimationFrame(() => {
      textarea.current?.focus();
      textarea.current?.setSelectionRange(1, 1);
    });
  }

  function select(i: number, completeOnly = false) {
    const item = matches[i];
    if (!item) return;
    if (item.kind === "skill") { choose(item.skill); return; }
    if (item.command.disabledReason) return;
    const next = `/${item.command.id}`;
    if (completeOnly) {
      setDismissed(next);
      onChange(next);
      requestAnimationFrame(() => {
        textarea.current?.setSelectionRange(next.length, next.length);
        setCaret(next.length);
      });
    } else {
      setDismissed(value);
      onCommand?.(item.command.id);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): boolean {
    if (!open || event.nativeEvent.isComposing || event.keyCode === 229) return false;
    const completeWithTab = event.key === "Tab" && !event.shiftKey && matches.length > 0;
    if (["ArrowDown", "ArrowUp", "Escape"].includes(event.key) || completeWithTab || (event.key === "Enter" && !event.shiftKey)) {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === "Escape") setDismissed(value);
      else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (matches.length) setSelected((index + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length);
      } else select(index, completeWithTab);
      return true;
    }
    return false;
  }

  const menu = open ? <div className="skill-suggestions">
    <div className="skill-suggestions-heading"><strong>{skillsOnly ? "选择技能" : "命令与技能"}</strong><span>{matches.length} 个匹配</span></div>
    <div id={id} role="listbox" aria-label={skillsOnly ? "Skill 候选" : "命令与技能"} className="skill-suggestions-list">
      {matches.map((item, i) => {
        const command = item.kind === "command" ? item.command : undefined;
        const skill = item.kind === "skill" ? item.skill : undefined;
        const Icon = command?.id === "compact" ? Archive : command?.id === "context" ? CircleGauge : ArrowUpRight;
        return <button key={command ? `command:${command.id}` : `skill:${skill!.name}`} type="button" role="option" id={`${id}-${i}`}
          aria-selected={i === index} aria-disabled={Boolean(command?.disabledReason)} tabIndex={-1}
          onMouseDown={event => event.preventDefault()} onClick={() => select(i)} onPointerMove={() => setSelected(i)}>
          {command ? <span className="composer-command-name"><Icon size={17} aria-hidden="true" /><strong>{command.name}</strong><code>/{command.id}</code></span>
            : <span className="skill-suggestion-name"><Package size={17} strokeWidth={1.6} aria-hidden="true" /><span>/{skill!.name}</span></span>}
          <span className="skill-suggestion-description">{command?.disabledReason || command?.description || skill?.description || "使用此技能处理任务"}</span>
        </button>;
      })}
    </div>
    {loading ? <p role="status">正在读取当前工作区的技能…</p>
      : error ? <p role="status">技能列表加载失败，内置命令仍可使用。</p>
      : matches.length === 0 && <p role="status">{skillsOnly ? "没有匹配的技能" : "没有匹配的命令或技能"}</p>}
    <div className="skill-suggestions-help">{skillsOnly ? "↑↓ 选择 · Enter / Tab 填入 · Esc 关闭" : "↑↓ 选择 · Enter 执行命令 / 填入技能 · Tab 补全 · Esc 关闭"}</div>
  </div> : null;

  return { menu, onKeyDown, openCommands, inputProps: {
    "aria-autocomplete": "list" as const,
    "aria-haspopup": "listbox" as const,
    "aria-controls": activeId ? id : undefined,
    "aria-activedescendant": activeId,
    onFocus: () => { setFocused(true); setCaret(textarea.current?.selectionStart ?? value.length); },
    onBlur: () => setFocused(false),
    onInput: (event: FormEvent<HTMLTextAreaElement>) => setCaret(event.currentTarget.selectionStart),
    onSelect: () => setCaret(textarea.current?.selectionStart ?? value.length),
  } };
}
