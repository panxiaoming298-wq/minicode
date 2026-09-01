import type { AgentStep, ChatMessage, ModelAdapter } from './types.js'

function lastUserMessage(messages: ChatMessage[]): string {
  const last = [...messages].reverse().find(message => message.role === 'user')
  return last?.content ?? ''
}

function lastToolMessage(messages: ChatMessage[]): ChatMessage | undefined {
  return [...messages].reverse().find(message => message.role === 'tool_result')
}

function extractLatestAssistantCall(messages: ChatMessage[]): string | undefined {
  const last = [...messages]
    .reverse()
    .find(
      message =>
        message.role === 'assistant_tool_call',
    )
  return last?.role === 'assistant_tool_call'
    ? last.toolName
    : undefined
}

export class MockModelAdapter implements ModelAdapter {
  async next(messages: ChatMessage[]): Promise<AgentStep> {
    const toolMessage = lastToolMessage(messages)
    if (toolMessage?.role === 'tool_result') {
      const lastCall = extractLatestAssistantCall(messages)
      if (lastCall === 'list_files') {
        return {
          type: 'assistant',
          content: `目录内容如下：\n\n${toolMessage.content}`,
        }
      }

      if (lastCall === 'read_file') {
        return {
          type: 'assistant',
          content: `文件内容如下：\n\n${toolMessage.content}`,
        }
      }

      if (lastCall === 'write_file' || lastCall === 'edit_file') {
        return {
          type: 'assistant',
          content: toolMessage.content,
        }
      }

      return {
        type: 'assistant',
        content: `我拿到了工具结果：\n\n${toolMessage.content}`,
      }
    }

    const userText = lastUserMessage(messages).trim()

    if (userText === '/tools') {
      return {
        type: 'assistant',
        content: '可用工具：ask_user, list_files, grep_files, read_file, write_file, edit_file, run_command',
      }
    }

    if (userText.startsWith('/ls')) {
      const dir = userText.replace('/ls', '').trim()
      return {
        type: 'tool_calls',
        calls: [{
          id: `mock-${Date.now()}`,
          toolName: 'list_files',
          input: dir ? { path: dir } : {},
        }],
      }
    }

    if (userText.startsWith('/grep ')) {
      const payload = userText.slice('/grep '.length).trim()
      const [pattern, searchPath] = payload.split('::')
      return {
        type: 'tool_calls',
        calls: [{
          id: `mock-${Date.now()}`,
          toolName: 'grep_files',
          input: {
            pattern: pattern.trim(),
            path: searchPath?.trim() || undefined,
          },
        }],
      }
    }

    if (userText.startsWith('/read ')) {
      return {
        type: 'tool_calls',
        calls: [{
          id: `mock-${Date.now()}`,
          toolName: 'read_file',
          input: { path: userText.slice('/read '.length).trim() },
        }],
      }
    }

    if (userText.startsWith('/cmd ')) {
      const parts = userText.slice('/cmd '.length).trim().split(/\s+/)
      const [command, ...args] = parts
      return {
        type: 'tool_calls',
        calls: [{
          id: `mock-${Date.now()}`,
          toolName: 'run_command',
          input: { command, args },
        }],
      }
    }

    if (userText.startsWith('/write ')) {
      const payload = userText.slice('/write '.length)
      const splitAt = payload.indexOf('::')
      if (splitAt === -1) {
        return {
          type: 'assistant',
          content: '用法: /write 路径::内容',
        }
      }

      return {
        type: 'tool_calls',
        calls: [{
          id: `mock-${Date.now()}`,
          toolName: 'write_file',
          input: {
            path: payload.slice(0, splitAt).trim(),
            content: payload.slice(splitAt + 2),
          },
        }],
      }
    }

    if (userText.startsWith('/edit ')) {
      const payload = userText.slice('/edit '.length)
      const [targetPath, search, replace] = payload.split('::')
      if (!targetPath || search === undefined || replace === undefined) {
        return {
          type: 'assistant',
          content: '用法: /edit 路径::查找文本::替换文本',
        }
      }

      return {
        type: 'tool_calls',
        calls: [{
          id: `mock-${Date.now()}`,
          toolName: 'edit_file',
          input: {
            path: targetPath.trim(),
            search,
            replace,
          },
        }],
      }
    }

    return {
      type: 'assistant',
      content: [
        '这是一个最小骨架版本。',
        '你可以试试：',
        '/tools',
        '/ls',
        '/grep pattern::src',
        '/read README.md',
        '/cmd pwd',
        '/write notes.txt::hello',
        '/edit notes.txt::hello::hello world',
      ].join('\n'),
    }
  }
}
