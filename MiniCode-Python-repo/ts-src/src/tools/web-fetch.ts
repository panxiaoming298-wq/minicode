import { z } from 'zod'
import type { ToolDefinition } from '../tool.js'
import { fetchWebPage } from '../utils/web.js'

type Input = {
  url: string
  max_chars?: number
}

export const webFetchTool: ToolDefinition<Input> = {
  name: 'web_fetch',
  description:
    'Fetch a web page and extract its readable text content. Use this after web_search when you need the full content of a specific page.',
  inputSchema: {
    type: 'object',
    properties: {
      url: { type: 'string', description: 'HTTP or HTTPS URL to fetch.' },
      max_chars: {
        type: 'number',
        description: 'Maximum number of characters to return from the page content. Defaults to 12000.',
      },
    },
    required: ['url'],
  },
  schema: z.object({
    url: z.string().url(),
    max_chars: z.number().int().min(500).optional(),
  }),
  async run(input) {
    try {
      const maxChars = input.max_chars ?? 12000
      const result = await fetchWebPage({ url: input.url, maxChars })

      if (result.status >= 400) {
        return {
          ok: false,
          output: `HTTP ${result.status} ${result.statusText}: ${input.url}`,
        }
      }

      const lines: string[] = [
        `URL: ${result.finalUrl}`,
        `STATUS: ${result.status}`,
        `CONTENT_TYPE: ${result.contentType}`,
      ]
      if (result.title) {
        lines.push(`TITLE: ${result.title}`)
      }
      lines.push('', result.content)

      return {
        ok: true,
        output: lines.join('\n'),
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      return {
        ok: false,
        output: `Web fetch failed: ${message}`,
      }
    }
  },
}
