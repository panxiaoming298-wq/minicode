import { useContext, useId } from "react";
import { Check, MessageCircleQuestion } from "lucide-react";
import type { TimelineItem } from "../types";
import { questionProgress } from "../lib/execution";
import { questionAnswer, questionChoices } from "../lib/questions";
import { QuestionPrompt } from "./QuestionPrompt";
import { QuestionActions } from "./QuestionSession";
import { QuestionOption } from "./QuestionOption";
import "./QuestionPrompt.css";

export function QuestionRecord({ item, items }: { item: Extract<TimelineItem, { type: "progress" }>; items: TimelineItem[] }) {
  const { run, activeId, openRequest, onAnswer, drafts, sent, busy } = useContext(QuestionActions);
  const titleId = useId();
  if (!item.question || !item.questionId) return null;
  const pending = run?.status === "running" && !item.questionResolution
    && run.pendingQuestions?.some(question => question.requestId === item.questionId);
  const answered = item.questionResolution === "answered";
  const draftAnswer = pending && drafts[item.questionId] ? questionAnswer(drafts[item.questionId]) : "";
  const submitted = sent.includes(item.questionId);
  const progress = questionProgress({ runId: item.questionRunId || run?.runId || "", pendingQuestions: run?.pendingQuestions },
    { requestId: item.questionId, turnId: item.questionTurnId, executionId: item.executionId,
      questionGroupId: item.questionGroupId, questionIndex: item.questionIndex, questionCount: item.questionCount }, items);
  if (pending && item.questionId === activeId && run) return <QuestionPrompt key={item.questionId} requestId={item.questionId}
    question={item.question} progress={progress} grouped={Boolean(item.questionGroupId && progress.total > 1)} openRequest={openRequest || 0} />;
  return <section id={item.id} className="question-record" aria-labelledby={titleId} data-question-id={item.questionId}>
    <header>
      <MessageCircleQuestion size={17} aria-hidden="true" />
      <strong id={titleId}>需要你补充的信息</strong>
      <span className="question-progress">第 {progress.current}/{progress.total} 题</span>
      <span className="question-record-status">{answered ? "已回答" : submitted ? "回答已发送" : draftAnswer ? "已填写 · 可修改" : pending ? "等待你的回答" : "未回答"}</span>
    </header>
    <p className="question-text">{item.question.question}</p>
    {Boolean(item.question.options?.length) && <ol className="question-record-options" aria-label="可选答案">
      {questionChoices(item.question).map(option => <li key={option.value}><QuestionOption {...option} /></li>)}
    </ol>}
    {(answered || draftAnswer) && <div className="question-record-answer">
      <strong><Check size={14} aria-hidden="true" />{answered || submitted ? "你的回答" : "你的回答（待提交）"}</strong>
      <p className="question-text">{answered ? item.questionAnswer : draftAnswer}</p>
    </div>}
    {pending && !submitted && onAnswer && <button type="button" className="button button-secondary" disabled={busy}
      onClick={() => onAnswer(item.questionId!)}>{draftAnswer ? "修改回答" : "回答这道问题"}</button>}
  </section>;
}
