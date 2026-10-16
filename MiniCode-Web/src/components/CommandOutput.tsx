import type { ToolTimelineItem } from "../lib/runSummary";

export function CommandOutput({ item }: { item: ToolTimelineItem }) {
  const result = item.commandResult;
  if (!result) return <>
    {item.output && <div><span>命令输出</span><pre>{item.output}</pre></div>}
    {item.status === "error" && <p>这条记录未保存进程退出码，也未区分正常输出和错误输出。</p>}
  </>;
  const combined = [result.stdout, result.stderr].filter(Boolean).join("\n");
  return <>
    <p>进程退出码：{result.exitCode ?? "未取得"}{!result.settled && "；进程清理或输出收集尚未确认完成"}</p>
    {result.stdout && <div><span>正常输出（stdout）</span><pre>{result.stdout}</pre></div>}
    {result.stderr && <div><span>错误流（stderr，也可能包含提示或警告）</span><pre>{result.stderr}</pre></div>}
    {!result.stderr && item.status === "error" && <p>未捕获错误输出。</p>}
    {item.output && item.output !== combined && <details>
      <summary>完整工具结果</summary><pre>{item.output}</pre>
    </details>}
  </>;
}
