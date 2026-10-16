import type { CapabilitySkill } from "../types";

/** Complete only the command/name at the beginning, never task arguments. */
export function skillQuery(value: string, caret = value.length): string | null {
  if (caret !== value.length || /[\r\n]/.test(value)) return null;
  if (value === "/skill") return "";
  const explicit = /^\/skill[ \t]+([^\s]*)$/i.exec(value);
  if (explicit) return explicit[1];
  const shorthand = /^\/([^\s]*)$/.exec(value);
  return shorthand?.[1] ?? null;
}

export function matchingSkills(skills: readonly CapabilitySkill[], query: string): CapabilitySkill[] {
  const needle = query.toLocaleLowerCase();
  const seen = new Set<string>();
  return skills.filter(skill => {
    if (seen.has(skill.name)) return false;
    seen.add(skill.name);
    return skill.userInvocable === true && /^[^\s/\\]+$/.test(skill.name) && skill.name.toLocaleLowerCase().includes(needle);
  }).sort((a, b) => Number(!a.name.toLocaleLowerCase().startsWith(needle)) - Number(!b.name.toLocaleLowerCase().startsWith(needle))
    || a.name.localeCompare(b.name));
}

export function skillCommand(name: string): string {
  return `/skill ${name} `;
}

/** A Skill command must lead the draft; retain the user's task arguments. */
export function applySkillCommand(value: string, command: string): string {
  const current = /^\/skill[ \t]+[^\s]+[ \t]*/i.exec(value);
  if (current) return command + value.slice(current[0].length);
  if (skillQuery(value) !== null) return command;
  return command + value;
}
