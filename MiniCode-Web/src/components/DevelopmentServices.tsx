import { ExternalLink, FileText, LoaderCircle, RotateCw, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { serviceStatusLabels } from "../lib/developmentServices";
import type { DevelopmentService, DevelopmentServiceLogs } from "../types";
import "./DevelopmentServices.css";

export function DevelopmentServices({ workspace, sessionId, refreshKey }: {
  workspace?: string; sessionId?: string; refreshKey: string;
}) {
  const [services, setServices] = useState<DevelopmentService[]>([]);
  const [error, setError] = useState("");
  const [operationError, setOperationError] = useState("");
  const [busy, setBusy] = useState("");
  const [openedLog, setOpenedLog] = useState("");
  const [logs, setLogs] = useState<DevelopmentServiceLogs | null>(null);
  const [logError, setLogError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  useEffect(() => {
    if (!workspace && !sessionId) return;
    const controller = new AbortController();
    let fetching = false;
    async function refresh() {
      if (fetching || controller.signal.aborted) return;
      fetching = true;
      try {
        const result = await api.developmentServices(workspace, sessionId, controller.signal);
        if (!controller.signal.aborted) { setServices(result); setError(""); }
      } catch (value) {
        if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "无法读取开发服务。");
      } finally { fetching = false; }
      if (openedLog && !controller.signal.aborted) {
        try {
          const result = await api.developmentServiceLogs(openedLog, controller.signal);
          if (!controller.signal.aborted) { setLogs(result); setLogError(""); }
        } catch (value) {
          if (!controller.signal.aborted) setLogError(value instanceof Error ? value.message : "无法读取服务日志。");
        }
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [workspace, sessionId, refreshKey, openedLog]);

  async function control(service: DevelopmentService, action: "stop" | "restart") {
    setBusy(service.serviceId); setOperationError("");
    try {
      const result = await (action === "stop" ? api.stopDevelopmentService(service.serviceId) : api.restartDevelopmentService(service.serviceId));
      if (mounted.current) setServices(current => current.map(item => item.serviceId === result.serviceId ? result : item));
    } catch (value) {
      if (mounted.current) setOperationError(value instanceof Error ? value.message : "服务操作失败，请重新读取状态。");
    } finally { if (mounted.current) setBusy(""); }
  }

  if (!services.length && !error) return null;
  return <section className="development-services" aria-label="开发服务">
    <div className="development-services-heading"><strong>开发服务</strong><span>回复结束后继续运行</span></div>
    {error && <p className="development-service-error" role="alert">{error}</p>}
    {operationError && <p className="development-service-error" role="alert">{operationError}</p>}
    {services.map(service => {
      const loading = busy === service.serviceId;
      const active = service.status === "running" || service.status === "starting";
      const title = service.cwd.split(/[\\/]/).filter(Boolean).at(-1) || "本地项目";
      return <article className={`development-service is-${service.status}`} key={service.serviceId} data-service-id={service.serviceId}>
        <header><strong>{title}</strong><span className="development-service-status" role="status">
          {(service.status === "starting" || service.status === "stopping" || loading) && <LoaderCircle size={13} className="spin" aria-hidden="true" />}
          {serviceStatusLabels[service.status]}</span></header>
        <div className="development-service-address">{service.status === "running"
          ? <a href={service.url} target="_blank" rel="noreferrer">{service.url}<ExternalLink size={13} aria-hidden="true" /></a>
          : <span>{service.url}</span>}</div>
        {service.error && <p className="development-service-error">{service.error}</p>}
        <div className="development-service-actions">
          <button type="button" aria-expanded={openedLog === service.serviceId} onClick={() => {
            setOpenedLog(current => current === service.serviceId ? "" : service.serviceId); setLogs(null); setLogError("");
          }}><FileText size={14} aria-hidden="true" />查看日志</button>
          <button type="button" disabled={Boolean(busy) || service.status === "starting" || service.status === "stopping"}
            onClick={() => void control(service, "restart")}><RotateCw size={14} aria-hidden="true" />重启</button>
          <button type="button" disabled={Boolean(busy) || !active} onClick={() => void control(service, "stop")}>
            <Square size={13} aria-hidden="true" />停止服务</button>
        </div>
        {openedLog === service.serviceId && <div className="development-service-log" aria-label="服务日志">
          <div className="development-service-command"><code>{service.command.join(" ")}</code><span>{service.cwd}</span></div>
          {logError ? <p role="alert" className="development-service-error">{logError}</p>
            : <pre>{logs?.service.serviceId === service.serviceId ? logs.output || "暂时没有日志。" : "正在读取日志…"}</pre>}
          {logs?.truncated && <p>仅显示最近的日志。</p>}
        </div>}
      </article>;
    })}
    <p className="development-services-note">切换对话或关闭页面后继续运行；停止服务或关闭 MiniCode 本机服务时结束。</p>
  </section>;
}
