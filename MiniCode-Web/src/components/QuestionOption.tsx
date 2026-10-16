export function QuestionOption({ label, recommended }: { label: string; recommended: boolean }) {
  return <span className="question-option-label">{label}{recommended && <> <span className="question-recommendation">推荐</span></>}</span>;
}
