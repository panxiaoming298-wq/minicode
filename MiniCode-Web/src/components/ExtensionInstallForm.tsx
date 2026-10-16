import { Download, LoaderCircle, Package, Server, Shield, Sparkles, X } from "lucide-react";
import { useRef, useEffect, useState } from "react";
import { api } from "../api/client";
import type { ConversationScope, ExtensionMutationResponse } from "../types";

export type ExtensionImportKind = "package" | "skill" | "mcp";
const descriptions = {
  package: "选择扩展包目录、ZIP 或 Git 仓库。一个扩展包可以提供多种能力。",
  skill: "选择包含 SKILL.md 的目录、ZIP 或 Git 仓库。导入的技能会保存为扩展包，由所属扩展统一管理信任与权限。",
  mcp: "添加远程 MCP 地址，或配置本机 MCP 服务的启动命令。",
};
const labels = { package: "扩展包", skill: "技能", mcp: "MCP" };
const optionLabels = { package: "导入扩展包", skill: "导入技能", mcp: "添加 MCP" };

interface Props {
  workspace: string;
  contextScope: ConversationScope;
  destinationLabel: string;
  destinationRoot?: string;
  kind: ExtensionImportKind;
  onKindChange: (kind: ExtensionImportKind) => void;
  onCancel: () => void;
  onBusy: (busy: boolean) => void;
  onInstalled: (result: ExtensionMutationResponse, kind: ExtensionImportKind) => void;
}

export function ExtensionInstallForm({ workspace, contextScope, destinationLabel, destinationRoot, kind, onKindChange, onCancel, onBusy, onInstalled }: Props) {
  const [source, setSource] = useState("");
  const [revision, setRevision] = useState("");
  const [transport, setTransport] = useState<"streamable-http" | "stdio">("streamable-http");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const ready = Boolean(destinationRoot) && (kind === "mcp"
    ? Boolean(name.trim() && (transport === "stdio" ? command.trim() : url.trim())) : Boolean(source.trim()));

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true); onBusy(true); setError("");
    const target = { workspace, contextScope, scope: contextScope === "project" ? "project" as const : "global" as const };
    try {
      const result = kind === "mcp" ? await api.installMcpExtension({ ...target, name: name.trim(), transport,
        ...(transport === "streamable-http" ? {url: url.trim()} : {command: command.trim(), args: args.split(/\r?\n/).map(value => value.trim()).filter(Boolean)}) })
        : await api.installExtension({ ...target, source: source.trim(), trusted: false, ...(revision.trim() ? {revision: revision.trim()} : {}) });
      if (mounted.current) onInstalled(result, kind);
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "导入失败，请检查来源后重试。");
    } finally {
      if (mounted.current) { setBusy(false); onBusy(false); }
    }
  }

  return <form className="extension-install capability-import-form" aria-label={kind === "skill" ? "添加技能" : "添加扩展"} onSubmit={event => void submit(event)}>
    <header className="capability-import-heading"><div><h2>{kind === "skill" ? "添加技能" : "添加扩展"}</h2><p>选择添加方式并确认保存位置。</p></div>
      <button type="button" className="icon-button" aria-label="取消导入" onClick={onCancel} disabled={busy}><X size={18} /></button></header>
    <div className="extension-import-tabs" role="group" aria-label="导入类型">
      {(["package", "skill", "mcp"] as const).map(value => <button type="button" key={value} aria-pressed={kind === value}
        disabled={busy} className={kind === value ? "is-active" : ""} onClick={() => { onKindChange(value); setError(""); }}>
        {value === "skill" ? <Sparkles size={15} /> : value === "mcp" ? <Server size={15} /> : <Package size={15} />}<span>{optionLabels[value]}</span></button>)}
    </div>
    <p className="capability-import-description">{descriptions[kind]}</p>
    <div className="capability-install-target" aria-label="安装目标"><span>保存到</span><strong>{destinationLabel}</strong>
      <p>取消添加后可切换顶部查看范围。</p><details className="capability-install-location"><summary>查看保存目录</summary><code>{destinationRoot || "正在读取安装位置…"}</code></details></div>
    {kind === "mcp" ? <>
      <label><span>MCP 名称</span><input value={name} onChange={event => setName(event.target.value)} placeholder="例如 团队文档" required disabled={busy} /></label>
      <fieldset className="capability-mcp-transport"><legend>连接方式</legend>
        <label><input type="radio" name="mcp-transport" checked={transport === "streamable-http"} onChange={() => setTransport("streamable-http")} disabled={busy} />远程 HTTP</label>
        <label><input type="radio" name="mcp-transport" checked={transport === "stdio"} onChange={() => setTransport("stdio")} disabled={busy} />本机命令</label>
      </fieldset>
      {transport === "streamable-http" ? <label><span>MCP 地址</span><input type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://example.com/mcp" required disabled={busy} /></label> : <>
        <label><span>启动命令</span><input value={command} onChange={event => setCommand(event.target.value)} placeholder="例如 uvx 或 npx" required disabled={busy} /></label>
        <label><span>命令参数，每行一个</span><textarea value={args} onChange={event => setArgs(event.target.value)} rows={4} disabled={busy} /></label>
      </>}
    </> : <>
      <label><span>{labels[kind]} 来源</span><input value={source} onChange={event => setSource(event.target.value)}
        placeholder={kind === "skill" ? "SKILL.md 或所在目录、ZIP 路径、Git URL" : "扩展包目录、ZIP 路径或 Git URL"} required disabled={busy} /></label>
      <details className="capability-import-advanced"><summary>Git 高级选项{revision.trim() ? " · 已设置版本" : ""}</summary>
        <label><span>分支、标签或提交（可选）</span><input value={revision} onChange={event => setRevision(event.target.value)} placeholder="留空使用仓库默认版本" disabled={busy} /></label>
      </details>
    </>}
    {error && <p className="capability-form-error" role="alert">{error}</p>}
    <footer className="capability-import-footer"><p><Shield size={15} />{kind === "mcp" ? "添加后检查信任和所需权限。" : "导入后保持未信任，可先预览内容。"}</p>
      <div><button type="button" className="button button-secondary" onClick={onCancel} disabled={busy}>取消</button>
        <button type="submit" className="button button-primary" disabled={!ready || busy}>{busy ? <LoaderCircle size={15} className="spin" /> : <Download size={15} />}
          {busy ? kind === "mcp" ? "正在添加…" : "正在导入…" : optionLabels[kind]}</button></div></footer>
  </form>;
}
