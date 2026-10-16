import type { TimelineItem } from "../types";
import type { ToolTimelineItem } from "./runSummary";
import { browserCommandCorrection, browserCommandKey } from "./browserCommandRecovery";
import { pytestCommandCorrection, pytestCommandKey } from "./pytestCommandRecovery";

function commandInput(item: ToolTimelineItem): { command: string; args: string[]; [key: string]: unknown } | undefined {
  if (!item.input || typeof item.input !== "object" || Array.isArray(item.input)) return undefined;
  const input = item.input as Record<string, unknown>;
  const args = input.args ?? [];
  if (typeof input.command !== "string" || !input.command.trim() || !Array.isArray(args)
    || !args.every(arg => typeof arg === "string")) return undefined;
  // A help/version probe succeeds without performing the requested operation.
  if (args.some(arg => ["--help", "-h", "--version", "-V", "--usage"].includes(arg))) return undefined;
  return { ...input, command: input.command, args };
}

function rejectedOption(item: ToolTimelineItem): { option: string; suggestion?: string } | undefined {
  const receipt = item.commandResult;
  if (receipt && (!receipt.settled || receipt.exitCode == null || receipt.exitCode === 0)) return undefined;
  const output = receipt ? [receipt.stdout, receipt.stderr].filter(Boolean).join("\n") : item.output;
  // Require the complete CLI diagnostic, not a quoted error in ordinary output.
  const match = /^(?:error: )?unknown option (['"])(--?[a-zA-Z][\w-]*)\1(?:\r?\n\(Did you mean (['"]?)(--?[a-zA-Z][\w-]*)\3\?\))?$/i.exec(output.trim());
  return match ? { option: match[2], suggestion: match[4] } : undefined;
}

export function commandRecoveryPresentation(failed: ToolTimelineItem, success: ToolTimelineItem): { label: string; summary: string } {
  if (pytestCommandCorrection(failed, success)) return { label: "后续已纠正",
    summary: "后续 pytest 测试已通过，覆盖了原失败测试。原始命令和错误保留在下方，可展开查看成功结果。" };
  const browserCorrection = browserCommandCorrection(failed, success);
  if (browserCorrection === "source-check-removed") return { label: "后续执行成功",
    summary: "同一浏览器场景后续已执行成功，已取消源码一致性检查；此状态不代表完整验收通过。原始输入与错误保留在下方，可展开查看后续成功命令。" };
  if (browserCorrection) return { label: "后续已纠正",
    summary: "同一浏览器检查后续已通过，声明的验收条件和视图均已完成检查。原始输入与错误保留在下方，可展开查看后续成功命令。" };
  const changedArgs = JSON.stringify(commandInput(failed)?.args) !== JSON.stringify(commandInput(success)?.args);
  return changedArgs
    ? { label: "后续已纠正", summary: "已纠正报错指出的不支持参数，后续命令执行成功。原始输入与错误保留在下方。" }
    : { label: "重试后已恢复", summary: "同一命令后续已执行成功，原始失败记录保留在下方。" };
}

/** Keep the complete operation and execution scope. Generic argument changes
 * need an exact CLI diagnostic. The native browser checker additionally permits
 * readiness repairs backed by a complete report for the same declared checks,
 * or a narrowly identified source-output preflight recovery with its own label.
 */
export function correctedCommandResults(items: TimelineItem[]): Map<string, ToolTimelineItem> {
  const pending = new Map<string, ToolTimelineItem[]>();
  const browserPending = new Map<string, ToolTimelineItem[]>();
  const pytestPending = new Map<string, ToolTimelineItem[]>();
  const corrected = new Map<string, ToolTimelineItem>();
  for (const item of items) {
    if (item.type === "user") { pending.clear(); browserPending.clear(); pytestPending.clear(); continue; }
    if (item.type !== "tool" || item.toolName !== "run_command"
      || !item.executionId || !item.nodeId || !item.attemptId || !item.toolCallId) continue;
    if (item.toolInput?.argumentError || item.toolInput && !["other", "success"].includes(item.toolInput.status)) continue;
    // Native command_timeout attests that the process was stopped and cleaned
    // up. Other recovery protocols and cancellations keep their own linkage.
    if (item.toolRecovery && (item.toolRecovery.kind !== "command_timeout"
      || item.toolRecovery.status !== (item.status === "error" ? "agent_action" : "recovered"))) continue;
    if (item.commandResult && (!item.commandResult.settled || item.commandResult.exitCode == null)) continue;
    if (/^(?:Canceled:|Command canceled|NotExecuted:|UserSteeringPending:|ModelToolArgumentsRejected:)/i.test(item.output.trim())) continue;
    if (item.status === "error" && !item.toolRecovery && !item.commandResult
      && /^(?:Command|Tool) timed out\b|^Command stopped at the tool deadline\b|cleanup is not confirmed/i.test(item.output)) continue;
    const input = commandInput(item);
    if (!input) continue;
    const browserKey = browserCommandKey(item);
    if (browserKey) {
      if (item.status === "error") browserPending.set(browserKey, [...(browserPending.get(browserKey) || []), item]);
      else if (item.status === "success" && (!item.toolInput || item.toolInput.status === "success")) {
        browserPending.set(browserKey, (browserPending.get(browserKey) || []).filter(failed => {
          if (failed.resultOrder === undefined || item.startOrder === undefined || failed.resultOrder >= item.startOrder
            || !browserCommandCorrection(failed, item)) return true;
          corrected.set(failed.id, item);
          return false;
        }));
      }
      continue;
    }
    const pytestKey = pytestCommandKey(item);
    if (pytestKey && item.commandResult) {
      if (item.status === "error") pytestPending.set(pytestKey, [...(pytestPending.get(pytestKey) || []), item]);
      else if (item.status === "success" && (!item.toolInput || item.toolInput.status === "success")) {
        pytestPending.set(pytestKey, (pytestPending.get(pytestKey) || []).filter(failed => {
          if (failed.resultOrder === undefined || item.startOrder === undefined || failed.resultOrder >= item.startOrder
            || !pytestCommandCorrection(failed, item)) return true;
          corrected.set(failed.id, item);
          return false;
        }));
      }
      continue;
    }
    const keyFor = (args: string[]) => JSON.stringify([item.executionId, item.nodeId, item.attemptId,
      Object.entries({ ...input, args, cwd: input.cwd || null }).filter(([key]) => key !== "timeout").sort(([a], [b]) => a.localeCompare(b))]);
    if (item.status === "error") {
      const variants = [input.args];
      const diagnostic = rejectedOption(item);
      if (diagnostic && input.args.filter(arg => arg === diagnostic.option).length === 1) {
        const index = input.args.indexOf(diagnostic.option);
        if (input.args.indexOf("--") < 0 || input.args.indexOf("--") > index) {
          const removed = input.args.filter((_, at) => at !== index);
          if (removed.length) variants.push(removed);
          if (diagnostic.suggestion) variants.push(input.args.map((arg, at) => at === index ? diagnostic.suggestion! : arg));
        }
      }
      for (const args of variants) {
        const key = keyFor(args);
        pending.set(key, [...(pending.get(key) || []), item]);
      }
    } else if (item.status !== "success" || item.toolInput && item.toolInput.status !== "success"
      || item.commandResult && (!item.commandResult.settled || item.commandResult.exitCode !== 0)) continue;
    else {
      const key = keyFor(input.args);
      pending.set(key, (pending.get(key) || []).filter(failed => {
        if (corrected.has(failed.id)) return false;
        if (failed.resultOrder === undefined || item.startOrder === undefined || failed.resultOrder >= item.startOrder) return true;
        corrected.set(failed.id, item);
        return false;
      }));
    }
  }
  return corrected;
}
