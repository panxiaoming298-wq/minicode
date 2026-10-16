import { Blocks, Bot, Images, Search, Settings, SlidersHorizontal, UserRound, Waypoints, Wrench, X } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";

import "./SettingsDialog.css";

export type SettingsSection = "account" | "general" | "models" | "search" | "agents" | "storage" | "tools" | "extensions";

const sections = [
  { id: "account", label: "账户与余额", description: "查看当前模型连接与账户余额", icon: UserRound },
  { id: "general", label: "通用设置", description: "管理任务预算、联网权限与模型计价", icon: Settings },
  { id: "models", label: "模型", description: "配置模型连接、密钥与输出偏好", icon: Waypoints },
  { id: "search", label: "联网搜索", description: "选择搜索来源，管理 Brave 与 Tavily 的 API 密钥", icon: Search },
  { id: "agents", label: "Agent 协作", description: "选择 Ultra 的任务分派与团队协作方式", icon: Bot },
  { id: "storage", label: "图片存储", description: "查看和管理聊天保存的图片副本", icon: Images },
  { id: "tools", label: "工具", description: "查看内置工具、扩展工具与 MCP 连接", icon: Wrench },
  { id: "extensions", label: "扩展与能力", description: "管理技能、扩展包与外部工具", icon: Blocks },
] as const;

interface SettingsDialogProps {
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
  onClose: () => void;
  children: ReactNode;
}

export function SettingsDialog({ section, onSectionChange, onClose, children }: SettingsDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const backdropPress = useRef(false);
  const current = sections.find(item => item.id === section)!;

  useEffect(() => {
    const dialog = dialogRef.current!;
    const previous = document.activeElement;
    dialog.showModal();
    dialog.querySelector<HTMLButtonElement>("[aria-current='page']")?.focus({ preventScroll: true });
    return () => {
      dialog.close();
      if (previous instanceof HTMLElement && previous.isConnected && previous.getClientRects().length
        && !previous.closest("[aria-hidden='true'], [inert]")) {
        previous.focus({ preventScroll: true });
      } else {
        document.querySelector<HTMLElement>(".composer textarea, .workspace-page-back")?.focus({ preventScroll: true });
      }
    };
  }, []);

  useEffect(() => { if (contentRef.current) contentRef.current.scrollTop = 0; }, [section]);

  return (
    <dialog ref={dialogRef} className="settings-dialog" aria-labelledby="settings-dialog-title"
      onCancel={event => {
        if (event.target !== event.currentTarget) return;
        event.preventDefault();
        onClose();
      }}
      onPointerDown={event => {
        const bounds = event.currentTarget.getBoundingClientRect();
        backdropPress.current = event.target === event.currentTarget &&
          (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom);
      }}
      onClick={event => {
        if (backdropPress.current && event.target === event.currentTarget) onClose();
        backdropPress.current = false;
      }}>
      <header className="settings-dialog-header">
        <div><h1 id="settings-dialog-title">设置</h1><span>MiniCode</span></div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="关闭设置" title="关闭设置"><X size={21} /></button>
      </header>
      <div className="settings-dialog-body">
        <aside className="settings-dialog-sidebar">
          <nav aria-label="设置分类">
            {sections.map(({ id, label, icon: Icon }) => (
              <button key={id} type="button" className={`settings-category ${section === id ? "is-selected" : ""}`}
                aria-current={section === id ? "page" : undefined} onClick={() => onSectionChange(id)}>
                <Icon size={19} strokeWidth={1.8} aria-hidden="true" /><span>{label}</span>
              </button>
            ))}
          </nav>
          <div className="settings-dialog-local"><SlidersHorizontal size={14} /><span>本机偏好设置</span></div>
        </aside>
        <section className={`settings-dialog-panel ${section === "extensions" ? "is-extensions" : ""}`} aria-labelledby="settings-category-title">
          <header className="settings-dialog-panel-heading"><h2 id="settings-category-title">{current.label}</h2><p>{current.description}</p></header>
          <div className="settings-dialog-content" ref={contentRef}>
            {children}
          </div>
        </section>
      </div>
    </dialog>
  );
}
