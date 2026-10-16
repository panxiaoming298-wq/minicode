export interface FileReference {
  path: string;
  line?: number;
  column?: number;
}

export interface FileNavigationRequest extends FileReference {
  revision: number;
}

const inlineExtensions = new Set([
  "py", "pyi", "js", "jsx", "ts", "tsx", "mjs", "cjs", "vue", "svelte",
  "css", "scss", "sass", "less", "html", "htm", "json", "jsonc", "yaml", "yml",
  "toml", "ini", "cfg", "conf", "md", "mdx", "txt", "rst", "sql", "sh", "ps1",
  "bat", "cmd", "go", "rs", "java", "kt", "kts", "c", "h", "cpp", "hpp",
  "cs", "fs", "rb", "php", "swift", "dart", "xml", "svg", "tex", "ipynb",
]);

function positiveInteger(value: string | undefined): number | undefined {
  if (!value) return undefined;
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : undefined;
}

/** Recognize file citations without treating URLs, commands or code symbols as files. */
export function parseFileReference(value: string, inline = false): FileReference | null {
  let text = value.trim();
  if (!text || /[\u0000-\u001f\u007f]/.test(text)) return null;
  try { text = decodeURIComponent(text); }
  catch { return null; }
  if (/[\u0000-\u001f\u007f]/.test(text) || text.startsWith("//")) return null;
  const location = text.match(/(?::([0-9]+)(?::([0-9]+))?|#L([0-9]+)(?:C([0-9]+))?)$/);
  const lineText = location?.[1] || location?.[3];
  const columnText = location?.[2] || location?.[4];
  const line = positiveInteger(lineText);
  const column = positiveInteger(columnText);
  if (location && (!line || columnText && !column)) return null;
  const path = (location ? text.slice(0, location.index) : text).replace(/\\/g, "/");
  if (/^[a-z][a-z\d+.-]*:/i.test(path) && !/^[a-z]:\//i.test(path)) return null;
  if (!path || /[?#<>|"\u0000-\u001f]/.test(path) || path.endsWith("/")) return null;
  if (path.replace(/^[a-z]:/i, "").includes(":")) return null;
  // Preserve web app routes and downloads as ordinary hyperlinks.
  if (path.startsWith("/api/")) return null;
  const name = path.split("/").at(-1) || "";
  const extension = name.match(/\.([a-z][a-z\d_-]*)$/i)?.[1]?.toLowerCase();
  const namedFile = /^(?:Dockerfile|Makefile|README|LICENSE|NOTICE|Procfile|\.env(?:\.[\w-]+)?|\.[\w-]+)$/i.test(name);
  if (!extension && !namedFile) return null;
  if (inline) {
    if (extension && !namedFile && !inlineExtensions.has(extension) && !path.includes("/")) return null;
    if (!path.includes("/") && /\s/.test(path)) return null;
    // Shell expressions are never navigation targets, even if they contain a path.
    if (/[`$;=&]/.test(path) || /^(?:[\w.-]+[ \t]+)+(?:\.\/|[a-z]:\/|\/|[\w.-]+\/)/i.test(path)) return null;
  }
  return { path, ...(line ? { line } : {}), ...(column ? { column } : {}) };
}

function normalizedPath(value: string): string {
  return value.replace(/\\/g, "/").replace(/\/\.\//g, "/").replace(/\/+$/, "");
}

/** Produce a relative member path for the existing workspace-scoped file API. */
export function workspaceRelativeFile(path: string, workspace: string): string | null {
  const normalized = normalizedPath(path);
  const root = normalizedPath(workspace);
  if (!workspace.trim()) return null;
  const absolute = /^(?:[a-z]:\/|\/)/i.test(normalized);
  let relative = normalized;
  if (absolute) {
    const windows = /^(?:[a-z]:|\/\/)/i.test(root);
    const prefix = `${root}/`;
    const matches = windows ? normalized.toLowerCase().startsWith(prefix.toLowerCase()) : normalized.startsWith(prefix);
    if (!matches) return null;
    relative = normalized.slice(prefix.length);
  }
  relative = relative.replace(/^(?:\.\/)+/, "");
  if (!relative || relative.split("/").some(part => !part || part === "..") || relative.includes(":")) return null;
  return relative;
}

export function fileReferenceTitle(reference: FileReference): string {
  return `${reference.path}${reference.line ? `:${reference.line}${reference.column ? `:${reference.column}` : ""}` : ""}`;
}

export function fileReferenceKind(path: string): "code" | "json" | "text" | "image" {
  const extension = path.split(".").at(-1)?.toLowerCase();
  if (["json", "jsonc", "ipynb"].includes(extension || "")) return "json";
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(extension || "")) return "image";
  if (inlineExtensions.has(extension || "") && !["md", "mdx", "txt", "rst"].includes(extension || "")) return "code";
  return "text";
}
