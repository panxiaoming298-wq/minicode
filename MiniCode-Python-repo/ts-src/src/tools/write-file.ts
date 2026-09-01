import { z } from 'zod'
import { applyReviewedFileChange } from '../file-review.js'
import type { ToolDefinition } from '../tool.js'
import { resolveToolPath } from '../workspace.js'

type Input = {
  path: string
  content: string
}

export const writeFileTool: ToolDefinition<Input> = {
  name: 'write_file',
  description: 'Write a UTF-8 text file relative to the workspace root.',
  inputSchema: {
    type: 'object',
    properties: {
      path: { type: 'string' },
      content: { type: 'string' },
    },
    required: ['path', 'content'],
  },
  schema: z.object({
    path: z.string().min(1),
    content: z.string(),
  }),
  async run(input, context) {
    const target = await resolveToolPath(context, input.path, 'write')
    return applyReviewedFileChange(context, input.path, target, input.content)
  },
}
