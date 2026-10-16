import { useContext, useEffect, useId, useRef, useState } from "react";
import { Check, LoaderCircle, MessageCircleQuestion } from "lucide-react";
import { emptyQuestionDraft, questionAnswer, questionChoices, validQuestionAnswer, type UserQuestion } from "../lib/questions";
import { QuestionActions } from "./QuestionSession";
import { QuestionOption } from "./QuestionOption";
import "./QuestionPrompt.css";

interface Props {
  requestId: string;
  question: UserQuestion;
  progress: { current: number; total: number };
  openRequest: number;
  grouped?: boolean;
}

export function QuestionPrompt({ requestId, question, progress, openRequest, grouped }: Props) {
  const form = useRef<HTMLFormElement>(null);
  const actions = useContext(QuestionActions);
  const [expanded, setExpanded] = useState(true);
  const titleId = useId();
  const textId = useId();
  const progressId = useId();
  const draft = actions.drafts[requestId] || emptyQuestionDraft;
  const { busy } = actions;
  const sent = actions.sent.includes(requestId);
  const error = actions.error?.requestId === requestId ? actions.error.message : "";
  const answer = questionAnswer(draft);
  const choices = questionChoices(question);
  const group = actions.groupQuestions(requestId);
  const previous = group[group.findIndex(question => question.requestId === requestId) - 1];
  const ready = actions.groupReady(requestId);
  const completed = group.filter(question => validQuestionAnswer(actions.drafts[question.requestId!] || emptyQuestionDraft)).length;
  const progressLabel = `第 ${progress.current}/${progress.total} 题`;

  useEffect(() => {
    setExpanded(true);
    const frame = requestAnimationFrame(() => {
      form.current?.scrollIntoView({ block: "start" });
      form.current?.querySelector<HTMLElement>("input, textarea")?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [openRequest]);

  return <section id={`question-${requestId}`} className="question-record question-prompt" aria-labelledby={titleId} data-question-id={requestId}>
      <form ref={form} onSubmit={event => { event.preventDefault(); void actions.submit?.(requestId); }} aria-describedby={`${progressId} ${textId}`} aria-busy={busy}>
        <header><div className="question-heading">
          <h2 id={titleId}><MessageCircleQuestion size={17} aria-hidden="true" />需要你补充的信息</h2>
          <span id={progressId} className="question-progress" role="status" aria-label="提问进度"
            title={grouped ? "整组提交前，可以返回修改任意一道题的答案。" : "按当前已收到的问题计数，新增追问时总数会更新。"}>{progressLabel}</span>
        </div>
          <span className="question-record-status">{sent ? "回答已发送" : "等待你的回答"}</span></header>
        <p id={textId} className="question-text">{question.question}</p>
        {expanded && !sent ? <>
        {Boolean(question.options?.length) && <fieldset disabled={busy} className="question-options">
          <legend>选择一个选项，也可以直接输入答案</legend>
          {choices.map(option => <label key={option.value} className={draft.selected === option.value ? "is-selected" : ""}>
            <input type="radio" name={requestId} value={option.value} checked={draft.selected === option.value}
              onChange={() => actions.updateDraft?.(requestId, { ...draft, selected: option.value })} />
            <QuestionOption {...option} />
          </label>)}
          <label className={!draft.selected ? "is-selected" : ""}>
            <input type="radio" name={requestId} value="" checked={!draft.selected}
              onChange={() => actions.updateDraft?.(requestId, { ...draft, selected: "" })} />
            <span>自行填写</span>
          </label>
        </fieldset>}
        <label className="question-answer">{draft.selected ? "补充说明（可选）" : "你的回答"}
          <textarea value={draft.text} disabled={busy} maxLength={12_000} rows={3}
            placeholder={draft.selected ? "还有需要说明的内容…" : "输入你的答案…"}
            onChange={event => actions.updateDraft?.(requestId, { ...draft, text: event.target.value })} />
        </label>
        {answer.length > 12_000 && <p role="alert" className="question-error">答案与选项合计不能超过 12000 字。</p>}
        {error && <p role="alert" className="question-error">{error}</p>}
        <footer><span>{grouped ? `本组已填写 ${completed}/${group.length} 题，提交前均可修改` : "回答后继续当前任务"}</span><div>
          {grouped && previous?.requestId && <button type="button" className="button button-secondary" disabled={busy}
            onClick={() => actions.onAnswer?.(previous.requestId!)}>上一题</button>}
          <button type="button" className="button button-secondary" onClick={() => setExpanded(false)} disabled={busy}>稍后回答</button>
          <button type="submit" className="button button-primary" disabled={busy || sent || !answer || answer.length > 12_000}>
            {busy && <LoaderCircle size={16} className="spin" />}{busy ? "正在发送…" : grouped ? ready ? "提交全部并继续" : "保存并下一题" : "提交并继续"}
          </button>
        </div></footer>
        </> : <>
          {Boolean(question.options?.length) && <ol className="question-record-options" aria-label="可选答案">
            {choices.map(option => <li key={option.value}><QuestionOption {...option} /></li>)}
          </ol>}
          {sent ? <div className="question-record-answer"><strong><Check size={14} aria-hidden="true" />你的回答</strong>
            <p className="question-text">{answer}</p></div>
            : <footer><span>{grouped ? "草稿已保留，整组提交前可随时修改。" : "进度已保留，回答后自动继续。"}</span><button type="button" className="button button-secondary"
              onClick={() => { setExpanded(true); requestAnimationFrame(() => form.current?.querySelector<HTMLElement>("input, textarea")?.focus()); }}>回答这道问题</button></footer>}
        </>}
      </form>
  </section>;
}
