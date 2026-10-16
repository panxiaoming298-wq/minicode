import { ArrowLeft, PanelLeftOpen, type LucideIcon } from "lucide-react";
import { useEffect, useRef, type ReactNode, type RefObject } from "react";

import "./WorkspacePage.css";

export interface WorkspacePageNavigation {
  onBack: () => void;
  onOpenRail: () => void;
  railCollapsed: boolean;
}

interface WorkspacePageProps extends WorkspacePageNavigation {
  title: string;
  description: string;
  icon: LucideIcon;
  headingId: string;
  headingRef?: RefObject<HTMLHeadingElement | null>;
  className?: string;
  embedded?: boolean;
  children: ReactNode;
}

export function WorkspacePage({ title, description, icon: Icon, headingId, headingRef,
  className = "", embedded = false, onBack, onOpenRail, railCollapsed, children }: WorkspacePageProps) {
  const localHeadingRef = useRef<HTMLHeadingElement>(null);
  const titleRef = headingRef || localHeadingRef;

  useEffect(() => { if (!embedded) titleRef.current?.focus(); }, [titleRef, embedded]);

  if (embedded) return (
    <section className={`workspace-page is-embedded ${className}`} aria-label={title}>
      <div className="workspace-page-content">{children}</div>
    </section>
  );

  return (
    <main id="main-content" className={`workspace-page ${className}`} aria-labelledby={headingId} tabIndex={-1}>
      <header className="workspace-page-topbar">
        <button className={`icon-button workspace-page-rail-toggle ${railCollapsed ? "is-collapsed" : ""}`}
          onClick={onOpenRail} aria-label="打开任务栏" title="打开任务栏"><PanelLeftOpen size={18} /></button>
        <button className="workspace-page-back" onClick={onBack}><ArrowLeft size={16} />返回对话</button>
      </header>
      <div className="workspace-page-content">
        <div className="workspace-page-heading">
          <div className="workspace-page-icon"><Icon size={22} aria-hidden="true" /></div>
          <h1 id={headingId} ref={titleRef} tabIndex={-1}>{title}</h1>
          <p>{description}</p>
        </div>
        {children}
      </div>
    </main>
  );
}
