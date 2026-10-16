import type { ExecutionLocation, ExecutionProfile, RunInfo, RuntimeTraceView, SettingsView } from "../types";

export function runExecutionLocation(run: RunInfo | null, trace?: RuntimeTraceView | null): ExecutionLocation | undefined {
  if (trace?.profile.writeIsolation === "direct") return "project";
  if (trace?.profile.writeIsolation === "temporary-workspace") return "isolated";
  if (run?.executionLocation) return run.executionLocation;
  // Older runs predate the explicit selector. Never relabel them using the draft.
  if (run) return run.executionProfile === "standard" ? "project" : "isolated";
  return undefined;
}

export function executionLocationError(settings: SettingsView | null, location: ExecutionLocation, mode: ExecutionProfile) {
  if (!settings?.executionLocationSupported) return "当前服务尚未支持选择执行位置；请重启更新后的 MiniCode 服务。";
  if (location === "isolated" && !settings.webRuntimePolicy?.modes.includes(mode)) {
    return "当前服务的此模式不支持检查后写回的隔离副本；请使用当前目录，或切换到支持隔离执行的模式。";
  }
  if (location === "project" && mode === "ultra" && settings.webAgentSettings?.primaryRole === "coordinator") {
    return "协调者模式需要隔离副本；请选择隔离副本，或在多 Agent 设置中选择主 Agent 执行。";
  }
  return "";
}
