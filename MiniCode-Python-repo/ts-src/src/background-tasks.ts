import process from 'node:process'
import type { BackgroundTaskResult } from './tool.js'
import { getErrorCode } from './utils/errors.js'

type BackgroundTaskRecord = BackgroundTaskResult & {
  cwd: string
}

const tasks = new Map<string, BackgroundTaskRecord>()

function makeTaskId(): string {
  return `shell_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
}

function refreshRecord(record: BackgroundTaskRecord): BackgroundTaskRecord {
  if (record.status !== 'running') {
    return record
  }

  try {
    process.kill(record.pid, 0)
    return record
  } catch (error) {
    const code = getErrorCode(error)
    if (code === 'ESRCH') {
      const next = {
        ...record,
        status: 'completed' as const,
      }
      tasks.set(record.taskId, next)
      return next
    }

    const next = {
      ...record,
      status: 'failed' as const,
    }
    tasks.set(record.taskId, next)
    return next
  }
}

export function registerBackgroundShellTask(args: {
  command: string
  pid: number
  cwd: string
}): BackgroundTaskResult {
  const task: BackgroundTaskRecord = {
    taskId: makeTaskId(),
    type: 'local_bash',
    command: args.command,
    pid: args.pid,
    cwd: args.cwd,
    status: 'running',
    startedAt: Date.now(),
  }
  tasks.set(task.taskId, task)
  return task
}

export function listBackgroundTasks(): BackgroundTaskResult[] {
  return [...tasks.values()].map(refreshRecord)
}

export function getBackgroundTask(taskId: string): BackgroundTaskResult | null {
  const task = tasks.get(taskId)
  if (!task) {
    return null
  }
  return refreshRecord(task)
}
