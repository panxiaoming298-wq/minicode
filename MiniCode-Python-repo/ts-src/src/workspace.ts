import path from 'node:path'
import { realpath } from 'node:fs/promises'
import type { ToolContext } from './tool.js'

export async function resolveToolPath(
  context: ToolContext,
  targetPath: string,
  intent: 'read' | 'write' | 'list' | 'search',
): Promise<string> {
  const resolved = path.resolve(context.cwd, targetPath)

  if (!context.permissions) {
    const workspaceRoot = path.resolve(context.cwd)
    
    // 使用 realpath 规范化路径，防止符号链接绕过
    let normalizedResolved: string
    let normalizedRoot: string
    
    try {
      normalizedResolved = await realpath(resolved)
      normalizedRoot = await realpath(workspaceRoot)
    } catch (error) {
      // realpath 失败时拒绝访问，这是更安全的选择
      // 失败原因可能是路径不存在或权限不足
      throw new Error(
        `Path access denied: unable to resolve path securely. ` +
        `Reason: ${error instanceof Error ? error.message : 'unknown error'}`
      )
    }
    
    const relative = path.relative(normalizedRoot, normalizedResolved)

    // 增强的路径检查：同时检查 ../ 和 ..\
    if (
      relative === '..' ||
      relative.startsWith('..\\') ||
      relative.startsWith('../') ||
      path.isAbsolute(relative)
    ) {
      throw new Error(`Path escapes workspace: ${targetPath}`)
    }

    return resolved
  }

  await context.permissions.ensurePathAccess(resolved, intent)
  return resolved
}
