import { ArrowUpRight, RotateCcw } from "lucide-react";
import { readableElapsed } from "../lib/execution";

export interface RunRecoveryAction {
  label: string;
  hint: string;
  disabled: boolean;
  onClick: () => void;
}

export function RecoveryButton({ action }: { action?: RunRecoveryAction }) {
  return action && <button type="button" className="run-recovery-action" disabled={action.disabled}
    title={action.hint} onClick={event => { event.preventDefault(); event.stopPropagation(); action.onClick(); }}>
    <RotateCcw size={14} aria-hidden="true" />{action.label}
  </button>;
}

export function StopLabel({ elapsedMs }: { elapsedMs?: number }) {
  const duration = elapsedMs !== undefined && Number.isFinite(elapsedMs) && elapsedMs >= 0 ? readableElapsed(elapsedMs) : null;
  return <span className="round-stop-label" role="status" aria-label="本轮已停止">已停止{duration && <> · 用时 {duration}</>}</span>;
}

export function StopActions({ recoveryAction, onOpenRuntime }: { recoveryAction?: RunRecoveryAction; onOpenRuntime?: () => void }) {
  if (!recoveryAction && !onOpenRuntime) return null;
  return <span className="round-stop-actions">
    <RecoveryButton action={recoveryAction} />
    {onOpenRuntime && <button type="button" onClick={event => { event.preventDefault(); event.stopPropagation(); onOpenRuntime(); }}>
      查看运行详情<ArrowUpRight size={13} aria-hidden="true" />
    </button>}
  </span>;
}

export function RunStopLine({ elapsedMs, recoveryAction, onOpenRuntime }: {
  elapsedMs?: number; recoveryAction?: RunRecoveryAction; onOpenRuntime?: () => void;
}) {
  return <div className="round-stop-line"><StopLabel elapsedMs={elapsedMs} /><StopActions recoveryAction={recoveryAction} onOpenRuntime={onOpenRuntime} /></div>;
}
