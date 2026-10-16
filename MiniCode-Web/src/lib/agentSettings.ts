import type { WebAgentSettings } from "../types";

export function agentSettingsDraft(saved?: WebAgentSettings): WebAgentSettings {
  return saved ? { ...saved } : { mode: "off", teamWrites: false, primaryRole: "worker", fork: false, promptCache: "off" };
}

export function changeAgentMode(draft: WebAgentSettings, mode: WebAgentSettings["mode"]): WebAgentSettings {
  return { ...draft, mode, teamWrites: mode === "teams" && draft.teamWrites,
    primaryRole: mode === "teams" ? draft.primaryRole : "worker", fork: mode !== "off" && draft.fork };
}
