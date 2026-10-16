import type { ExecutionLocation } from "../types";

export function ExecutionLocationFields({ value, workspace, active, disabled, error, pendingWorktree, onChange }: {
  value: ExecutionLocation | undefined; workspace: string; active: boolean; disabled: boolean;
  error: string; onChange: (value: ExecutionLocation) => void;
  pendingWorktree?: boolean;
}) {
  return <div className="execution-location-fields">
    <label>{active ? "本轮文件写入位置" : "下次文件写入位置"}
      <select aria-label="文件写入位置" value={value || ""} disabled={disabled}
        onChange={event => onChange(event.target.value as ExecutionLocation)}>
        {!value && <option value="">正在确认执行位置</option>}
        <option value="project">当前目录 · 直接修改（默认）</option>
        <option value="isolated">隔离副本 · 检查后写回</option>
      </select>
    </label>
    <p>{value === "project" ? "主 Agent 在当前目录写入；启用可写子 Agent 时按文件范围协作，同一范围排队修改。失败时保留已写文件，最终验证仍须通过。"
      : value === "isolated" ? "在临时副本中执行，支持子 Agent 并行写入；检查通过后写回目标目录。候选文件和实际路径见文件面板。" : "等待运行信息。"}</p>
    <p className="execution-location-path">{pendingWorktree ? "Worktree 来源" : value === "project" ? "执行目录" : "目标目录"}：{workspace}</p>
    {pendingWorktree && <p>发送时先创建新的 Worktree，再在其目录执行；创建后显示实际目录。</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
