import { z } from 'zod'
import type { ToolDefinition } from '../tool.js'
import { loadSkill } from '../skills.js'

type Input = {
  name: string
}

export function createLoadSkillTool(cwd: string): ToolDefinition<Input> {
  return {
    name: 'load_skill',
    description:
      'Load the full contents of a named SKILL.md file so you can follow that workflow accurately.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string' },
      },
      required: ['name'],
    },
    schema: z.object({
      name: z.string().min(1),
    }),
    async run(input) {
      const skill = await loadSkill(cwd, input.name)
      if (!skill) {
        return {
          ok: false,
          output: `Unknown skill: ${input.name}`,
        }
      }

      return {
        ok: true,
        output: [
          `SKILL: ${skill.name}`,
          `SOURCE: ${skill.source}`,
          `PATH: ${skill.path}`,
          '',
          skill.content,
        ].join('\n'),
      }
    },
  }
}
