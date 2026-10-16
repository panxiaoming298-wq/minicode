import { Copy, LoaderCircle, RotateCcw, Square, TerminalSquare, ClipboardPaste, Eraser } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { api, terminalSocketUrl } from "../api/client";
import "@xterm/xterm/css/xterm.css";
import "./TerminalPanel.css";

type Status = "connecting" | "running" | "stopping" | "closed" | "error";

export default function TerminalPanel({ workspace, sessionId, visible }: {
  workspace: string; sessionId?: string | null; visible: boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const fitRef = useRef<(() => void) | null>(null);
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const [revision, setRevision] = useState(0);
  const [status, setStatus] = useState<Status>("connecting");
  const [directory, setDirectory] = useState(workspace);
  const [message, setMessage] = useState("");
  const [clipboardMessage, setClipboardMessage] = useState("");
  const connected = status === "running";
  const busy = status === "connecting" || status === "stopping";

  useEffect(() => {
    const host = container.current;
    if (!host) return;
    let disposed = false;
    let ready = false;
    let settled = false;
    let socket: WebSocket | null = null;
    const controller = new AbortController();
    const terminal = new Terminal({
      cursorBlink: true, fontSize: 13, lineHeight: 1.35, scrollback: 5000,
      fontFamily: getComputedStyle(host).getPropertyValue("--font-code").trim() || "Consolas, monospace",
      disableStdin: true, screenReaderMode: true,
      theme: { background: "#fdfdfe", foreground: "#25272c", cursor: "#34363c", selectionBackground: "#cdd9ed",
        black: "#25272c", red: "#a94035", green: "#3f684f", yellow: "#805215", blue: "#2563eb",
        magenta: "#8052a1", cyan: "#19787e", white: "#e9ecf1", brightBlack: "#666b75",
        brightRed: "#c2493c", brightGreen: "#39804f", brightYellow: "#9a670c", brightBlue: "#3574e4",
        brightMagenta: "#9762be", brightCyan: "#248d94", brightWhite: "#fdfdfe" },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(host);
    terminalRef.current = terminal;
    setStatus("connecting");
    setMessage("");
    setClipboardMessage("");

    function sendInput(data: string) {
      if (!ready || socket?.readyState !== WebSocket.OPEN) return;
      // Bound JSON frames, including pasted control characters and emoji.
      const characters = Array.from(data);
      for (let index = 0; index < characters.length; index += 2048) {
        socket.send(JSON.stringify({ type: "input", data: characters.slice(index, index + 2048).join("") }));
      }
    }
    const input = terminal.onData(sendInput);
    let frame = 0;
    const resize = () => {
      if (disposed || !visibleRef.current || !host.clientWidth || !host.clientHeight) return;
      const dimensions = fit.proposeDimensions();
      if (!dimensions) return;
      const cols = Math.min(400, Math.max(20, dimensions.cols));
      const rows = Math.min(200, Math.max(4, dimensions.rows));
      terminal.resize(cols, rows);
      if (ready && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols, rows }));
    };
    fitRef.current = resize;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(resize);
    });
    observer.observe(host);
    resize();
    terminal.attachCustomKeyEventHandler(event => {
      if (event.type !== "keydown") return true;
      if (event.ctrlKey && !event.altKey && event.key.toLowerCase() === "c" && (event.shiftKey || terminal.hasSelection())) {
        if (terminal.hasSelection()) void navigator.clipboard.writeText(terminal.getSelection()).catch(() => {
          if (!disposed) setClipboardMessage("复制失败，请使用工具栏复制按钮重试。");
        });
        event.preventDefault();
        return false;
      }
      return true;
    });
    const timeout = window.setTimeout(() => {
      if (disposed || ready || settled) return;
      settled = true;
      controller.abort();
      socket?.close();
      setStatus("error");
      setMessage("连接终端超时，请确认后端已重启后重试。");
    }, 15_000);

    void api.createTerminalTicket({ workspace, sessionId, cols: terminal.cols, rows: terminal.rows }, controller.signal)
      .then(receipt => {
        if (disposed || settled) return;
        setDirectory(receipt.workspace);
        socket = new WebSocket(terminalSocketUrl(receipt.ticket));
        socketRef.current = socket;
        socket.onmessage = event => {
          if (disposed || settled) return;
          try {
            const data = JSON.parse(event.data);
            if (data.type === "ready" && typeof data.workspace === "string") {
              ready = true;
              clearTimeout(timeout);
              terminal.options.disableStdin = false;
              setDirectory(data.workspace);
              setStatus("running");
              resize();
              if (visibleRef.current) terminal.focus();
            } else if (data.type === "output" && typeof data.data === "string" && Number.isSafeInteger(data.sequence)) {
              terminal.write(data.data, () => {
                if (!disposed && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ack", sequence: data.sequence }));
              });
            } else if (data.type === "closed" || data.type === "exit" || data.type === "error") {
              settled = true;
              ready = false;
              clearTimeout(timeout);
              terminal.options.disableStdin = true;
              setStatus(data.type === "error" ? "error" : "closed");
              setMessage(typeof data.message === "string" ? data.message : "终端已结束。");
            } else throw new Error("终端响应格式不兼容，请重启服务后重试。");
          } catch (error) {
            settled = true;
            ready = false;
            terminal.options.disableStdin = true;
            setStatus("error");
            setMessage(error instanceof Error ? error.message : "终端响应无法读取。");
            socket?.close();
          }
        };
        socket.onclose = () => {
          ready = false;
          clearTimeout(timeout);
          if (disposed || settled) return;
          settled = true;
          terminal.options.disableStdin = true;
          setStatus("error");
          setMessage("终端连接已断开，请重新打开；断线前的命令不会自动重发。");
        };
      }).catch(error => {
        if (disposed || settled) return;
        settled = true;
        clearTimeout(timeout);
        setStatus("error");
        setMessage(error instanceof Error ? error.message : "无法启动终端。");
      });

    return () => {
      disposed = true;
      ready = false;
      controller.abort();
      clearTimeout(timeout);
      cancelAnimationFrame(frame);
      observer.disconnect();
      input.dispose();
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "close" }));
      socket?.close();
      if (socketRef.current === socket) socketRef.current = null;
      terminalRef.current = null;
      fitRef.current = null;
      terminal.dispose();
    };
  }, [workspace, sessionId, revision]);

  useEffect(() => {
    if (!visible) return;
    const frame = requestAnimationFrame(() => {
      fitRef.current?.();
      if (socketRef.current?.readyState === WebSocket.OPEN) terminalRef.current?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [visible]);

  async function copy() {
    const selected = terminalRef.current?.getSelection();
    if (!selected) { setClipboardMessage("先在终端中选中文字，再复制。"); return; }
    try { await navigator.clipboard.writeText(selected); setClipboardMessage("已复制选中内容。"); }
    catch { setClipboardMessage("无法访问剪贴板，请使用 Ctrl+Shift+C。"); }
  }

  async function paste() {
    const terminal = terminalRef.current;
    const socket = socketRef.current;
    try {
      const text = await navigator.clipboard.readText();
      if (!terminal || socket?.readyState !== WebSocket.OPEN || !connected
        || terminalRef.current !== terminal || socketRef.current !== socket) return;
      terminal.paste(text);
      terminal.focus();
      setClipboardMessage("");
    } catch { setClipboardMessage("无法读取剪贴板，请在终端中按 Ctrl+V 粘贴。"); }
  }

  function stop() {
    const socket = socketRef.current;
    if (socket?.readyState !== WebSocket.OPEN) return;
    terminalRef.current!.options.disableStdin = true;
    setStatus("stopping");
    socket.send(JSON.stringify({ type: "close" }));
  }

  return <section className="terminal-panel" hidden={!visible} aria-label="项目终端">
    <div className="terminal-toolbar">
      <span className={`terminal-state terminal-state-${status}`} role="status">
        {busy ? <LoaderCircle size={14} className="spin" /> : <TerminalSquare size={14} />}
        {status === "connecting" ? "正在连接" : status === "running" ? "PowerShell" : status === "stopping" ? "正在结束" : status === "error" ? "连接失败" : "已结束"}
      </span>
      <div className="terminal-actions">
        <button type="button" className="icon-button" title="复制选中内容" aria-label="复制终端选中内容" onClick={() => void copy()}><Copy size={14} /></button>
        <button type="button" className="icon-button" title="粘贴（Ctrl+V）" aria-label="粘贴到终端" disabled={!connected} onClick={() => void paste()}><ClipboardPaste size={14} /></button>
        <button type="button" className="icon-button" title="清空显示" aria-label="清空终端显示" onClick={() => terminalRef.current?.clear()}><Eraser size={14} /></button>
        <button type="button" className="icon-button" title="中断当前命令（Ctrl+C）" aria-label="中断终端命令" disabled={!connected} onClick={() => {
          socketRef.current?.send(JSON.stringify({ type: "input", data: "\u0003" })); terminalRef.current?.focus();
        }}><Square size={12} /></button>
        {connected || busy ? <button type="button" className="terminal-end" disabled={!connected} onClick={stop}>结束终端</button>
          : <button type="button" className="terminal-restart" onClick={() => setRevision(value => value + 1)}><RotateCcw size={13} />重新打开</button>}
      </div>
    </div>
    <div className="terminal-directory" title={directory}>{directory}</div>
    <div className="terminal-screen" ref={container} />
    {message && <div className={`terminal-notice ${status === "error" ? "is-error" : ""}`} role={status === "error" ? "alert" : "status"}>{message}</div>}
    {clipboardMessage && <div className="terminal-notice" role="status">{clipboardMessage}</div>}
    <div className="terminal-hint">Ctrl+C 中断 · 选中文字后 Ctrl+C 复制 · Ctrl+V 粘贴</div>
  </section>;
}
