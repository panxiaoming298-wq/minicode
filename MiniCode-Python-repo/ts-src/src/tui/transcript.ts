import process from 'node:process'
import { renderMarkdownish } from './markdown.js'
import type { TranscriptEntry } from './types.js'

const RESET = '\u001b[0m'
const DIM = '\u001b[2m'
const CYAN = '\u001b[36m'
const GREEN = '\u001b[32m'
const YELLOW = '\u001b[33m'
const RED = '\u001b[31m'
const MAGENTA = '\u001b[35m'
const BOLD = '\u001b[1m'
const BLUE = '\u001b[34m'

function indentBlock(input: string, prefix = '  '): string {
  return input
    .split('\n')
    .map(line => `${prefix}${line}`)
    .join('\n')
}

function previewToolBody(toolName: string, body: string): string {
  const maxChars = toolName === 'read_file' ? 1000 : 1800
  const maxLines = toolName === 'read_file' ? 20 : 36
  const lines = body.split('\n')
  const limitedLines = lines.length > maxLines ? lines.slice(0, maxLines) : lines
  let limited = limitedLines.join('\n')

  if (limited.length > maxChars) {
    limited = `${limited.slice(0, maxChars)}...`
  }

  if (limited !== body) {
    return `${limited}\n${DIM}... output truncated in transcript${RESET}`
  }

  return limited
}

function renderTranscriptEntry(entry: TranscriptEntry): string {
  if (entry.kind === 'user') {
    return `${CYAN}${BOLD}you${RESET}\n${indentBlock(entry.body)}`
  }

  if (entry.kind === 'assistant') {
    return `${GREEN}${BOLD}assistant${RESET}\n${indentBlock(
      renderMarkdownish(entry.body),
    )}`
  }

  if (entry.kind === 'progress') {
    return `${YELLOW}${BOLD}progress${RESET}\n${indentBlock(
      renderMarkdownish(entry.body),
    )}`
  }

  const status =
    entry.status === 'running'
      ? `${YELLOW}running${RESET}`
      : entry.status === 'success'
        ? `${GREEN}ok${RESET}`
        : `${RED}err${RESET}`

  const body =
    entry.status === 'running'
      ? entry.body
      : entry.collapsed
        ? `${DIM}${entry.collapsedSummary ?? 'output collapsed'}${RESET}`
        : entry.collapsePhase
          ? `${DIM}collapsing${'.'.repeat(entry.collapsePhase)}${RESET}`
          : previewToolBody(entry.toolName, renderMarkdownish(entry.body))

  return `${MAGENTA}${BOLD}tool${RESET} ${entry.toolName} ${status}\n${indentBlock(body)}`
}

export function getTranscriptWindowSize(windowSize?: number): number {
  if (windowSize !== undefined) {
    return Math.max(4, windowSize)
  }
  const rows = process.stdout.rows ?? 40
  return Math.max(8, rows - 15)
}

const entryCache = new WeakMap<TranscriptEntry, { state: string; lines: string[] }>()

function getEntryLines(entry: TranscriptEntry): string[] {
  const stateStr = JSON.stringify({
    kind: entry.kind,
    body: entry.body,
    status: (entry as any).status,
    collapsed: (entry as any).collapsed,
    collapsePhase: (entry as any).collapsePhase,
    collapsedSummary: (entry as any).collapsedSummary,
    toolName: (entry as any).toolName
  })

  const cached = entryCache.get(entry)
  if (cached && cached.state === stateStr) {
    return cached.lines
  }

  const lines = renderTranscriptEntry(entry).split('\n')
  entryCache.set(entry, { state: stateStr, lines })
  return lines
}

function renderTranscriptLines(entries: TranscriptEntry[]): string[] {
  const separator = `${BLUE}${DIM}·${RESET}`
  const lines: string[] = []

  entries.forEach((entry, index) => {
    if (index > 0) {
      lines.push('')
      lines.push(separator)
      lines.push('')
    }

    lines.push(...getEntryLines(entry))
  })

  return lines
}

export function getTranscriptMaxScrollOffset(
  entries: TranscriptEntry[],
  windowSize?: number,
): number {
  if (entries.length === 0) return 0
  const lines = renderTranscriptLines(entries)
  return Math.max(0, lines.length - getTranscriptWindowSize(windowSize))
}

export function renderTranscript(
  entries: TranscriptEntry[],
  scrollOffset: number,
  windowSize?: number,
): string {
  if (entries.length === 0) {
    return ''
  }

  const lines = renderTranscriptLines(entries)
  const pageSize = getTranscriptWindowSize(windowSize)
  const maxOffset = Math.max(0, lines.length - pageSize)
  const offset = Math.max(0, Math.min(scrollOffset, maxOffset))
  const end = lines.length - offset
  const start = Math.max(0, end - pageSize)
  const body = lines.slice(start, end).join('\n')

  if (offset === 0) {
    return body
  }

  return `${body}\n\n${DIM}scroll offset: ${offset}${RESET}`
}
