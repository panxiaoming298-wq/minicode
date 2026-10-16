import type { useTaskFile } from "../lib/useTaskFile";
import "./TaskFilePicker.css";

export function TaskFilePicker({ task, disabled }: { task: ReturnType<typeof useTaskFile>; disabled: boolean }) {
  const { draft } = task;
  return <details className="task-file-picker">
    <summary>任务验收契约 <span>{draft.trusted ? "已确认 · 仅下一轮" : draft.path ? "待确认" : "可选"}</span></summary>
    <p>选择后端本机的 JSON 文件，格式与 Headless --task-file 相同。仅本次发送生效，可另设单次任务预算；契约任务结束后再发送下一条消息。</p>
    <label>本机契约文件<input value={draft.path} disabled={disabled} placeholder="例如 task.json 或完整路径" onChange={event => task.setPath(event.target.value)} /></label>
    <div className="task-file-actions"><button type="button" disabled={disabled || draft.busy || !draft.path.trim()} onClick={() => void task.preview()}>{draft.busy ? "正在预览…" : "预览契约"}</button>
      <button type="button" disabled={disabled || !draft.path} onClick={task.clear}>移除契约</button></div>
    {draft.error && <p role="alert">{draft.error}</p>}
    {draft.preview && <div className="task-file-preview">
      <p>SHA-256：<code>{draft.preview.sha256}</code></p>
      <p>{draft.preview.requiresWorkspaceWrite ? "需要修改工作区" : "声明为只读任务"} · {draft.preview.verificationCommandCount} 个验收命令</p>
      <strong>验收条件</strong><ol>{draft.preview.acceptanceCriteria.map((value, index) => <li key={index}>{value}</li>)}</ol>
      {draft.preview.taskConstraints.length > 0 && <><strong>任务约束</strong><ul>{draft.preview.taskConstraints.map((value, index) => <li key={index}>{value}</li>)}</ul></>}
      {draft.preview.expectedOutputs.length > 0 && <><strong>预期产物（不是写入白名单）</strong><ul>{draft.preview.expectedOutputs.map(value => <li key={value}><code>{value}</code></li>)}</ul></>}
      <strong>将执行的验收命令</strong>{draft.preview.runtimeVerificationCommands.map(command => <div key={command.name}>
        <p>{command.name} · 覆盖条件 {command.covers.join(", ")} · 最长 {command.timeout} 秒</p>
        {command.browser && <p>浏览器交互检查 · 场景 <code>{command.browser.scenario}</code></p>}
        <pre>{JSON.stringify(command.browser ?? command.command, null, command.browser ? 2 : undefined)}</pre>
      </div>)}
      <p>{draft.preview.warning}</p>
      <label className="task-file-trust"><input type="checkbox" checked={draft.trusted} disabled={disabled} onChange={event => task.confirm(event.target.checked)} />我已审阅并信任以上验收命令</label>
    </div>}
  </details>;
}
