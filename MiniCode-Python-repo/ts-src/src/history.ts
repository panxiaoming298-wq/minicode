import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { MINI_CODE_DIR, MINI_CODE_HISTORY_PATH } from './config.js'

type HistoryFile = {
  entries: string[]
}

export async function loadHistoryEntries(): Promise<string[]> {
  try {
    const raw = await readFile(MINI_CODE_HISTORY_PATH, 'utf8')
    const parsed = JSON.parse(raw) as HistoryFile
    return Array.isArray(parsed.entries) ? parsed.entries : []
  } catch {
    return []
  }
}

export async function saveHistoryEntries(entries: string[]): Promise<void> {
  await mkdir(MINI_CODE_DIR, { recursive: true })
  await writeFile(
    MINI_CODE_HISTORY_PATH,
    `${JSON.stringify({ entries: entries.slice(-200) }, null, 2)}\n`,
    'utf8',
  )
}
