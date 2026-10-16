const LEGACY_PREFIX = "Run my explicitly selected Skill ";
const NAME = /^[^\s/\\]+$/;

/** Presentation compatibility for the exact Skill wrapper saved by older workers. */
export function skillDisplayText(text: string): string {
  if (!text.startsWith(LEGACY_PREFIX) || !text.endsWith(".")) return text;
  try {
    const invocation = JSON.parse(text.slice(LEGACY_PREFIX.length, -1));
    if (invocation && typeof invocation.name === "string" && NAME.test(invocation.name)
      && typeof invocation.arguments === "string"
      && Object.keys(invocation).every(key => key === "name" || key === "arguments")) {
      return `/skill ${invocation.name}${invocation.arguments ? ` ${invocation.arguments}` : ""}`;
    }
  } catch { /* Preserve malformed or ordinary user text verbatim. */ }
  return text;
}

/** Old sidebar metadata can end in the middle of the JSON argument string. */
export function skillDisplayPreview(text: string): string {
  const restored = skillDisplayText(text);
  if (restored !== text || !text.startsWith(LEGACY_PREFIX) || text.endsWith(".")) return restored;
  const match = /^\{"name":\s*("(?:[^"\\]|\\.)*"),\s*"arguments":\s*"([\s\S]*)$/.exec(text.slice(LEGACY_PREFIX.length));
  if (!match) return text;
  try {
    const name: string = JSON.parse(match[1]);
    if (!NAME.test(name)) return text;
    // A truncated JSON escape can occupy at most six characters (\uXXXX).
    for (let trim = 0; trim <= Math.min(6, match[2].length); trim++) {
      try {
        const argumentsPrefix: string = JSON.parse(`"${match[2].slice(0, match[2].length - trim)}"`);
        return `/skill ${name}${argumentsPrefix ? ` ${argumentsPrefix}` : ""}`;
      } catch { /* Try the last complete escape boundary. */ }
    }
    return `/skill ${name}`;
  } catch { return text; }
}

export function skillPrefix(text: string): { text: string; name: string } | null {
  const match = /^\/skill[ \t]+([^\s/\\]+)(?=\s|$)/i.exec(text);
  return match ? { text: match[0], name: match[1] } : null;
}
