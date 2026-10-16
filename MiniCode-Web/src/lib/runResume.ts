import type { SessionTurn, TimelineItem } from "../types";

export const RESUME_PROMPT = "继续上次任务。先确认已完成和未完成的部分，再从中断处接着处理。";
export const RETRY_PROMPT = "重试未完成的任务。先检查失败原因和当前状态，再从未完成处继续。";
const SAVED_CONTINUATION = "按当前预算从已保存的主任务进度继续，保留原验收条件。";
const ORIGINAL_REQUEST = "\n\n请继续完成以下原任务要求，先核对当前工作区与未完成项：\n";

/** A retry without a restorable candidate still needs the full user objective. */
export function resumeTaskPrompt(
  items: TimelineItem[],
  failed: boolean,
  savedTurns: Pick<SessionTurn, "prompt">[] = [],
): string | undefined {
  const controls = [RESUME_PROMPT, RETRY_PROMPT, SAVED_CONTINUATION];
  // Saved conversation views can omit the process timeline entirely. Their
  // turn prompts are still the original user input and must survive a retry.
  const prompts = [...savedTurns.map(turn => turn.prompt),
    ...items.flatMap(item => item.type === "user" ? [item.content] : [])];
  for (let index = prompts.length - 1; index >= 0; index--) {
    const content = prompts[index].trim();
    if (!content || controls.includes(content)
      || [RESUME_PROMPT, RETRY_PROMPT].some(prefix => content.startsWith(prefix + ORIGINAL_REQUEST))) continue;
    return (failed ? RETRY_PROMPT : RESUME_PROMPT) + ORIGINAL_REQUEST + prompts[index];
  }
  return undefined;
}
