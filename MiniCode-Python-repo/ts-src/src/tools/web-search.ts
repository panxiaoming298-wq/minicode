import { z } from 'zod'
import type { ToolDefinition } from '../tool.js'
import { searchDuckDuckGoLite } from '../utils/web.js'

type Input = {
  query: string
  max_results?: number
  allowed_domains?: string[]
  blocked_domains?: string[]
}

export const webSearchTool: ToolDefinition<Input> = {
  name: 'web_search',
  description:
    'Search the public web using DuckDuckGo. Use this for current information, documentation, or anything outside the local workspace.',
  inputSchema: {
    type: 'object',
    properties: {
      query: { type: 'string', description: 'Search query.' },
      max_results: { type: 'number', description: 'Maximum number of results to return. Defaults to 5.' },
      allowed_domains: {
        type: 'array',
        items: { type: 'string' },
        description: 'Only return results from these domains.',
      },
      blocked_domains: {
        type: 'array',
        items: { type: 'string' },
        description: 'Exclude results from these domains.',
      },
    },
    required: ['query'],
  },
  schema: z
    .object({
      query: z.string().min(1),
      max_results: z.number().int().min(1).max(20).optional(),
      allowed_domains: z.array(z.string().min(1)).optional(),
      blocked_domains: z.array(z.string().min(1)).optional(),
    })
    .superRefine((value, ctx) => {
      if (
        (value.allowed_domains?.length ?? 0) > 0 &&
        (value.blocked_domains?.length ?? 0) > 0
      ) {
        ctx.addIssue({
          code: 'custom',
          message:
            'Cannot specify both allowed_domains and blocked_domains in one request.',
          path: ['allowed_domains'],
        })
      }
    }),
  async run(input) {
    try {
      const result = await searchDuckDuckGoLite({
        query: input.query,
        maxResults: input.max_results ?? 5,
        allowedDomains: input.allowed_domains,
        blockedDomains: input.blocked_domains,
      })

      if (result.organic.length === 0) {
        return {
          ok: true,
          output: 'No results found.',
        }
      }

      const lines: string[] = [`QUERY: ${input.query}`, '']
      for (const [i, item] of result.organic.entries()) {
        lines.push(`[${i + 1}] ${item.title}`)
        lines.push(`    URL: ${item.link}`)
        if (item.snippet) {
          lines.push(`    ${item.snippet}`)
        }
        lines.push('')
      }

      return {
        ok: true,
        output: lines.join('\n').trimEnd(),
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      return {
        ok: false,
        output: `Web search failed: ${message}`,
      }
    }
  },
}
