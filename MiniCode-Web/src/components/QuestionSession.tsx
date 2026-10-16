import { createContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { RunEvent, RunInfo, TimelineItem } from "../types";
import { emptyQuestionDraft, questionAnswer, questionDraftKey, restoreQuestionDraft, validQuestionAnswer, type QuestionDraft } from "../lib/questions";

interface Actions {
  run: RunInfo | null;
  activeId?: string;
  openRequest?: number;
  drafts: Record<string, QuestionDraft>;
  sent: string[];
  busy: boolean;
  error?: { requestId: string; message: string };
  onAnswer?: (requestId: string) => void;
  updateDraft?: (requestId: string, draft: QuestionDraft) => void;
  submit?: (requestId: string) => Promise<void>;
  groupQuestions: (requestId: string) => RunEvent[];
  groupReady: (requestId: string) => boolean;
}

export const QuestionActions = createContext<Actions>({ run: null, drafts: {}, sent: [], busy: false,
  groupQuestions: () => [], groupReady: () => false });

interface Props {
  run: RunInfo | null;
  items: TimelineItem[];
  activeId?: string;
  openRequest?: number;
  onAnswer?: (requestId: string) => void;
  children: ReactNode;
}

/** Keep every answer editable until the user submits the complete group. */
export function QuestionSession({ run, items, activeId, openRequest, onAnswer, children }: Props) {
  const [drafts, setDrafts] = useState<Record<string, QuestionDraft>>(() => Object.fromEntries(
    (run?.pendingQuestions || []).filter(question => question.requestId).map(question =>
      [question.requestId!, restoreQuestionDraft(run!.runId, question)])));
  const [sent, setSent] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Actions["error"]>();
  const submitting = useRef(false);

  useEffect(() => {
    if (!run) return;
    setDrafts(current => {
      const missing = (run.pendingQuestions || []).filter(question => question.requestId && !current[question.requestId]);
      return missing.length ? { ...current, ...Object.fromEntries(missing.map(question =>
        [question.requestId!, restoreQuestionDraft(run.runId, question)])) } : current;
    });
  }, [run?.runId, run?.pendingQuestions]);

  function groupQuestions(requestId: string) {
    const question = run?.pendingQuestions?.find(question => question.requestId === requestId);
    if (!question) return [];
    return question.questionGroupId && (question.questionCount || 0) > 1
      ? (run!.pendingQuestions || []).filter(other => other.questionGroupId === question.questionGroupId)
        .sort((a, b) => (a.questionIndex || 0) - (b.questionIndex || 0)) : [question];
  }

  function groupReady(requestId: string) {
    const questions = groupQuestions(requestId);
    if (!questions.length) return false;
    const question = questions[0];
    const resolved = items.filter(item => item.type === "progress" && item.questionGroupId === question.questionGroupId
      && item.questionResolution === "answered").length;
    return (!question.questionGroupId || questions.length + resolved >= (question.questionCount || 1))
      && questions.every(question => validQuestionAnswer(drafts[question.requestId!] || emptyQuestionDraft));
  }

  function selectQuestion(requestId: string) {
    if (!submitting.current) onAnswer?.(requestId);
  }

  function updateDraft(requestId: string, draft: QuestionDraft) {
    if (!run || submitting.current || sent.includes(requestId)) return;
    setDrafts(current => ({ ...current, [requestId]: draft }));
    setError(undefined);
    try { sessionStorage.setItem(questionDraftKey(run.runId, requestId), JSON.stringify(draft)); }
    catch { /* Keep the in-memory draft when storage is unavailable. */ }
  }

  async function submit(requestId: string) {
    if (!run || submitting.current || sent.includes(requestId) || !validQuestionAnswer(drafts[requestId] || emptyQuestionDraft)) return;
    const questions = groupQuestions(requestId);
    if (!questions.length) return;
    const grouped = Boolean(questions[0].questionGroupId && (questions[0].questionCount || 0) > 1);
    if (grouped && !groupReady(requestId)) {
      const current = questions.findIndex(question => question.requestId === requestId);
      const next = [...questions.slice(current + 1), ...questions.slice(0, current)]
        .find(question => !validQuestionAnswer(drafts[question.requestId!] || emptyQuestionDraft));
      if (next?.requestId) selectQuestion(next.requestId);
      return;
    }
    submitting.current = true; setBusy(true); setError(undefined);
    try {
      const answers = questions.map(question => ({ requestId: question.requestId!, answer: questionAnswer(drafts[question.requestId!]) }));
      if (grouped) await api.answerQuestionGroup(run.runId, questions[0].questionGroupId!, answers);
      else await api.answerQuestion(run.runId, requestId, answers[0].answer);
      setSent(current => [...current, ...answers.map(answer => answer.requestId)]);
      for (const answer of answers) {
        try { sessionStorage.removeItem(questionDraftKey(run.runId, answer.requestId)); }
        catch { /* Storage is optional. */ }
      }
    } catch (cause) {
      setError({ requestId, message: cause instanceof Error ? cause.message : "答案未发送，请重试。" });
    } finally { submitting.current = false; setBusy(false); }
  }

  return <QuestionActions.Provider value={{ run, activeId, openRequest, drafts, sent, busy, error,
    onAnswer: selectQuestion, updateDraft, submit, groupQuestions, groupReady }}>{children}</QuestionActions.Provider>;
}
