import type { McpServerSummary } from './mcp.js'

export type McpStatusSummary = {
  total: number
  connected: number
  connecting: number
  error: number
  toolCount: number
}

export function summarizeMcpServers(
  mcpServers: McpServerSummary[],
): McpStatusSummary {
  return mcpServers.reduce<McpStatusSummary>(
    (summary, server) => {
      summary.total += 1
      summary.toolCount += server.toolCount
      if (server.status === 'connected') {
        summary.connected += 1
      } else if (server.status === 'connecting') {
        summary.connecting += 1
      } else if (server.status === 'error') {
        summary.error += 1
      }
      return summary
    },
    {
      total: 0,
      connected: 0,
      connecting: 0,
      error: 0,
      toolCount: 0,
    },
  )
}
