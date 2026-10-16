import type { ToolTimelineItem } from "./runSummary";

type RecordValue = Record<string, unknown>;
type Operation = { key: string; views: string[]; sourceRoot?: string; readySelector: string; hasCriteria: boolean };
type BrowserCorrection = "verified" | "source-check-removed";

function object(value: unknown): RecordValue | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as RecordValue : undefined;
}

function pathFrom(cwd: string, value: string): string {
  const path = value.replaceAll("\\", "/");
  const parts: string[] = [];
  for (const part of (/^(?:[a-z]:\/|\/)/i.test(path) ? path : `${cwd.replaceAll("\\", "/")}/${path}`).split("/")) {
    if (part === ".") continue;
    if (part === "..") parts.pop();
    else parts.push(part);
  }
  return parts.join("/");
}

/** Recognize the native browser checker, keeping its target and check scope.
 * Group source-root variants, but allow removing source observation only for
 * its exact preflight output-location error below. Other scope stays fixed.
 * This is a display association, never task acceptance or visual verification.
 */
function operation(item: ToolTimelineItem): Operation | undefined {
  const input = object(item.input);
  if (!input || typeof input.command !== "string"
    || input.cwd != null && (typeof input.cwd !== "string" || !input.cwd.trim())
    || !/(?:^|[\\/])python(?:3(?:\.\d+)?)?(?:\.exe)?$/i.test(input.command)
    || !Array.isArray(input.args) || !input.args.every((arg): arg is string => typeof arg === "string")) return;
  const args = input.args;
  let script = 0;
  while (["-I", "-B", "-u"].includes(args[script])) script++;
  if (!/(?:^|[\\/])minicode[\\/]project_browser\.py$/.test(args[script] || "")) return;
  const values = new Map<string, string[]>();
  const repeatable = new Set(["--view", "--allow-origin", "--allow-websocket"]);
  const options = new Set(["--root", "--url", "--output", "--scenario", "--criteria-file", "--ready-selector",
    "--source-root", "--source-revision", "--source-binding", "--browser", "--view", "--allow-origin",
    "--allow-websocket", "--timeout-ms", "--animations", "--color-scheme", "--screenshots", "--trace"]);
  const scope = args.slice(0, script + 1);
  for (let index = script + 1; index < args.length; index += 2) {
    const flag = args[index], value = args[index + 1];
    if (!options.has(flag) || !value?.trim() || value.startsWith("--") || values.has(flag) && !repeatable.has(flag)) return;
    values.set(flag, [...(values.get(flag) || []), value]);
    if (flag !== "--ready-selector" && flag !== "--source-root") scope.push(flag, value);
  }
  if (values.has("--root") === values.has("--url")
    || !["--output", "--scenario", "--ready-selector"].every(key => values.has(key))) return;
  const views = values.get("--view") || ["desktop", "mobile"];
  if (new Set(views).size !== views.length || views.some(view => !["desktop", "mobile"].includes(view))) return;
  const cwd = typeof input.cwd === "string" ? input.cwd : ".";
  if (input.check_report !== undefined && input.check_report !== null
    && (typeof input.check_report !== "string"
      || pathFrom(cwd, input.check_report) !== pathFrom(cwd, `${values.get("--output")![0]}/report.json`))) return;
  // Omitted/null cwd both mean this execution's workspace root. Ownership is
  // part of the key, so this never equates defaults from different executions.
  const stable = Object.entries({ ...input, args: scope, cwd: input.cwd ?? null }).filter(([key]) => !["timeout", "check_report"].includes(key))
    .sort(([a], [b]) => a.localeCompare(b));
  return { key: JSON.stringify([item.executionId, item.nodeId, item.attemptId, stable]), views,
    sourceRoot: values.get("--source-root")?.[0], readySelector: values.get("--ready-selector")![0], hasCriteria: values.has("--criteria-file") };
}

export function browserCommandKey(item: ToolTimelineItem): string | undefined {
  return operation(item)?.key;
}

function report(item: ToolTimelineItem): RecordValue | undefined {
  if (!item.commandResult?.settled) return;
  try { return object(JSON.parse(item.commandResult.stdout)); } catch { return; }
}

function criteria(value: unknown): RecordValue[] | undefined {
  if (!Array.isArray(value) || !value.length || value.length > 64) return;
  const result: RecordValue[] = [];
  for (const entry of value) {
    const item = object(entry);
    if (!item || !Number.isSafeInteger(item.number) || Number(item.number) < 1
      || typeof item.id !== "string" || !item.id.trim() || typeof item.text !== "string" || !item.text.trim()
      || typeof item.required !== "boolean") return;
    result.push(item);
  }
  if (new Set(result.map(item => item.id)).size !== result.length
    || new Set(result.map(item => item.number)).size !== result.length) return;
  return result.sort((a, b) => Number(a.number) - Number(b.number));
}

function definitions(items: RecordValue[]): string {
  return JSON.stringify(items.map(item => [item.number, item.id, item.text, item.required]));
}

export function browserCommandCorrection(failed: ToolTimelineItem, success: ToolTimelineItem): BrowserCorrection | undefined {
  const first = operation(failed), last = operation(success);
  if (!first || !last || first.key !== last.key || failed.status !== "error" || success.status !== "success"
    || !failed.commandResult?.settled || failed.commandResult.exitCode == null || failed.commandResult.exitCode === 0
    || !success.commandResult?.settled || success.commandResult.exitCode !== 0) return;
  const passed = report(success), rejected = report(failed);
  const sourceCheckRemoved = Boolean(first.sourceRoot && last.sourceRoot === undefined && first.readySelector === last.readySelector
    && rejected?.schema === undefined && rejected?.passed === false && Array.isArray(rejected.errors) && rejected.errors.length === 1
    && rejected.errors[0] === "ValueError: Keep capture outputs outside source_root (or in its ignored .temp directory)");
  if (first.sourceRoot !== last.sourceRoot && !sourceCheckRemoved) return;
  if (!passed || !rejected || rejected.passed !== false
    || passed.schema !== "minicode.check-report.v1" || passed.scope !== "browser-scenarios"
    || passed.completed !== true || passed.passed !== true
    || !Array.isArray(passed.errors) || passed.errors.length || !Array.isArray(passed.views)
    || passed.views.length !== last.views.length || !Array.isArray(passed.checks) || !passed.checks.length) return;
  const views = passed.views.map(object), checks = passed.checks.map(object);
  if (views.some(view => !view || !last.views.includes(String(view.name)) || !Array.isArray(view.errors) || view.errors.length)
    || new Set(views.map(view => view?.name)).size !== last.views.length
    || checks.some(check => !check || typeof check.name !== "string" || !check.name.trim() || check.passed !== true)
    || new Set(checks.map(check => check?.name)).size !== checks.length
    || last.views.some(view => !checks.some(check => String(check?.name).startsWith(`${view}/`)))) return;
  // Without a criteria file, the successful scenario only resolves this
  // startup error. It does not attest task acceptance or source consistency.
  if (!last.hasCriteria) return sourceCheckRemoved && passed.all_criteria_covered === false
    && Array.isArray(passed.criterion_results) && passed.criterion_results.length === 0 ? "source-check-removed" : undefined;
  if (passed.all_criteria_covered !== true) return;
  const declared = criteria(passed.criterion_results);
  if (!declared || declared.some(item => item.status !== "passed" || !Array.isArray(item.missing_views) || item.missing_views.length
    || !Array.isArray(item.checks) || !item.checks.length
    || last.views.some(view => !checks.some(check => check && String(check.name).startsWith(`${view}/`)
      && (item.checks as unknown[]).includes(check.name) && Array.isArray(check.covers) && check.covers.includes(item.number))))) return;
  if (sourceCheckRemoved) return "source-check-removed";
  if (rejected.schema === "minicode.check-report.v1" && rejected.scope === "browser-scenarios") {
    const original = criteria(rejected.criterion_results);
    return original && definitions(original) === definitions(declared) ? "verified" : undefined;
  }
  // These exact native preflight errors precede all browser assertions. A later
  // complete report for the same criteria file can resolve its invalid format.
  const invalidCriteria = new Set(["ValueError: Criteria file must declare acceptanceCriteria",
    "ValueError: Declare at most 64 distinct nonblank task criteria"]);
  return rejected.schema === undefined && Array.isArray(rejected.errors)
    && rejected.errors.length === 1 && invalidCriteria.has(String(rejected.errors[0])) ? "verified" : undefined;
}
