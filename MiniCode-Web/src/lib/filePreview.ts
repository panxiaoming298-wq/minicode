import { parseFileReference, workspaceRelativeFile } from "./fileReferences";
import type { FileReference } from "./fileReferences";

export function isMarkdownFile(path: string): boolean {
  return /\.(?:md|markdown|mdown)$/i.test(path);
}

/** Resolve document links relative to the document, keeping them inside its workspace. */
export function previewFileReference(url: string, documentPath: string, workspace: string): FileReference | null {
  const reference = parseFileReference(url);
  if (!reference) return null;
  if (/^(?:[a-z]:\/|\/)/i.test(reference.path)) {
    const path = workspaceRelativeFile(reference.path, workspace);
    return path ? { ...reference, path } : null;
  }
  const parts = documentPath.replace(/\\/g, "/").split("/").slice(0, -1);
  for (const part of reference.path.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!parts.length) return null;
      parts.pop();
    } else parts.push(part);
  }
  const path = workspaceRelativeFile(parts.join("/"), workspace);
  return path ? { ...reference, path } : null;
}
