import type { TimelineItem } from "../types";
import type { ToolTimelineItem } from "./runSummary";

interface Edit {
  path: string;
  old: string;
  replacement: string;
  fuzzy: boolean;
}

function editInput(item: ToolTimelineItem): Edit | null {
  if (item.toolName !== "edit_file" || !item.input || typeof item.input !== "object" || Array.isArray(item.input)) return null;
  const input = item.input as Record<string, unknown>;
  const old = input.old ?? input.search;
  const replacement = input.new ?? input.replace;
  if (typeof input.path !== "string" || !input.path || typeof old !== "string" || !old.trim()
    || typeof replacement !== "string" || old === replacement
    || input.old !== undefined && input.search !== undefined && input.old !== input.search
    || input.new !== undefined && input.replace !== undefined && input.new !== input.replace
    || input.replace_all !== undefined && input.replace_all !== false
    || input.replaceAll !== undefined && input.replaceAll !== false
    || input.fuzzy !== undefined && typeof input.fuzzy !== "boolean") return null;
  return { path: input.path, old: old.replace(/\r\n/g, "\n"), replacement: replacement.replace(/\r\n/g, "\n"), fuzzy: input.fuzzy === true };
}

/** A retry may shorten/extend context, but must perform the WHOLE same edit.
 * Preserve every character, including indentation inside replacement text.
 * Never infer recovery from the file name, similar text, or an Agent's answer.
 */
function implementsEdit(outer: Edit, inner: Edit): boolean {
  const at = outer.old.indexOf(inner.old);
  return at >= 0 && outer.old.indexOf(inner.old, at + 1) < 0
    && outer.old.slice(0, at) + inner.replacement + outer.old.slice(at + inner.old.length) === outer.replacement;
}

export function recoveredEditToolIds(items: TimelineItem[]): Set<string> {
  const pending = new Map<string, Array<{ item: ToolTimelineItem; edit: Edit }>>();
  const recovered = new Set<string>();
  for (const item of items) {
    if (item.type === "user") { pending.clear(); continue; }
    if (item.type !== "tool" || !item.executionId || !item.nodeId || !item.attemptId) continue;
    // Schema rejections and native recovery protocols keep their own linkage.
    if (item.toolRecovery || item.toolInput && ["invalid", "schema_required"].includes(item.toolInput.status)) continue;
    const edit = editInput(item);
    if (!edit) continue;
    const key = JSON.stringify([item.executionId, item.nodeId, item.attemptId, edit.path]);
    if (item.status === "error" && /^Search string not found in file\.(?:\r?\n|$)/.test(item.output || "")) {
      pending.set(key, [...(pending.get(key) || []), { item, edit }]);
    } else if (item.status === "success" && !edit.fuzzy
      && (!item.toolInput || item.toolInput.status === "success")
      && item.output === `Applied reviewed changes to ${edit.path}`) {
      pending.set(key, (pending.get(key) || []).filter(failed => {
        if (failed.item.resultOrder !== undefined && item.startOrder !== undefined
          && failed.item.resultOrder < item.startOrder
          && (implementsEdit(failed.edit, edit) || implementsEdit(edit, failed.edit))) {
          recovered.add(failed.item.id);
          return false;
        }
        return true;
      }));
    }
  }
  return recovered;
}
