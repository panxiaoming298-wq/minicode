import type { ToolTimelineItem } from "./runSummary";

type Operation = { key: string; targets: string[] };
type Summary = { passed: number; failed: number; incomplete: boolean };

/** Parse only pytest's ordinary test invocation. Unknown plugin flags, rerun
 * selectors and collection-only commands cannot establish expanded coverage. */
function operation(item: ToolTimelineItem): Operation | undefined {
  if (!item.input || typeof item.input !== "object" || Array.isArray(item.input)) return;
  const input = item.input as Record<string, unknown>;
  if (typeof input.command !== "string" || typeof input.cwd !== "string" || !input.cwd.trim()
    || !Array.isArray(input.args) || !input.args.every((arg): arg is string => typeof arg === "string")) return;
  const args = input.args;
  let offset = 0;
  if (/(?:^|[\\/])python(?:3(?:\.\d+)?)?(?:\.exe)?$/i.test(input.command)) {
    while (["-I", "-B", "-u"].includes(args[offset])) offset++;
    if (args[offset] !== "-m" || args[offset + 1] !== "pytest") return;
    offset += 2;
  } else if (!/(?:^|[\\/])pytest(?:\.exe)?$/i.test(input.command)) return;
  const scope = args.slice(0, offset), targets: string[] = [];
  const switches = new Set(["--disable-warnings", "--strict-markers", "--strict-config", "--no-header", "--no-summary"]);
  const values = new Set(["-k", "-m", "-c", "--rootdir", "--confcutdir", "--tb", "--color"]);
  let positional = false;
  for (let index = offset; index < args.length; index++) {
    const arg = args[index];
    if (!positional && arg === "--") { positional = true; continue; }
    // Verbosity changes the report, not which tests run.
    if (!positional && (/^-[qv]+$/.test(arg) || ["--quiet", "--verbose"].includes(arg))) continue;
    if (!positional && switches.has(arg)) { scope.push(arg); continue; }
    if (!positional && arg.startsWith("-")) {
      const equals = arg.indexOf("="), flag = equals < 0 ? arg : arg.slice(0, equals);
      if (!values.has(flag)) return;
      const value = equals < 0 ? args[++index] : arg.slice(equals + 1);
      if (!value?.trim() || value.startsWith("-")) return;
      scope.push(flag, value);
      continue;
    }
    if (!arg.trim() || arg.startsWith("-") || /[*?\x00]/.test(arg)) return;
    targets.push(normalizeTarget(arg));
  }
  const stable = Object.entries({ ...input, args: scope }).filter(([key]) => key !== "timeout")
    .sort(([a], [b]) => a.localeCompare(b));
  return { key: JSON.stringify([item.executionId, item.nodeId, item.attemptId, stable]), targets };
}

function normalizeTarget(value: string): string {
  const [path, ...nodes] = value.split("::");
  return [path.replaceAll("\\", "/").replace(/^(?:\.\/)+/, "").replace(/\/$/, ""), ...nodes].join("::");
}

function output(item: ToolTimelineItem): string {
  return (item.commandResult?.stdout ?? item.output).replace(/\x1b\[[\d;]*m/g, "");
}

function summary(item: ToolTimelineItem): Summary | undefined {
  for (const raw of output(item).split(/\r?\n/).reverse()) {
    const line = raw.trim().replace(/^=+\s*|\s*=+$/g, "");
    const match = /^(\d+ (?:passed|failed|skipped|deselected|xfailed|xpassed|warnings?|errors?)(?:, \d+ (?:passed|failed|skipped|deselected|xfailed|xpassed|warnings?|errors?))*) in \d+(?:\.\d+)?s(?: \([^\r\n]+\))?$/.exec(line);
    if (!match) continue;
    const result: Summary = { passed: 0, failed: 0, incomplete: false };
    for (const entry of match[1].split(", ")) {
      const [count, status] = entry.split(" ");
      if (status === "passed" || status === "failed") result[status] += Number(count);
      else if (!status.startsWith("warning") && Number(count) > 0) result.incomplete = true;
    }
    return result;
  }
}

export function pytestCommandKey(item: ToolTimelineItem): string | undefined {
  return operation(item)?.key;
}

/** Display linkage only. Explicit superset targets preserve the failed check;
 * default collection or a parent directory additionally needs PASSED rows for
 * every originally failing node. A passing count alone cannot prove that scope. */
export function pytestCommandCorrection(failed: ToolTimelineItem, success: ToolTimelineItem): boolean {
  const first = operation(failed), last = operation(success);
  if (!first || !last || first.key !== last.key || failed.status !== "error" || success.status !== "success"
    || !failed.commandResult?.settled || failed.commandResult.exitCode !== 1
    || !success.commandResult?.settled || success.commandResult.exitCode !== 0) return false;
  const rejected = summary(failed), passed = summary(success);
  if (!rejected || !passed || rejected.incomplete || passed.incomplete || rejected.failed < 1 || passed.failed
    || passed.passed < rejected.passed + rejected.failed) return false;
  if (first.targets.length ? first.targets.every(target => last.targets.includes(target)) : !last.targets.length) return true;
  const failures = output(failed).split(/\r?\n/).flatMap(line => {
    const match = /^FAILED (.+?)(?: - .*)?$/.exec(line.trim());
    return match && match[1].includes("::") ? [normalizeTarget(match[1])] : [];
  });
  const completed = new Set(output(success).split(/\r?\n/).flatMap(line => {
    const match = /^(.+?::.+?) PASSED(?:\s+\[\s*\d+%\])?$/.exec(line.trim());
    return match ? [normalizeTarget(match[1])] : [];
  }));
  return new Set(failures).size === rejected.failed && failures.every(node => completed.has(node));
}
