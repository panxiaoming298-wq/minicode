import type { PermissionMode } from "../types";

type ModeStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const PREFIX = "minicode.permissionMode.v1:";

export class PermissionModeStore {
  private modes = new Map<string, PermissionMode | undefined>();

  constructor(private storage: ModeStorage | null) {}

  read(context: string): PermissionMode | undefined {
    if (this.modes.has(context)) return this.modes.get(context);
    let mode: PermissionMode | undefined;
    try {
      const saved = this.storage?.getItem(PREFIX + context);
      if (saved === "default" || saved === "bypass") mode = saved;
    } catch { /* Keep the selection usable without browser storage. */ }
    this.modes.set(context, mode);
    return mode;
  }

  write(context: string, mode: PermissionMode | undefined) {
    this.modes.set(context, mode);
    try {
      if (mode) this.storage?.setItem(PREFIX + context, mode);
      else this.storage?.removeItem(PREFIX + context);
    } catch { /* Retain the selection for this page lifetime. */ }
  }

  bindSession(source: string, session: string, mode: PermissionMode, consumeDraft = false) {
    if (!this.read(session)) this.write(session, mode);
    // A fresh conversation uses the product default rather than inheriting the
    // submitted draft's selection.
    if (consumeDraft && source !== session) this.write(source, undefined);
  }
}
