import type { RunEvent } from "../types";

export type UserQuestion = NonNullable<RunEvent["question"]>;
export interface QuestionDraft { selected: string; text: string }
export const emptyQuestionDraft: QuestionDraft = { selected: "", text: "" };

export function questionAnswer(draft: QuestionDraft): string {
  return [draft.selected, draft.text.trim()].filter(Boolean).join("\n\n");
}

export function validQuestionAnswer(draft: QuestionDraft): boolean {
  const answer = questionAnswer(draft);
  return Boolean(answer) && answer.length <= 12_000;
}

export function questionChoices(question: UserQuestion) {
  const options = question.options || [];
  const marker = /\s*[（(](?:推荐|recommended)[）)]\s*$/i;
  const recommended = question.recommendedOption !== undefined
    ? options.indexOf(question.recommendedOption) : options.findIndex(option => marker.test(option));
  return options.map((value, index) => ({ value, label: value.replace(marker, "").trim(), recommended: index === recommended }));
}

export function questionDraftKey(runId: string, requestId: string) {
  return `minicode-question:${runId}:${requestId}`;
}

export function restoreQuestionDraft(runId: string, event: RunEvent): QuestionDraft {
  try {
    const saved = JSON.parse(sessionStorage.getItem(questionDraftKey(runId, event.requestId || "")) || "null");
    if (saved && typeof saved.text === "string" && typeof saved.selected === "string")
      return { selected: event.question?.options?.includes(saved.selected) ? saved.selected : "", text: saved.text };
  } catch { /* An unavailable browser store must not prevent answering. */ }
  return emptyQuestionDraft;
}
