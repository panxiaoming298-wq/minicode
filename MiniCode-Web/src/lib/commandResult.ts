import type { CommandCheckReport } from "../types";
import type { ToolTimelineItem } from "./runSummary";

function failedCheckReport(item: ToolTimelineItem): CommandCheckReport | undefined {
  if (item.toolName !== "run_command" || item.status !== "error"
    || !item.commandResult?.settled || item.commandResult.exitCode !== 0) return undefined;
  const check = item.commandCheckReport;
  if (check && ["unverified", "failed"].includes(check.status)) return check;
  // Older native receipts stored this diagnostic only in the result footer.
  // Use it only to explain failure, never to grant check success or recovery.
  if (!item.input || typeof item.input !== "object" || !("check_report" in item.input)
    || typeof item.input.check_report !== "string") return undefined;
  const footer = "\n\nCommand check report (script-produced evidence): ";
  const offset = item.output.lastIndexOf(footer);
  if (offset < 0) return undefined;
  try {
    const value = JSON.parse(item.output.slice(offset + footer.length).split(/\r?\n/, 1)[0]);
    if (value && typeof value.path === "string" && ["unverified", "failed"].includes(value.status)) {
      return { path: value.path, status: value.status,
        reason: typeof value.reason === "string" ? value.reason : undefined,
        failedChecks: Array.isArray(value.failedChecks) ? value.failedChecks.filter((name: unknown) => typeof name === "string") : undefined };
    }
  } catch { /* An incomplete footer cannot explain the check failure. */ }
  return undefined;
}

/** A diagnostic hint only; shell syntax never establishes command success. */
export function hidesPowerShellErrors(input: unknown): boolean {
  if (!input || typeof input !== "object") return false;
  const { command, args } = input as { command?: unknown; args?: unknown };
  if (typeof command !== "string" || !/(?:^|[\\/])(?:powershell|pwsh)(?:\.exe)?$/i.test(command)
    || !Array.isArray(args) || !args.every(arg => typeof arg === "string")) return false;
  const index = args.findIndex(arg => /^(?:-command|-c)$/i.test(arg));
  if (index < 0) return false;
  // Ignore literals, comments and escaped characters. Dynamic/encoded scripts
  // remain unknown; this is deliberately not a general PowerShell parser.
  const script = args.slice(index + 1).join(" ").replace(
    /'(?:''|[^'])*'|"(?:`[\s\S]|""|[^"`])*"|<#[\s\S]*?#>|#[^\r\n]*|`[\s\S]/g, " ");
  return /(?:^|[\s;|&(])[2*]\s*>>?\s*\$null\b/i.test(script);
}

function hasCommandDiagnosticFailure(item: ToolTimelineItem): boolean {
  // Keep lifecycle errors and failed check reports visible, even with exit 0.
  return item.toolName === "run_command" && item.status === "error"
    && !/canceled|timed out|tool deadline|cleanup is not confirmed|command check report rejected/i.test(item.output);
}

export function commandFailureLabel(item: ToolTimelineItem): string | undefined {
  const check = failedCheckReport(item);
  if (check) return check.status === "failed" ? "检查报告未通过" : "检查报告未生成或无效";
  if (hasCommandDiagnosticFailure(item) && !item.commandResult && hidesPowerShellErrors(item.input)) {
    return "命令状态异常";
  }
  return undefined;
}

export function commandFailureSummary(item: ToolTimelineItem): string | undefined {
  const check = failedCheckReport(item);
  if (check) return "命令已正常退出（退出码 0），但"
    + (check.status === "failed" ? `附加检查报告中有未通过项${check.failedChecks?.length ? `：${check.failedChecks.join("、")}` : ""}。`
      : "声明的 check_report 没有生成有效的新报告。该参数只读取自定义脚本产物，不会让 pytest 等命令自动生成报告。")
    + "命令输出已保留，整体调用仍未通过；最终验收以登记的检查为准。";
  if (!hasCommandDiagnosticFailure(item)) return undefined;
  const receipt = item.commandResult;
  if (receipt) {
    if (!receipt.settled) return "命令的进程清理或输出收集尚未确认完成，请先核对执行状态。";
    if (receipt.exitCode == null || receipt.exitCode === 0) return undefined;
    return `命令返回了失败退出码 ${receipt.exitCode}。${hidesPowerShellErrors(item.input)
      ? "命令隐藏了错误流，普通提示也可能影响 Windows PowerShell 5.1 的执行状态；请保留诊断信息后核对。"
      : !receipt.stderr.trim() ? "未捕获错误输出，请结合命令和正常输出排查原因。" : "请查看下方错误流和命令输出。"}`;
  }
  if (hidesPowerShellErrors(item.input)) {
    return `${item.output.trim() ? "已取得命令输出，但旧记录的执行状态为失败。" : "旧记录的执行状态为失败。"}命令隐藏了错误流，普通提示也可能影响 Windows PowerShell 5.1 的执行状态。记录未保存退出码，无法仅凭旧记录确定原因。`;
  }
  return undefined;
}
