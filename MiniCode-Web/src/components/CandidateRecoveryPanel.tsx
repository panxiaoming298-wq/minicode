import { ArchiveRestore, Copy, LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { RecoverableCandidate } from "../types";
import "./CandidateRecoveryPanel.css";

export function CandidateRecoveryPanel({ sessionId, active }: { sessionId: string; active: boolean }) {
  const [candidates, setCandidates] = useState<RecoverableCandidate[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const inFlight = useRef(false);
  useEffect(() => {
    let canceled = false;
    void api.candidateRecoveries(sessionId).then(({ candidates: values }) => {
      if (!canceled) setCandidates(values);
    }).catch(reason => { if (!canceled) setError(reason instanceof Error ? reason.message : "无法读取候选恢复记录。"); });
    return () => { canceled = true; };
  }, [sessionId]);

  async function restore(candidate: RecoverableCandidate) {
    if (inFlight.current || active) return;
    if (!window.confirm("仅将这份失败候选恢复到一个新的隔离目录。它尚未验证或合入；不会修改原目录、重跑模型或将原任务改为成功。是否继续？")) return;
    inFlight.current = true;
    setBusy(candidate.artifactId); setError("");
    try {
      const { recovery } = await api.restoreCandidate(sessionId, candidate.runId, candidate.artifactId);
      setCandidates(values => values.map(value => value.artifactId === candidate.artifactId ? { ...value, recovery } : value));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "恢复失败；原任务未重跑、未合入。");
    } finally {
      inFlight.current = false; setBusy(null);
    }
  }

  if (!candidates.length && !error) return null;
  return <section className="inspector-section candidate-recovery-section" aria-label="未提交候选恢复">
    <div className="section-heading"><h2><ArchiveRestore size={15} />未提交候选</h2><span>{candidates.length} 份</span></div>
    <p className="section-note">只恢复文件到新隔离目录。未验证、未合入；原任务状态不变，也不会调用模型。</p>
    {error && <p className="worktree-error" role="alert">{error}</p>}
    {candidates.map(candidate => <div className="candidate-recovery-card" key={candidate.artifactId}>
      <strong>{candidate.nodeId} · 失败候选</strong>
      <small>运行 {candidate.runId} · SHA {candidate.contentHash.slice(0, 12)}</small>
      {candidate.recovery?.status === "restored" && candidate.recovery.workspace ? <>
        <p role="status">已恢复隔离副本 · 未验证、未合入</p>
        <code className="candidate-recovery-path">{candidate.recovery.workspace}</code>
        <button className="button button-secondary" onClick={() => {
          void navigator.clipboard.writeText(candidate.recovery!.workspace!).then(() => setCopied(candidate.artifactId))
            .catch(() => setError("无法复制，请手动选择上方路径。"));
        }}><Copy size={14} />{copied === candidate.artifactId ? "已复制路径" : "复制目录路径"}</button>
      </> : candidate.recovery ? <p>上次恢复未完成；记录与目录已保留，需要人工检查，不会自动重试。</p> :
        <button className="button button-secondary" disabled={Boolean(busy) || active} onClick={() => void restore(candidate)}>
          {busy === candidate.artifactId && <LoaderCircle size={14} className="spin" />}恢复到新隔离目录
        </button>}
      {active && <small>任务结束后才可恢复。</small>}
    </div>)}
  </section>;
}
