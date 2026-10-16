import { Archive, CircleAlert } from "lucide-react";
import type { TaskMemoryView } from "../types";
import "./RuntimeMemory.css";

export function RuntimeMemory({ receipt }: { receipt?: TaskMemoryView | null }) {
  const warning = receipt?.status === "failed" || receipt?.status === "invalid";
  return (
    <section className="inspector-section task-memory-receipt" aria-label="任务历史记忆">
      <div className="section-heading">
        <h2>{warning ? <CircleAlert size={15} /> : <Archive size={15} />}任务历史记忆</h2>
        <span>{receipt ? ({ stored: "已保存", already_recorded: "已存在", skipped: "已跳过", failed: "保存失败", invalid: "回执无效" }[receipt.status]) : "未报告"}</span>
      </div>
      <p className="section-note"><strong>{receipt?.label || "本轮没有任务历史写入回执"}</strong></p>
      <p className="section-note">{receipt?.detail || "旧版或其他记忆路径可能没有这项回执；不能据此判断所有记忆功能是否生效。"}</p>
      {receipt?.entryId && <details>
        <summary>查看历史记录来源</summary>
        <dl className="compact-metrics">
          <div><dt>作用域</dt><dd>当前工作区 local</dd></div>
          <div><dt>运行 ID</dt><dd><code>{receipt.runId}</code></dd></div>
        </dl>
        <p className="section-note">记录 ID：<code>{receipt.entryId}</code></p>
        <p className="section-note">证据摘要：<code>{receipt.evidenceSha256}</code></p>
      </details>}
    </section>
  );
}
