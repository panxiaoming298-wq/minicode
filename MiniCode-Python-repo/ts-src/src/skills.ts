import { mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { isEnoentError } from './utils/errors.js'

export type SkillSummary = {
  name: string
  description: string
  path: string
  source: 'project' | 'user' | 'compat_project' | 'compat_user'
}

export type LoadedSkill = SkillSummary & {
  content: string
}

type SkillSourceRoot = {
  root: string
  source: SkillSummary['source']
}

type SkillScope = 'user' | 'project'

function extractDescription(markdown: string): string {
  const normalized = markdown.replace(/\r\n/g, '\n')
  const paragraphs = normalized
    .split('\n\n')
    .map(block => block.trim())
    .filter(Boolean)

  for (const block of paragraphs) {
    if (block.startsWith('#')) {
      continue
    }

    const line = block
      .split('\n')
      .map(part => part.trim())
      .find(part => part.length > 0 && !part.startsWith('#'))

    if (line) {
      return line.replace(/`/g, '')
    }
  }

  return 'No description provided.'
}

function getSkillRoots(cwd: string): SkillSourceRoot[] {
  return [
    {
      root: path.join(cwd, '.mini-code', 'skills'),
      source: 'project',
    },
    {
      root: path.join(os.homedir(), '.mini-code', 'skills'),
      source: 'user',
    },
    {
      root: path.join(cwd, '.claude', 'skills'),
      source: 'compat_project',
    },
    {
      root: path.join(os.homedir(), '.claude', 'skills'),
      source: 'compat_user',
    },
  ]
}

function getManagedSkillRoot(scope: SkillScope, cwd: string): string {
  return scope === 'project'
    ? path.join(cwd, '.mini-code', 'skills')
    : path.join(os.homedir(), '.mini-code', 'skills')
}

async function listSkillDirs(root: SkillSourceRoot): Promise<LoadedSkill[]> {
  try {
    const entries = await readdir(root.root, { withFileTypes: true })
    const results: LoadedSkill[] = []

    for (const entry of entries) {
      if (!entry.isDirectory()) {
        continue
      }

      const skillPath = path.join(root.root, entry.name, 'SKILL.md')

      try {
        const content = await readFile(skillPath, 'utf8')
        results.push({
          name: entry.name,
          description: extractDescription(content),
          path: skillPath,
          source: root.source,
          content,
        })
      } catch {
        // Ignore malformed or missing skills.
      }
    }

    return results
  } catch {
    return []
  }
}

export async function discoverSkills(cwd: string): Promise<SkillSummary[]> {
  const byName = new Map<string, LoadedSkill>()

  for (const root of getSkillRoots(cwd)) {
    const skills = await listSkillDirs(root)
    for (const skill of skills) {
      if (!byName.has(skill.name)) {
        byName.set(skill.name, skill)
      }
    }
  }

  return [...byName.values()].map(skill => ({
    name: skill.name,
    description: skill.description,
    path: skill.path,
    source: skill.source,
  }))
}

export async function loadSkill(
  cwd: string,
  name: string,
): Promise<LoadedSkill | null> {
  const normalizedName = name.trim()
  if (!normalizedName) {
    return null
  }

  for (const root of getSkillRoots(cwd)) {
    const skillPath = path.join(root.root, normalizedName, 'SKILL.md')
    try {
      const content = await readFile(skillPath, 'utf8')
      return {
        name: normalizedName,
        description: extractDescription(content),
        path: skillPath,
        source: root.source,
        content,
      }
    } catch {
      // Keep searching lower-priority roots.
    }
  }

  return null
}

export async function installSkill(args: {
  cwd: string
  sourcePath: string
  name?: string
  scope?: SkillScope
}): Promise<{ name: string; targetPath: string }> {
  const scope = args.scope ?? 'user'
  const statPath = path.resolve(args.cwd, args.sourcePath)
  let content: string
  let inferredName: string

  try {
    const entries = await readdir(statPath, { withFileTypes: true })
    const skillFile = entries.find(entry => entry.isFile() && entry.name === 'SKILL.md')
    if (!skillFile) {
      throw new Error(`No SKILL.md found in ${statPath}`)
    }
    content = await readFile(path.join(statPath, 'SKILL.md'), 'utf8')
    inferredName = path.basename(statPath)
  } catch (error) {
    const filePath = statPath.endsWith('SKILL.md') ? statPath : path.join(statPath, 'SKILL.md')
    try {
      content = await readFile(filePath, 'utf8')
      inferredName = path.basename(path.dirname(filePath))
    } catch {
      throw error
    }
  }

  const skillName = (args.name ?? inferredName).trim()
  if (!skillName) {
    throw new Error('Skill name cannot be empty.')
  }

  const targetRoot = getManagedSkillRoot(scope, args.cwd)
  const targetDir = path.join(targetRoot, skillName)
  const targetPath = path.join(targetDir, 'SKILL.md')
  await mkdir(targetDir, { recursive: true })
  await writeFile(targetPath, content, 'utf8')

  return {
    name: skillName,
    targetPath,
  }
}

export async function removeManagedSkill(args: {
  cwd: string
  name: string
  scope?: SkillScope
}): Promise<{ removed: boolean; targetPath: string }> {
  const scope = args.scope ?? 'user'
  const targetPath = path.join(getManagedSkillRoot(scope, args.cwd), args.name)
  try {
    await rm(targetPath, { recursive: true, force: false })
    return { removed: true, targetPath }
  } catch (error) {
    if (isEnoentError(error)) {
      return { removed: false, targetPath }
    }
    throw error
  }
}
