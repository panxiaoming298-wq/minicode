import type { ExecutionProfile } from "../types";

type ProfileStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const PREFIX = "minicode.executionProfile.v1:";

export class ExecutionProfileStore {
  private profiles = new Map<string, ExecutionProfile | undefined>();

  constructor(private storage: ProfileStorage | null) {}

  read(context: string): ExecutionProfile | undefined {
    if (this.profiles.has(context)) return this.profiles.get(context);
    let profile: ExecutionProfile | undefined;
    try {
      const saved = this.storage?.getItem(PREFIX + context);
      if (saved === "standard" || saved === "deep" || saved === "ultra") profile = saved;
    } catch { /* Keep conversation preferences usable without browser storage. */ }
    this.profiles.set(context, profile);
    return profile;
  }

  write(context: string, profile: ExecutionProfile | undefined) {
    this.profiles.set(context, profile);
    try {
      if (profile) this.storage?.setItem(PREFIX + context, profile);
      else this.storage?.removeItem(PREFIX + context);
    } catch { /* The selection remains available in this page. */ }
  }

  bindSession(source: string, session: string, profile: ExecutionProfile, consumeDraft = false) {
    // A prepared worktree session may already have its own next-send selection.
    if (!this.read(session)) this.write(session, profile);
    if (consumeDraft && source !== session) this.write(source, undefined);
  }
}
