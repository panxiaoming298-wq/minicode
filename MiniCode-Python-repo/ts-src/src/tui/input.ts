const RESET = '\u001b[0m'
const DIM = '\u001b[2m'
const GREEN = '\u001b[32m'
const YELLOW = '\u001b[33m'
const BOLD = '\u001b[1m'
const REVERSE = '\u001b[7m'

export function renderInputPrompt(input: string, cursorOffset: number): string {
  const offset = Math.max(0, Math.min(cursorOffset, input.length))
  const before = input.slice(0, offset)
  const current = input[offset] ?? ' '
  const after = input.slice(Math.min(offset + 1, input.length))
  return [
    `${YELLOW}${BOLD}prompt${RESET} ${DIM}Enter send | /help commands | Esc clear | Ctrl+C exit${RESET}`,
    '',
    `${GREEN}${BOLD}mini-code>${RESET} ${before}${REVERSE}${current}${RESET}${after}${DIM}${input ? '' : ' Ask for code, files, tasks, or MCP tools'}${RESET}`,
  ].join('\n')
}
