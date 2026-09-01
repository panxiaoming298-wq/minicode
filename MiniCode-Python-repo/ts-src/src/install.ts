import { mkdir, writeFile, access } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import readline from 'node:readline'
import { fileURLToPath } from 'node:url'
import {
  MINI_CODE_SETTINGS_PATH,
  loadEffectiveSettings,
  saveMiniCodeSettings,
} from './config.js'

function hasPathEntry(target: string): boolean {
  // 使用 path.delimiter 跨平台兼容（Windows 是 ;，Unix 是 :）
  const pathEntries = (process.env.PATH ?? '').split(path.delimiter)
  return pathEntries.includes(target)
}

async function askRequired(
  nextLine: () => Promise<string | null>,
  label: string,
  defaultValue?: string,
): Promise<string> {
  while (true) {
    const suffix = defaultValue ? ` [${defaultValue}]` : ''
    process.stdout.write(`${label}${suffix}: `)
    const incoming = await nextLine()
    const answer = (incoming ?? '').trim()
    const value = answer || defaultValue || ''
    if (value) return value
    console.log('该项不能为空，请重新输入。')
  }
}

function secretPromptSuffix(secret?: string): string {
  if (!secret) return ' [not set]'
  return ' [saved]'
}

async function main(): Promise<void> {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  })

  try {
    const iterator = rl[Symbol.asyncIterator]()
    const nextLine = async (): Promise<string | null> => {
      const result = await iterator.next()
      return result.done ? null : result.value
    }

    const settings = await loadEffectiveSettings()
    const currentEnv = settings.env ?? {}

    console.log('mini-code installer')
    console.log(`配置会写入 ${MINI_CODE_SETTINGS_PATH}`)
    console.log('配置保存在独立目录中，不会影响其它本地工具配置。')
    console.log('')

    const model = await askRequired(
      nextLine,
      'Model name',
      settings.model ? String(settings.model) : String(currentEnv.ANTHROPIC_MODEL ?? ''),
    )
    const baseUrl = await askRequired(
      nextLine,
      'ANTHROPIC_BASE_URL',
      String(currentEnv.ANTHROPIC_BASE_URL ?? 'https://api.anthropic.com'),
    )
    const savedAuthToken = String(currentEnv.ANTHROPIC_AUTH_TOKEN ?? '')
    process.stdout.write(`ANTHROPIC_AUTH_TOKEN${secretPromptSuffix(savedAuthToken)}: `)
    const tokenInput = ((await nextLine()) ?? '').trim()
    const authToken = tokenInput || savedAuthToken

    if (!authToken) {
      throw new Error('ANTHROPIC_AUTH_TOKEN 不能为空。')
    }

    await saveMiniCodeSettings({
      model,
      env: {
        ANTHROPIC_BASE_URL: baseUrl,
        ANTHROPIC_AUTH_TOKEN: authToken,
        ANTHROPIC_MODEL: model,
      },
    })

    const home = os.homedir()
    const targetBinDir = path.join(home, '.local', 'bin')
    const launcherPath = path.join(targetBinDir, 'minicode')
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
    
    // 验证 repoRoot 路径的合法性（确保在预期的安装目录内）
    const normalizedRepoRoot = path.normalize(repoRoot)
    if (normalizedRepoRoot.includes('..') || normalizedRepoRoot.includes('~')) {
      throw new Error('Invalid installation path: repo root contains suspicious path elements')
    }
    
    const launcherScript = [
      '#!/usr/bin/env bash',
      'set -euo pipefail',
      `exec "${path.join(repoRoot, 'bin', 'minicode')}" "$@"`,
      '',
    ].join('\n')

    await mkdir(targetBinDir, { recursive: true })
    
    // 检查文件是否已存在，存在则提示用户确认
    try {
      await access(launcherPath)
      console.log('')
      console.log(`⚠️  警告: ${launcherPath} 已存在`)
      console.log('继续安装将覆盖现有文件。')
      
      // 复用外部的 readline 实例
      process.stdout.write('是否继续？(y/N): ')
      const response = ((await nextLine()) ?? '').trim().toLowerCase()
      
      if (response !== 'y' && response !== 'yes') {
        console.log('安装已取消。')
        return
      }
    } catch {
      // 文件不存在，继续安装
    }
    
    // 使用原子写入（先写临时文件再重命名）
    const tempLauncherPath = `${launcherPath}.tmp.${process.pid}`
    try {
      // 1. 先写入临时文件
      await writeFile(tempLauncherPath, launcherScript, { mode: 0o755 })
      
      // 2. 重命名临时文件到目标路径（原子操作）
      const { rename } = await import('node:fs/promises')
      
      // Windows 上 rename 可能会失败，需要特殊处理
      if (process.platform === 'win32') {
        // Windows 上如果目标文件存在，需要先删除
        try {
          const { unlink } = await import('node:fs/promises')
          await unlink(launcherPath)
        } catch {
          // 目标文件不存在，忽略
        }
      }
      
      await rename(tempLauncherPath, launcherPath)
    } catch (error) {
      // 发生错误时清理临时文件
      try {
        const { unlink } = await import('node:fs/promises')
        await unlink(tempLauncherPath)
      } catch {
        // 临时文件不存在，忽略
      }
      throw error
    }

    console.log('')
    console.log('安装完成。')
    console.log(`配置文件: ${MINI_CODE_SETTINGS_PATH}`)
    console.log(`启动命令: ${launcherPath}`)

    if (!hasPathEntry(targetBinDir)) {
      console.log('')
      console.log(`你的 PATH 里还没有 ${targetBinDir}`)
      
      // 根据平台显示不同的配置指引
      if (process.platform === 'win32') {
        console.log('Windows 用户请将此目录添加到系统 PATH 环境变量：')
        console.log(`  ${targetBinDir}`)
        console.log('或者在 PowerShell 中运行：')
        console.log(`  [Environment]::SetEnvironmentVariable("PATH", "$env:PATH;${targetBinDir}", "User")`)
      } else {
        const shellConfigFile = process.platform === 'darwin' ? '~/.zshrc' : '~/.bashrc'
        console.log(`可以把下面这行加入 ${shellConfigFile}:`)
        console.log(`export PATH="${targetBinDir}:$PATH"`)
      }
    } else {
      console.log('')
      console.log('现在你可以在任意终端输入 `minicode` 启动。')
    }
  } finally {
    rl.close()
  }
}

main().catch(error => {
  console.error(error)
  process.exitCode = 1
})
