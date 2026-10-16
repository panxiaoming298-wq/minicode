import type { CapabilitySnapshot, ExtensionCatalog, ExtensionScope, ToolCapability } from "../types";
import { capabilitySourceLabel, matchesToolQuery, toolSource } from "./capabilityText";

export type ToolCatalogSource = "builtin" | "extension" | "mcp" | "other";

export interface ToolCatalogEntry extends ToolCapability {
  id: string;
  kind: ToolCatalogSource;
  origin: string;
  status: string;
  declaredName?: string;
  scope?: ExtensionScope;
  inputFields?: string[];
  requiredInputs?: string[];
  destructive?: boolean;
  activation?: string;
}

function kindOf(tool: ToolCapability): ToolCatalogSource {
  const source = toolSource(tool);
  if (["core", "builtin", "skill"].includes(source)) return "builtin";
  if (source === "mcp" || source === "extension") return source;
  return "other";
}

export function buildToolCatalog(capabilities: CapabilitySnapshot | null, extensions: ExtensionCatalog | null): ToolCatalogEntry[] {
  const registered = capabilities?.tools || [];
  const inventory = new Map((capabilities?.toolCatalog || []).map(tool => [tool.name, tool]));
  for (const tool of registered) inventory.set(tool.name, { ...inventory.get(tool.name), ...tool, availability: "registered", activation: "" });
  const declarations = extensions?.tools || [];
  const declaredNames = new Set(declarations.map(tool => tool.runtimeName));
  const registeredNames = new Set(registered.map(tool => tool.name));
  const rows: ToolCatalogEntry[] = [...inventory.values()].filter(tool => !declaredNames.has(tool.name)).map(tool => ({
    ...tool, source: toolSource(tool), id: tool.name, kind: kindOf(tool), status: tool.availability === "conditional" ? "conditional" : "ready",
    origin: toolSource(tool) === "mcp" ? tool.name.split("__")[1] || "MCP 服务" : capabilitySourceLabel(toolSource(tool)),
  }));

  for (const tool of declarations) {
    const owner = extensions?.extensions.find(extension => extension.id === tool.extensionId && extension.scope === tool.scope);
    rows.push({
      id: `${tool.scope}:${tool.extensionId}:${tool.runtimeName || tool.name}`,
      name: tool.runtimeName || tool.name, declaredName: tool.name,
      description: tool.description, effect: tool.effect, replayPolicy: tool.replayPolicy,
      source: "extension", kind: "extension", origin: owner?.name || tool.extensionId,
      status: tool.availability === "ready" && !registeredNames.has(tool.runtimeName) ? "declared" : tool.availability || "declared",
      scope: tool.scope, inputFields: tool.inputFields, requiredInputs: tool.requiredInputs, destructive: tool.destructive,
    });
  }

  const order: Record<ToolCatalogSource, number> = { builtin: 0, extension: 1, mcp: 2, other: 3 };
  return rows.sort((left, right) => order[left.kind] - order[right.kind] || left.name.localeCompare(right.name));
}

export function matchesCatalogQuery(tool: ToolCatalogEntry, query: string): boolean {
  return matchesToolQuery(tool, query) || [tool.origin, tool.declaredName, tool.activation, tool.scope === "project" ? "本项目" : tool.scope === "global" ? "全局" : ""]
    .join(" ").toLowerCase().includes(query.trim().toLowerCase());
}
