import { readFile } from 'node:fs/promises'
import { z } from 'zod'
import { applyReviewedFileChange } from '../file-review.js'
import type { ToolDefinition } from '../tool.js'
import { resolveToolPath } from '../workspace.js'

type Input = {
  path: string
  search: string
  replace: string
  replaceAll?: boolean
}

export const editFileTool: ToolDefinition<Input> = {
  name: 'edit_file',
  description: 'Edit a text file by replacing exact text.',
  inputSchema: {
    type: 'object',
    properties: {
      path: { type: 'string' },
      search: { type: 'string' },
      replace: { type: 'string' },
      replaceAll: { type: 'boolean' },
    },
    required: ['path', 'search', 'replace'],
  },
  schema: z.object({
    path: z.string().min(1),
    search: z.string().min(1),
    replace: z.string(),
    replaceAll: z.boolean().optional(),
  }),
  async run(input, context) {
    const target = await resolveToolPath(context, input.path, 'write')
    const original = await readFile(target, 'utf8')

    if (!original.includes(input.search)) {
      return {
        ok: false,
        output: `Text not found in ${input.path}`,
      }
    }

    const next = input.replaceAll
      ? original.split(input.search).join(input.replace)
      : original.replace(input.search, input.replace)

    return applyReviewedFileChange(context, input.path, target, next)
  },
}
