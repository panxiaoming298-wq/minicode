import { ArrowLeft, Folder, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { InstructionLayerView, WorkspaceFileContent } from "../types";
import { FilePreview } from "./FilePreview";

export function RuleFilePreview({ sessionId, rule, onBack, onBrowseProject }: {
  sessionId: string;
  rule: InstructionLayerView;
  onBack: () => void;
  onBrowseProject?: () => void;
}) {
  const [file, setFile] = useState<WorkspaceFileContent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setFile(null);
    void api.contextRule(sessionId, rule.name, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setFile({ path: value.path, name: value.path.split(/[\\/]/).pop() || rule.name, content: value.content,
        encoding: "UTF-8", size: new TextEncoder().encode(value.content).length,
        lineCount: value.content ? value.content.split("\n").length : 0, truncated: false });
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "无法读取规则文件。");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [sessionId, rule.name, revision]);

  return <div className="inspector-body files-panel rule-file-panel">
    <div className="rule-file-toolbar">
      <button type="button" className="button button-secondary" onClick={onBack}><ArrowLeft size={13} />返回规则与偏好</button>
      {onBrowseProject && <button type="button" className="icon-button" onClick={onBrowseProject} aria-label="浏览项目文件" title="浏览项目文件"><Folder size={14} /></button>}
      <button type="button" className="icon-button" disabled={loading} onClick={() => setRevision(value => value + 1)} aria-label="刷新规则文件" title="重新读取当前文件"><RefreshCw size={14} /></button>
    </div>
    <p className="rule-file-origin">{rule.scope === "global" ? "全局规则文件" : "项目规则文件"}</p>
    {error && <p className="knowledge-path rule-file-error-path">{rule.path}</p>}
    <div className="source-pane">
      <FilePreview file={file} workspace="" loading={loading} error={error} location={null} allowPreview={false} />
    </div>
  </div>;
}
