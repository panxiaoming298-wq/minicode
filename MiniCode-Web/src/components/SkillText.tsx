import { Package, X } from "lucide-react";
import { skillPrefix } from "../lib/skillText";
import "./SkillText.css";

export function SkillMention({ name, onRemove, disabled }: { name: string; onRemove?: () => void; disabled?: boolean }) {
  return <span className="skill-mention" title={name}>
    <Package size={15} strokeWidth={1.6} aria-hidden="true" />
    <span className="skill-mention-name">{name}</span>
    {onRemove && <button type="button" className="skill-mention-remove" aria-label={`移除技能 ${name}`} disabled={disabled}
      onMouseDown={event => event.preventDefault()} onClick={onRemove}><X size={12} aria-hidden="true" /></button>}
  </span>;
}

/** Skill markup is presentation only; the saved command remains literal text. */
export function SkillText({ text }: { text: string }) {
  const skill = skillPrefix(text);
  return skill ? <><SkillMention name={skill.name} />{text.slice(skill.text.length)}</> : <>{text}</>;
}
