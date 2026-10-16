/** Human-readable summaries; raw diagnostics remain available in run details. */
export function providerBalanceMessage(reason: string): string | null {
  // Require a provider failure prefix so task budgets and quoted document text
  // cannot be mistaken for the model provider's account balance.
  if (!/^(?:Model API error\b|Provider availability failure\b)/i.test(reason.trim())) return null;
  if (!/\binsufficient balance\b|\bcredit balance is too low\b|\binsufficient credits?\b/i.test(reason)) return null;
  return "模型服务账户余额不足，当前请求已被服务商拒绝。请给当前服务商账号补充余额，或在设置中切换可用的模型服务；增大 MiniCode 任务 Token 预算不会恢复调用。";
}

export function providerFailure(reason: string): { title: string; message: string } | null {
  const text = reason.trim().replace(/^(?:ProviderError|ToolFailed):\s*/, "");
  if (text.startsWith("Provider stream error: ")) {
    try {
      const diagnostic = JSON.parse(text.slice("Provider stream error: ".length));
      if (!["stream", "http_response", "connection"].includes(diagnostic?.stage)) return null;
      const titles: Record<string, string> = {
        upstream: "上游模型输出中断，执行已暂停",
        rate_limit: "模型服务请求受限，执行已暂停",
        configuration: "模型或请求配置不可用",
        authentication: "模型服务身份验证或权限失败",
        quota: "模型服务账户额度不足",
        unknown: "模型服务返回错误，执行已暂停",
      };
      if (typeof diagnostic.classification !== "string" || !Object.hasOwn(titles, diagnostic.classification)) return null;
      const title = titles[diagnostic.classification];
      const retried = Number.isInteger(diagnostic.retries) && diagnostic.retries > 0
        ? `已自动重试 ${diagnostic.retries} 次，仍未恢复。` : "";
      const guidance = diagnostic.classification === "configuration"
        ? "请检查服务地址、模型名称和请求参数后继续。"
        : diagnostic.classification === "authentication" ? "请检查密钥与模型调用权限后继续。"
        : diagnostic.classification === "quota" ? "请检查服务商账户额度，补充额度或切换可用模型后继续。"
        : "请检查模型服务与连接，稍后从已有进度继续。";
      return { title, message: `${retried}已完成的工具结果和已有修改保留。${guidance}未确认的调用用量仍标记为未知。` };
    } catch { return null; }
  }
  if (/^Model API error\b/i.test(text) && /Streaming error:/.test(text)
    && /upstream_error|upstream sent an error event/.test(text)) {
    return { title: "上游模型输出中断，执行已暂停",
      message: "模型流式响应中返回了上游错误，当前记录无法区分中转服务和原始模型服务故障。已有修改保留；检查服务与连接后，从已有进度继续。" };
  }
  if ((/^(?:Model API error\b|Provider availability failure\b)/i.test(text) && /ProviderFirstOutputTimeout/.test(text))
    || /^Model API timeout: No model text, reasoning or tool output within\b/.test(text)) {
    return { title: "模型迟迟未开始有效输出", message: "连接中没有收到模型文字、推理或工具参数，MiniCode 已结束这次等待。可重试或切换模型；未返回的用量仍标记为未知。" };
  }
  const balance = providerBalanceMessage(reason);
  if (balance) return { title: "模型服务账户余额不足", message: balance };
  if (/^(?:Model API error\b|Provider availability failure\b)/i.test(reason.trim())
    && /\baccess to model denied\b/i.test(reason)) {
    return { title: "当前模型无调用权限", message: "模型服务商拒绝了当前模型的调用权限。请在服务商处确认该模型已开通且当前密钥有权使用，或在设置中切换可调用的模型；模型出现在列表中、连接检查成功，都不保证生成请求可用。" };
  }
  return null;
}

export function explainStopReason(reason: string): string {
  const failure = providerFailure(reason);
  if (failure) return failure.message;
  if (/^dependency wait \S+ blocked: timeout$/.test(reason.trim())) return "等待子任务超时，主任务已停止。任务总预算与依赖等待时限分别生效；保留已有进度并检查等待设置后继续。";
  if (/^Runtime model budget stopped execution: run canceled during model request$/i.test(reason.trim())) return "运行在模型请求期间被取消；请查看主任务记录确认最初停止原因。";
  if (/context.window|context.length|context.*capacity/i.test(reason)) return "当前请求超出模型上下文容量，需要缩小读取范围或整理上下文；增加任务总预算不能扩大上下文。";
  if (/\bdeadline\b|\btime[- ]+budget\b/i.test(reason)) return "已达到本次任务的截止时间，任务尚未完成。可查看已保存的进度后继续。";
  if (/budget|token.*(limit|exhaust)|max_tokens|reserve.*request|request.*reserv/i.test(reason)) return "剩余额度不足以启动下一步，或需要保留额度完成验收。已用量与请求预留分别计入限制；任务尚未完成。";
  if (/native read set is incomplete|missing observations/i.test(reason)) return "成员的读取证据不完整，补丁尚未合并；需要重新读取当前文件并生成新候选。";
  if (/Builder has no accepted, applied and checked patch/i.test(reason)) return "部分成员修改尚未完成合并与组合检查。";
  if (/terminal node failure|not every current Plan node succeeded/i.test(reason)) return "部分执行步骤未完成，最终交付尚未通过验收。";
  return reason;
}
