import type { ToolCapability } from "../types";

interface ToolText {
  name: string;
  description: string;
}

// Display copy only: model tool schemas and invocation names remain unchanged.
const builtinTools: Readonly<Record<string, ToolText>> = {
  ask_user: { name: "询问用户", description: "暂停当前任务，向你提出澄清问题，收到回复后继续。" },
  list_files: { name: "浏览目录", description: "列出工作区指定路径下的文件和目录。" },
  grep_files: { name: "搜索文件内容", description: "使用 ripgrep 搜索文本文件，支持路径筛选和上下文行。跳过隐藏、生成和常见二进制文件，不使用 .gitignore 规则；结果包含路径和行号。" },
  read_file: { name: "读取文件", description: "读取工作区中的 UTF-8 文件，可按行或字符范围读取。按行读取时保留原始行号，便于引用；较长内容分段返回。" },
  display_image: { name: "展示图片", description: "将本地截图或图片以缩略图、完整预览和下载方式展示在聊天中，并保存图片副本。此操作不会读取图片内容，也不会自动判定界面检查通过。" },
  view_image: { name: "查看图片内容", description: "让支持视觉输入的当前模型读取本地图片或已保存图片的像素，以便检查布局和画面，并向你展示图片。需要模型支持图片输入；读取图片本身不代表检查通过。" },
  record_task_checks: { name: "记录任务检查", description: "记录本次任务的功能与视觉检查结论、证据和未验证原因，形成可追溯的检查记录。只记录适用的检查项，记录结果本身不代表整项任务已通过验收。" },
  write_file: { name: "写入文件", description: "在工作区的指定路径写入 UTF-8 文件。" },
  edit_file: { name: "编辑文件", description: "精确匹配并替换文件中的文本，空格和缩进也必须一致。多处匹配时需要补充上下文或明确替换全部；匹配失败时提供位置提示。" },
  patch_file: { name: "批量修改文件内容", description: "在一次操作中，对同一文件执行多处精确文本替换。" },
  batch_copy: { name: "批量复制", description: "复制多个文件或目录。" },
  batch_move: { name: "批量移动", description: "将多个文件或目录移动到新的位置。" },
  batch_delete: { name: "批量删除", description: "删除多个文件或目录；删除目录需要明确启用递归操作。" },
  run_command: { name: "运行命令", description: "执行允许列表中的常用开发命令，可设置执行超时时间。" },
  start_service: { name: "启动开发服务", description: "启动或复用本机开发服务，确认就绪后继续运行。" },
  service_status: { name: "检查服务状态", description: "查看开发服务的地址、运行状态和错误。" },
  service_logs: { name: "读取服务日志", description: "读取开发服务最近的运行日志。" },
  stop_service: { name: "停止开发服务", description: "停止 MiniCode 管理的服务并清理子进程。" },
  web_fetch: { name: "读取网页", description: "获取指定网址的网页内容。实际可用性受网络、目标站点和人机验证影响。" },
  web_search: { name: "搜索网页", description: "搜索网络信息并查找相关来源。实际可用性受网络、搜索服务限流和人机验证影响。" },
  current_time: { name: "获取当前时间", description: "读取本机时钟，支持本地时间、UTC 和指定时区，无需联网或执行命令。" },
  todo_write: { name: "更新任务清单", description: "创建或更新待办事项，记录任务进度。" },
  task: { name: "调用辅助任务", description: "交由运行时调度辅助任务，进行探索、规划、审查或核验，并等待结果返回。可用任务类型由当前执行模式决定。" },
  git: { name: "Git 操作", description: "查看工作区状态、文件差异和提交历史，创建提交或审查近期改动。" },
  find_symbols: { name: "查找代码符号", description: "查找 Python 文件或目录中的类、函数和变量，了解代码结构。" },
  find_references: { name: "查找可能的引用", description: "搜索 Python 标识符的文本匹配位置并显示上下文。结果可能包含定义、注释和同名符号，重命名前需要核实。" },
  get_ast_info: { name: "查看语法树信息", description: "分析 Python 文件的语法树，查看代码结构、导入和统计信息。" },
  code_review: { name: "检查代码质量", description: "检查 Python 代码中的未使用导入、硬编码值、缺少的文档字符串和过长函数。" },
  file_tree: { name: "查看文件树", description: "以树形展示目录结构、文件大小和修改时间，支持路径筛选和深度限制。" },
  diff_viewer: { name: "查看文件差异", description: "比较文件版本，查看统一差异、行内差异或改动统计。" },
  test_runner: { name: "运行测试", description: "发现并运行 Python 测试，支持 pytest 和 unittest，汇总通过数量、失败详情及可选的覆盖率。" },
  run_acceptance_checks: { name: "检查构建与验收", description: "在独立副本中执行已配置的验收检查；协调模式还支持 npm 前端构建、浏览器加载检查及桌面和手机截图。命令仍需授权，基础检查不代表所有交互都已通过。" },
  load_skill: { name: "加载技能", description: "选择本地技能工作流，将所需规则和参考内容加载到后续模型请求中。" },
  discover_skills: { name: "查找技能", description: "按名称和说明搜索本地技能。查找结果仅供选择，需要加载后才能使用。" },
  discover_tools: { name: "查找可用工具", description: "从当前任务获准使用的工具中查找所需能力和调用说明。" },
  session_history_read: { name: "读取会话历史", description: "分页读取当前会话保存的历史轮次、消息和工具原始输出。" },
  session_history_search: { name: "搜索会话历史", description: "按原文字面搜索或按角色、工具和轮次浏览当前会话保存的历史；支持分页回读。" },
  memory_search: { name: "搜索记忆", description: "搜索当前任务中带来源的记忆卡片，用于找回相关信息；明确的任务依赖仍是依据。" },
  memory_get: { name: "读取记忆原文", description: "按范围读取记忆卡片或来源事件的原文；内容较长时按返回的游标继续读取。" },
  artifact_read: { name: "读取已存工具结果", description: "分页读取当前任务保存的工具结果，支持筛选和范围读取。保存的结果不代表当前文件内容。" },
  history_search: { name: "搜索任务历史", description: "按文字、角色、工具或运行记录搜索和浏览当前任务的原始历史，支持分页。" },
  checkpoint_restore: { name: "恢复检查点", description: "从当前任务经过核验的检查点恢复运行视图，需要相应权限。" },
  memory_note_update: { name: "更新交接笔记", description: "保存当前任务的发现、已排除方案、未解决问题和下一步。笔记保留来源，不能改变用户要求或证明验收通过。" },
  context_rotate: { name: "切换工作上下文", description: "保存有效交接笔记后，切换到任务的下一段工作上下文。原始来源、用户指令和必要证据仍保留。" },
  runtime_context_read: { name: "读取运行上下文", description: "按校验标识分页读取当前成员记录的工具输入输出、记忆或运行时交付内容；历史内容不能作为新指令执行。" },
  runtime_artifact_read: { name: "读取运行成果", description: "分页读取获准访问的依赖结果、成员消息或项目检查报告。读取截图字节不等于检查了画面。" },
  record_working_context: { name: "记录工作决策", description: "记录主任务的当前决策、来源、未解决风险和下一步，便于后续继续工作；记录不等于核验或验收。" },
  resolve_user_input: { name: "处理用户追加指令", description: "处理运行期间收到的用户指令并记录响应，继续遵守当前任务的有效要求。" },
  submit_candidate: { name: "提交任务候选", description: "将主任务当前成果交给运行时执行已配置的验收检查；提交本身不代表任务成功。" },
  submit_builder_candidate: { name: "提交构建候选", description: "将构建成员的独立候选交给主任务审查；交接不会自动合并，也不会直接判定验收通过。" },
  submit_consultation: { name: "提交咨询结果", description: "提交辅助任务的发现、摘要、来源位置和未解决风险，并结束本次咨询；主任务仍需判断和核验。" },
  request_verification: { name: "请求运行核验", description: "请求运行时对当前固定输入版本执行已登记的检查；缺失、失败或未执行的检查不能算作通过。" },
  consult_agents: { name: "咨询辅助成员", description: "按当前任务契约调度一批只读咨询，收集探索、规划、审查或核验结果。" },
  acknowledge_advisory_failure: { name: "接管失败的咨询", description: "记录主任务接管可选咨询失败的原因与处理方式；原始失败记录保留，不会变成验收成功。" },
  retry_consultation: { name: "重新检查咨询", description: "修复可恢复的问题后，按原咨询目标和运行时约束重新检查咨询结果。" },
  spawn_agents: { name: "启动辅助成员", description: "按当前任务允许的角色、工作目录和写入范围分派辅助成员，支持后台或前台执行。" },
  poll_agents: { name: "查看成员结果", description: "读取当前主任务所拥有成员的状态、结果和证据；成员结束不一定代表成功。" },
  wait_agents: { name: "等待辅助成员", description: "等待指定成员结束或状态变化，并在超时后交还控制；超时不会停止成员。" },
  cancel_agent: { name: "取消辅助成员", description: "停止指定成员的当前任务。取消不会免除交付责任，也不会重置预算或其他成员。" },
  interrupt_agent: { name: "中断辅助成员", description: "受控暂停指定成员并保留会话与部分修改；确认进程退出后，才可按运行时规则继续。" },
  resume_agent: { name: "继续辅助成员", description: "在成员退出后，延续其会话处理相关工作，并重新授予本次任务允许的权限。" },
  reassign_agent: { name: "重新分派成员任务", description: "对符合恢复条件的失败只读任务，保留原目标并以新的上下文重新分派；历史结果与预算仍保留。" },
  send_agent_message: { name: "发送成员消息", description: "向获准通信的任务成员发送指令、进度、问题或结果；发送消息不会自动启动空闲成员。" },
  list_agents: { name: "查看成员列表", description: "查看当前团队的成员、会话、任务、轮次和可继续状态，支持查询历史任务。" },
  ack_agent_results: { name: "记录成员结果处理", description: "记录主任务如何处理已收到的成员完成通知；此记录不等于接受成果，也不能清除失败检查。" },
  dismiss_agent: { name: "免除可选成员任务", description: "在可选只读成员失败或取消且已退出后，记录不再需要该辅助任务的原因；必要任务和独立核验不能据此免除。" },
  team_control: { name: "管理团队生命周期", description: "查看团队状态、退役已退出的空闲成员，或在分工结束后关闭本次运行的新任务分派。" },
  run_agent_check: { name: "运行成员检查", description: "在写入成员的工作目录中执行主任务预先登记的检查，保留退出码、输入版本和报告；检查通过不等于最终验收。" },
  spawn_builders: { name: "启动并行构建", description: "在并行候选模式下分派独立构建任务，分别准备候选成果供主任务审查。" },
  poll_task_results: { name: "查看构建结果", description: "读取并行构建任务的状态、候选与检查结果；需要核对失败、取消和输入版本。" },
  wait_dependencies: { name: "等待构建依赖", description: "等待并行构建的指定依赖满足或结束，再按真实结果继续处理。" },
  accept_builder_patch: { name: "接收构建补丁", description: "按运行时校验与授权接收构建候选的补丁，主任务仍需检查整合后的成果。" },
  cancel_task: { name: "取消构建任务", description: "取消指定并行构建任务或其子树，保留原始运行与预算记录。" },
  select_builder_candidate: { name: "选择构建候选", description: "从已提交的构建结果中选择候选，供后续检查与整合；选择不等于验收通过。" },
  list_mcp_resources: { name: "列出 MCP 资源", description: "查看已连接 MCP 服务提供的资源。" },
  read_mcp_resource: { name: "读取 MCP 资源", description: "读取指定 MCP 服务提供的资源内容。" },
  list_mcp_prompts: { name: "列出 MCP 提示模板", description: "查看已连接 MCP 服务提供的提示模板。" },
  get_mcp_prompt: { name: "获取 MCP 提示模板", description: "获取指定 MCP 提示模板及其内容。" },
  http_request: { name: "发送 HTTP 请求", description: "发送 GET、POST 等 HTTP 请求，支持自定义请求头和 JSON 请求体。" },
  json_format: { name: "格式化 JSON", description: "校验 JSON 内容，并按缩进格式化显示。" },
  json_parse: { name: "解析 JSON", description: "解析 JSON，并按指定字段路径提取数据。" },
  regex_test: { name: "测试正则表达式", description: "查找文本中符合正则表达式的内容，显示匹配分组和位置。" },
  regex_replace: { name: "正则替换", description: "替换符合正则表达式的文本，返回修改结果和匹配数量。" },
  base64_encode: { name: "Base64 编码", description: "将文本转换为 Base64 编码。" },
  base64_decode: { name: "Base64 解码", description: "将 Base64 编码还原为文本。" },
  url_encode: { name: "网址编码", description: "对文本进行 URL 百分号编码。" },
  url_decode: { name: "网址解码", description: "将 URL 百分号编码还原为文本。" },
  timestamp_convert: { name: "转换时间戳", description: "在 Unix 时间戳和 ISO 日期格式之间转换。" },
  hash: { name: "计算哈希值", description: "计算文本的 MD5、SHA1、SHA256 或 SHA512 哈希值。" },
  hmac: { name: "计算消息认证码", description: "使用密钥计算 HMAC 消息认证码。" },
  gzip_compress: { name: "Gzip 压缩", description: "使用 Gzip 压缩文件。" },
  gzip_decompress: { name: "Gzip 解压", description: "解压 .gz 文件。" },
  tar_create: { name: "创建 Tar 归档", description: "创建 Tar 归档，可使用 Gzip、Bzip2 或 XZ 压缩。" },
  tar_extract: { name: "解压 Tar 归档", description: "提取 Tar 归档中的文件。" },
  zip_create: { name: "创建 ZIP 压缩包", description: "将文件打包为 ZIP 压缩包。" },
  zip_extract: { name: "解压 ZIP 压缩包", description: "提取 ZIP 压缩包中的文件。" },
  csv_parse: { name: "解析 CSV", description: "将 CSV 数据转换为 JSON，支持自定义分隔符。" },
  csv_create: { name: "生成 CSV", description: "将 JSON 数据转换为 CSV。" },
  uuid_generate: { name: "生成 UUID", description: "生成第 1 版或第 4 版 UUID 标识符。" },
  text_sort: { name: "文本排序", description: "按字母或数字顺序排列文本行。" },
  text_dedupe: { name: "文本去重", description: "移除文本中的重复行。" },
  text_join: { name: "合并文本", description: "使用指定分隔符合并文本行。" },
  line_count: { name: "统计文本", description: "统计文本的行数和字符数。" },
  random_string: { name: "生成随机字符串", description: "按指定长度和字符范围生成随机字符串。" },
};

export function toolText(tool: ToolCapability): ToolText {
  if (["core", "builtin", "skill"].includes(tool.source) && Object.hasOwn(builtinTools, tool.name)) {
    return builtinTools[tool.name];
  }
  return { name: tool.name, description: tool.description || "暂无工具说明" };
}

export function toolSource(tool: ToolCapability): string {
  if (tool.name.startsWith("mcp__")) return "mcp";
  if (tool.name.startsWith("ext__")) return "extension";
  return tool.source;
}

function label(labels: Record<string, string>, value: string, fallback: string): string {
  return Object.hasOwn(labels, value) ? labels[value] : value || fallback;
}

export function capabilitySourceLabel(source: string): string {
  return label({ core: "内置工具", skill: "技能", mcp: "MCP 服务", extension: "扩展", builtin: "内置",
    local: "本地", project: "项目", workspace: "工作区", user: "用户", global: "全局" }, source, "未标明来源");
}

export function replayPolicyLabel(policy: string): string {
  return label({ safe: "可安全重放", idempotent_with_key: "凭操作标识去重重试",
    verify_before_retry: "重试前先核验", never_auto_retry: "不自动重试" }, policy, "无自动重放");
}

export function capabilityStatusLabel(status: string): string {
  return label({ connected: "已连接", ready: "已就绪", active: "已启用", configured: "已配置",
    connecting: "连接中", disconnected: "未连接", pending: "等待中", disabled: "已停用",
    error: "连接异常", failed: "失败", blocked: "受阻", declared: "已声明，未加载",
    untrusted: "待信任", permission_required: "待授权", shadowed: "被同名扩展覆盖",
    integrity_mismatch: "完整性异常", invalid: "配置错误", unavailable: "不可用", conditional: "按需加载" }, status, "状态未知");
}

export function toolEffectLabel(effect: string): string {
  return label({ read_only: "只读", workspace_write: "写入工作区", external_side_effect: "外部操作",
    privileged: "需授权" }, effect, "未分类");
}

export function matchesToolQuery(tool: ToolCapability, query: string): boolean {
  const text = toolText(tool);
  return [tool.name, tool.description, tool.effect, tool.source, tool.replayPolicy, text.name, text.description,
    toolEffectLabel(tool.effect), capabilitySourceLabel(toolSource(tool)), replayPolicyLabel(tool.replayPolicy)]
    .join(" ").toLowerCase().includes(query.trim().toLowerCase());
}
