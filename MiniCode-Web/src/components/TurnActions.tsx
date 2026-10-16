import { Check, Copy, Layers, Split } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { SessionTurn } from "../types";

interface TurnActionsProps {
  answer?: string;
  skills: string[];
  forkTurn?: SessionTurn;
  forkUnavailable?: boolean;
  onFork?: (turn: SessionTurn) => void;
}

export function TurnActions({ answer, skills, forkTurn, forkUnavailable, onFork }: TurnActionsProps) {
  const [copyStatus, setCopyStatus] = useState("");
  const [skillsOpen, setSkillsOpen] = useState(false);
  const skillRef = useRef<HTMLDivElement>(null);
  const skillButtonRef = useRef<HTMLButtonElement>(null);
  const skillsId = useId();

  useEffect(() => {
    if (!copyStatus) return;
    const timer = window.setTimeout(() => setCopyStatus(""), 2500);
    return () => window.clearTimeout(timer);
  }, [copyStatus]);

  useEffect(() => {
    if (!skillsOpen) return;
    function dismiss(event: PointerEvent) {
      if (!skillRef.current?.contains(event.target as Node)) setSkillsOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setSkillsOpen(false);
      skillButtonRef.current?.focus();
    }
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [skillsOpen]);

  if (!answer && !forkTurn && !skills.length) return null;
  const forkReason = forkUnavailable ? "工作树尚未就绪，暂时无法分叉" : forkTurn?.reason || "从这一轮完成后的对话继续";

  return <div className="answer-actions" role="group" aria-label="本轮操作">
    {answer && <button className="turn-action" aria-label="复制回复" title="复制回复" onClick={async () => {
      try { await navigator.clipboard.writeText(answer); setCopyStatus("已复制"); }
      catch { setCopyStatus("复制失败，请选中文字复制"); }
    }}>{copyStatus === "已复制" ? <Check size={16} /> : <Copy size={16} />}</button>}
    {forkTurn && onFork && <span className="turn-action-hint" title={forkReason}>
      <button className="turn-action" aria-label="从此处分叉" title={forkReason}
        disabled={!forkTurn.canFork || forkUnavailable} onClick={() => onFork(forkTurn)}><Split className="fork-action-icon" size={17} aria-hidden="true" /></button>
    </span>}
    {skills.length > 0 && <div className="turn-skills" ref={skillRef}>
      <button className={`turn-action ${skillsOpen ? "is-active" : ""}`} ref={skillButtonRef}
        aria-label={`本轮使用了 ${skills.length} 个 Skill`} title={`使用了 Skill：${skills.join("、")}`}
        aria-expanded={skillsOpen} aria-controls={skillsOpen ? skillsId : undefined} onClick={() => setSkillsOpen(value => !value)}>
        <Layers size={17} aria-hidden="true" />
      </button>
      {skillsOpen && <div className="turn-skills-popover" id={skillsId} role="region" aria-label="本轮使用的 Skill">
        <strong>本轮使用的 Skill</strong>
        <ul>{skills.map(name => <li key={name}><Layers size={14} aria-hidden="true" /><span>{name}</span></li>)}</ul>
      </div>}
    </div>}
    {copyStatus && <span role="status" className="turn-copy-status">{copyStatus}</span>}
  </div>;
}
