# MiniCode Web

MiniCode Web 是 MiniCode 的本地浏览器工作台。页面由 React + TypeScript + Vite 构建，通过本机 FastAPI 服务调用 `MiniCode-Python-repo` 里的 agent 运行时。

## 运行形态

- 浏览器打开 `http://127.0.0.1:8765`。
- 模型、工具、Git、文件和命令由本机 Python 服务处理。
- 任务在独立子进程中运行，可以真正停止。
- 前端使用 SSE 接收模型、工具、审批和完成事件。
- API Key 保存在 `~/.mini-code/settings.json`，不保存到 `localStorage`。

当前不需要向量数据库、RabbitMQ、Redis 或其他外部中间件。会话和设置都是本机文件。

## 一次构建，直接使用

确保目录结构为：

```text
MiniCode/
  MiniCode-Python-repo/
  MiniCode-Web/
```

安装并构建前端：

```powershell
cd D:\ai测试项目\MiniCode\MiniCode-Web
npm install
npm run build
```

安装 Web 后端并启动：

```powershell
cd D:\ai测试项目\MiniCode\MiniCode-Python-repo
python -m pip install -e ".[web]"
minicode-web --workspace "D:\ai测试项目\MiniCode\MiniCode-Python-repo"
```

然后打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。后端会自动读取同级 `MiniCode-Web/dist` 生产构建。

也可以在本目录直接运行：

```powershell
.\Start-MiniCode-Web.ps1
```

## 开发模式

终端 1：

```powershell
cd D:\ai测试项目\MiniCode\MiniCode-Python-repo
minicode-web --workspace "D:\ai测试项目\MiniCode\MiniCode-Python-repo"
```

终端 2：

```powershell
cd D:\ai测试项目\MiniCode\MiniCode-Web
npm run dev
```

开发页面位于 [http://127.0.0.1:5173](http://127.0.0.1:5173)，Vite 会把 `/api` 代理到 `127.0.0.1:8765`。

## 页面能力

- 通过 Windows 系统文件夹选择器打开项目，也保留手动路径输入作为备用。
- 左侧分开展示置顶、项目和独立对话；每个项目可展开多个归属于该项目的对话。
- 顶部“新对话”创建不读取项目文件的独立对话；项目行的加号创建可读写该项目的新对话。
- 跨项目选择对话时，会自动切换到它所属的工作区再恢复会话。
- 流式查看 assistant 输出、工具调用、运行状态和权限审批。
- 停止正在运行的 agent 子进程。
- 在右侧文件树浏览项目原文，并在 Git 变更中切换“差异 / 原文”。
- 在页面里配置 DeepSeek、OpenAI、Anthropic、OpenRouter 和自定义 OpenAI-compatible 接口。
- 显式执行提供商连接测试。
- 适配桌面和手机宽度。

## 后续扩展

如果要操作浏览器，应在 Python 服务侧增加 Playwright/CDP 工具或配套浏览器扩展，然后继续通过现有任务事件和审批协议展示。项目文件夹已经由本地服务调用系统选择器；如果还需要托盘、自动更新或更深的桌面集成，可以用 Tauri 包装这套前端，不需要重写 UI。

## 验证

```powershell
npm run typecheck
npm run test
npm run build
npm run test:e2e
```
