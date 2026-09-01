import process from 'node:process'

const ENTER_ALT_SCREEN = '\u001b[?1049h'
const EXIT_ALT_SCREEN = '\u001b[?1049l'
const ERASE_SCREEN_AND_HOME = '\u001b[2J\u001b[H'
const ENABLE_MOUSE_TRACKING =
  '\u001b[?1000h' +
  '\u001b[?1006h'
const DISABLE_MOUSE_TRACKING =
  '\u001b[?1006l' +
  '\u001b[?1000l'
export function hideCursor(): void {
  process.stdout.write('\u001b[?25l')
}

export function showCursor(): void {
  process.stdout.write('\u001b[?25h')
}

export function enterAlternateScreen(): void {
  process.stdout.write(
    DISABLE_MOUSE_TRACKING + ENTER_ALT_SCREEN + ERASE_SCREEN_AND_HOME + ENABLE_MOUSE_TRACKING,
  )
}

export function exitAlternateScreen(): void {
  process.stdout.write(DISABLE_MOUSE_TRACKING + EXIT_ALT_SCREEN)
}

export function clearScreen(): void {
  // Softer redraw than full clear to reduce visible flicker.
  process.stdout.write('\u001b[H\u001b[J')
}
