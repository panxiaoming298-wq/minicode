import type { DevelopmentService, DevelopmentServiceLogs } from "../types";

const states = new Set(["starting", "running", "stopping", "stopped", "failed"]);
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value);

export function parseDevelopmentService(value: unknown): DevelopmentService {
  let localUrl = false;
  if (object(value) && typeof value.url === "string") {
    try {
      const url = new URL(value.url);
      localUrl = ["http:", "https:"].includes(url.protocol) && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
        && !url.username && !url.password;
    } catch { /* Invalid URLs are rejected below. */ }
  }
  if (!object(value) || typeof value.serviceId !== "string" || !/^svc_[0-9a-f]{32}$/.test(value.serviceId)
    || !Array.isArray(value.command) || !value.command.length || !value.command.every(item => typeof item === "string")
    || typeof value.cwd !== "string" || typeof value.workspace !== "string" || !localUrl
    || !Array.isArray(value.sessionIds) || !value.sessionIds.every(item => typeof item === "string")
    || typeof value.status !== "string" || !states.has(value.status)
    || typeof value.error !== "string" || typeof value.startedAt !== "number"
    || (value.pid !== null && (!Number.isSafeInteger(value.pid) || (value.pid as number) <= 0))
    || (value.readyAt !== null && typeof value.readyAt !== "number")
    || (value.exitCode !== null && !Number.isInteger(value.exitCode))) {
    throw new Error("开发服务响应格式不兼容，请重启 MiniCode 后刷新页面。");
  }
  return value as unknown as DevelopmentService;
}

export function parseDevelopmentServices(value: unknown): DevelopmentService[] {
  if (!object(value) || !Array.isArray(value.services)) throw new Error("开发服务列表格式不兼容。");
  return value.services.map(parseDevelopmentService);
}

export function parseDevelopmentServiceLogs(value: unknown): DevelopmentServiceLogs {
  if (!object(value) || typeof value.output !== "string" || typeof value.truncated !== "boolean") {
    throw new Error("开发服务日志格式不兼容。");
  }
  return { service: parseDevelopmentService(value.service), output: value.output, truncated: value.truncated };
}

export const serviceStatusLabels: Record<DevelopmentService["status"], string> = {
  starting: "正在启动", running: "运行中", stopping: "正在停止", stopped: "已停止", failed: "失败",
};
