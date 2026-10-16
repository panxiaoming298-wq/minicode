import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { TaskFilePreview, TaskFileSelection } from "../types";

export interface TaskFileDraft {
  context: string; revision: number; path: string; preview: TaskFilePreview | null;
  trusted: boolean; busy: boolean; error: string;
}

export function emptyTaskFile(context: string, revision = 0): TaskFileDraft {
  return { context, revision, path: "", preview: null, trusted: false, busy: false, error: "" };
}

export function taskFileSelection(draft: TaskFileDraft): TaskFileSelection | undefined {
  if (!draft.path.trim()) return undefined;
  if (!draft.preview || !draft.trusted || draft.busy || draft.error) throw new Error("请先预览任务契约并确认信任验收命令，或移除契约。");
  return { path: draft.preview.path, sha256: draft.preview.sha256, commandsTrusted: true };
}

export function useTaskFile(context: string, workspace: string, sessionId: string | null) {
  const [saved, setSaved] = useState(() => emptyTaskFile(context));
  const sequence = useRef(0);
  useEffect(() => { setSaved(current => current.context === context ? current : emptyTaskFile(context, ++sequence.current)); }, [context]);
  const draft = saved.context === context ? saved : emptyTaskFile(context);
  return {
    draft,
    blocked: Boolean(draft.path.trim() && (!draft.preview || !draft.trusted || draft.busy || draft.error)),
    setPath: (path: string) => setSaved({ ...emptyTaskFile(context, ++sequence.current), path }),
    confirm: (trusted: boolean) => setSaved({ ...draft, trusted, revision: ++sequence.current }),
    clear: () => setSaved(emptyTaskFile(context, ++sequence.current)),
    consume: (submitted: TaskFileDraft) => setSaved(current => current.context === submitted.context && current.revision === submitted.revision
      ? emptyTaskFile(current.context, ++sequence.current) : current),
    invalidate: (submitted: TaskFileDraft) => setSaved(current => current.context === submitted.context && current.revision === submitted.revision
      ? { ...current, preview: null, trusted: false, busy: false, revision: ++sequence.current } : current),
    preview: async () => {
      const revision = ++sequence.current;
      setSaved({ ...draft, revision, preview: null, trusted: false, busy: true, error: "" });
      try {
        const preview = await api.previewTaskFile({ scope: "project", workspace, ...(sessionId ? { sessionId } : {}), path: draft.path.trim() });
        setSaved(current => current.context === context && current.revision === revision ? { ...current, preview, busy: false } : current);
      } catch (error) {
        setSaved(current => current.context === context && current.revision === revision
          ? { ...current, busy: false, error: error instanceof Error ? error.message : "契约预览失败。" } : current);
      }
    },
  };
}
