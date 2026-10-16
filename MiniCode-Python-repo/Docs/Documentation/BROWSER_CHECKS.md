# 浏览器截图与检查

默认使用已有 Playwright，无需外部浏览器插件。运行时静态构建检查和项目自己的交互脚本共用截图检查点。图片展示、功能断言与模型视觉结论分别记录。

## 检查实际运行的服务

先使用项目的开发服务启动方式（Agent 使用 `start_service`）启动服务，再通过 `run_command` 调用 MiniCode 自身的 Python 解释器和 `minicode/project_browser.py` 绝对路径。下面是从已安装 MiniCode 的 Python 环境直接调用的示例：

```powershell
python -m minicode.project_browser --url http://127.0.0.1:5173 --output <检查产物目录> --source-root <被检查项目目录> --ready-selector "#app-ready"
```

`--root <dist目录>` 可替代 `--url`，用于静态构建。输出为 `desktop.png`、`mobile.png`、图片旁的 `.capture.json` 和 `report.json`。产物目录应放在本工作区所属模块的 `Data/Test/`；为避免来源版本被输出污染，目录必须在 `--source-root` 外，通用项目也可以使用源码根目录内被快照忽略的 `.temp/`。

Agent 调用 `run_command` 时：

- `image_paths` 填写全部预期截图的确切路径。
- `check_report` 填写本次的 `report.json`，它使用现有 `minicode.check-report.v1` 协议。
- 使用命令结果中的 `image_id` 调用 `view_image`。本地截图即使被覆盖，已保存的图片引用仍指向原始像素。
- 收到图片像素后再调用 `record_task_checks`，功能结论引用命令，视觉结论引用看图调用。图片生成不等于视觉检查通过。

## 交互检查点

场景是普通 Python 文件，定义同步函数 `inspect(page, capture)`，通过 `--scenario <文件>` 传入。每个视口使用独立浏览器上下文运行该函数。项目提供具体交互和断言，不需要新的操作配置语言。

```python
from playwright.sync_api import expect

def inspect(page, capture):
    expect(page.get_by_test_id("loading")).to_be_hidden()
    capture.checkpoint(page, "loaded", ready=page.get_by_role("main"))

    page.get_by_role("button", name="设置", exact=True).click()
    dialog = page.get_by_role("dialog", name="设置")
    capture.checkpoint(page, "settings", ready=dialog,
        visible={"save": page.get_by_role("button", name="保存", exact=True)})
    page.get_by_role("button", name="关闭", exact=True).click()

    footer = page.get_by_role("button", name="下一页", exact=True)
    footer.scroll_into_view_if_needed()
    capture.checkpoint(page, "bottom", visible={"next": footer})
```

对应图片为 `desktop-loaded.png`、`desktop-settings.png`、`desktop-bottom.png` 和手机视口对应文件。检查点名称不区分大小写且不可重复，防止 Windows 等文件系统覆盖已有图片。使用 `target=locator` 可生成局部截图：先滚动到目标，再等待图片和字体、执行检查并截图；`visible` 中的元素本身不会触发滚动。长页面优先通过滚动建立多个检查点，避免超长图缩小后无法检查细节。

每个检查点记录就绪、页面横向溢出、指定元素在视口内且采样点未被遮挡、截图保存等独立结果。元素可见性检查不证明业务操作成功。使用普通 Playwright 断言或 `capture.check("unique-name", boolean, detail)` 补充实际业务检查。默认每个视口最多 12 个检查点；单次命令展示仍受现有图片数量限制。

## 按任务选择检查

原生诊断、提交前检查、候选审查和最终验收使用同一选择策略。运行时结合本次请求、验收标准和实际变动文件选择范围，并记录 `check_scope`：

- 只修改普通文档：不自动构建或启动浏览器，不生成截图。已明确配置的必需检查仍执行。
- 后端 npm 代码：保留适用的构建检查，避免浏览器和截图；Python 代码继续使用已有的单元检查。
- 界面交互或被界面引用的共享逻辑：执行浏览器检查，默认只保留失败现场。
- 布局、样式、视觉资源和语言/主题配置：保留桌面、手机关键状态截图。

选择依据包含源码中的本地依赖关系；不是只按文件后缀判断。明确的视觉验收要求仍会触发相应检查。未知依赖结构和没有登记交互场景的项目不会凭空获得业务验收通过。

自动选择会遵守“不要截图”等明确限制：界面相关的浏览器断言仍可执行，但不保存截图。文档识别按文档扩展名和完整的常见文档名判断，`license.py`、`README.tsx` 等源码不会因名称前缀被跳过。

本地依赖发现有扫描范围和数量限制，不能完整理解所有自定义构建或动态加载。需要固定执行的业务检查应明确登记，避免仅依赖自动选择。

`--screenshots all|failure|none` 控制图像保存；检查点在不保存图片时仍执行就绪、溢出及指定元素检查。`--trace failure|none` 默认在失败时保存 `desktop-trace.zip` / `mobile-trace.zip`，包含 DOM 和网络操作记录；成功检查不保留 Trace，也不额外录制截图帧。已有已通过的原生构建结果只在同一运行、相同源码、检查策略和环境、且附件完整时复用。

## 交互验收项

`--criteria-file` 读取本次任务冻结的 JSON，例如：

```json
{"acceptanceCriteria":["搜索结果符合关键词","没有匹配项时说明空结果"],"requiredCriteria":[1,2]}
```

场景执行真实用户操作后，用可等待的断言关联验收项：

```python
from playwright.sync_api import expect

def inspect(page, capture):
    page.get_by_label("Search").fill("orange")
    capture.criterion(1, lambda: expect(page.locator("li:visible")).to_have_text(["orange"]))
    page.get_by_label("Search").fill("missing")
    capture.criterion(2, lambda: expect(page.get_by_text("No results")).to_be_visible())
    capture.checkpoint(page, "empty", visible={"search": page.get_by_label("Search")})
```

报告按验收项显示 `passed`、`failed` 或 `not_checked`。每个要求的视口都必须有实际关联断言；首屏加载、生成截图、执行点击本身不会自动覆盖业务要求。

正式验收复用已有任务文件的 `runtimeVerificationCommands` / `covers`，其中可配置原生浏览器场景：

```json
{
  "name":"search",
  "kind":"browser_render",
  "browser":{"root":"dist","scenario":"tests/browser/search.py","views":["desktop","mobile"],"screenshots":"failure"},
  "covers":[1,2],
  "timeout":60
}
```

`browser` 与 `command` 二选一。服务项目使用 `url` 替代 `root`；场景路径必须位于工作区内。需要构建或启动服务时，先通过现有构建/服务流程准备目标。运行时会冻结验收项编号、执行自己的浏览器入口、校验实际断言覆盖与源码稳定性；缺少覆盖、断言失败或源码变化均不通过。业务断言属于任务配置，不能仅凭模型提供的 JSON 报告获得正式验收。

`readySelector` 在场景开始前等待指定元素可见，超时会阻止场景执行。失败断言即使没有显式检查点，也会在截图策略允许时尽可能补充现场图。多个场景分别展示结果，后执行的成功场景不会覆盖前一个场景的失败记录；源码版本不同的记录不会混合展示。

静态目标另行记录文件内容指纹。隔离检查会复制明确指定、但被源码快照忽略的构建目录（如 `dist`），并在执行前后检查原目录和隔离副本；构建内容发生变化时，已通过的断言不能作为该候选版本的验收证据。静态目标上限为 10000 个文件、512 MiB；这项内容绑定仍不证明构建来自最新源码，构建准备需要由任务流程负责。

`runtime_collaboration_eval` 的独立外部校验器仍只执行 `command` 声明；它会在任务登记时明确拒绝 `browser` 声明，避免运行结束后因格式不兼容而崩溃。该评测入口需要配置显式的浏览器测试命令，常规任务运行时不受此限制。

## 环境与边界

- 默认只允许请求目标页面同源。使用重复的 `--allow-origin http://127.0.0.1:8000` 明确加入所需 API；WebSocket 单独使用 `--allow-websocket ws://127.0.0.1:8000`。页面路由限制不是 Python 场景脚本的网络沙箱，场景执行仍使用正常命令权限和进程取消机制。
- 桌面视口为 1440×900，手机为 390×844，并包含 Pixel 5 的触摸、UA 与像素比模拟。截图按 CSS 像素保存。使用 `--view desktop` 或 `--view mobile` 只检查一个视口。
- 默认固定语言、时区、浅色主题和减少动画。`--color-scheme dark` 检查暗色；`--animations allow` 用于动画任务，静态截图不能证明完整运动效果。
- 就绪等待与截图分别有界，`--timeout-ms` 默认 10000，最大 30000。整个场景的总时间由 `run_command.timeout` 控制。场景失败保留已有截图，尽可能生成失败现场图，进程返回非零退出码。
- `--browser auto` 按 Chromium、Edge、Chrome 尝试，并记录实际浏览器和版本。截图基线比较应使用固定浏览器、字体、操作系统和版本，避免使用自动回退。
- `--source-root` 在检查前后复用工作区内容指纹，包含未提交修改。它只证明观察到的本地文件版本；运行服务可能尚未热更新或根本不是该目录的服务。原生构建检查另行绑定经过验证的构建来源。
- `.capture.json` 通过图片 SHA-256 绑定到实际像素；错误、过期或超大的旁文件不会被采信。URL 的认证信息、查询字符串和片段不进入持久化来源记录。这些来源字段是上下文，不是可替代原生执行记录的验收证书。

## 回归验证

`tests/test_browser_capture.py` 用真实 HTTP 服务和浏览器执行正常页面及四个有缺陷的版本：手机横向溢出、弹窗按钮遮挡、透明父容器、底部固定栏遮挡。它还覆盖就绪超时、API/WebSocket 来源、源码变化、局部截图，以及命令截图到图片 ID、模型读取与证据记录的完整流程。模型读取用例验证实际像素投递，不评估模型是否能正确判断界面。

`tests/test_task_browser_checks.py` 覆盖任务范围选择、文档跳过与显式检查保留、真实搜索与空结果断言、缺失断言不能通过最终验收，以及失败现场在工作区清理后仍可读取。前端 `task-browser-checks.spec.ts` 通过真实检查产物验证桌面和手机上的验收项展示、图片读取及 Trace 下载；下载接口校验所属会话和运行。

`tests/test_browser_check_regressions.py` 另外覆盖源码名称误判、禁止截图、场景就绪、检查名称冲突、无检查点时的失败现场、隔离构建目录，以及检查期间构建文件被修改后必须拒绝验收。

稳定产品界面使用前端 Playwright Test 的 `toHaveScreenshot` 保存和比较基线；新增或更新基线需要查看实际图片与差异，不能自动更新来掩盖失败。新设计的视觉质量仍由明确的验收标准和看图判断负责。

前端已有图片预览弹窗的桌面、手机基线，执行：

```powershell
cd MiniCode-Web
npm run build
npm run test:visual
```

只有确认界面有意变更时才运行 `npm run test:visual -- --update-snapshots`，查看 `tests/visual/snapshots/` 中的实际图片，再正常执行比较。用例通过真实后端上传固定的 640×480 PNG，并检查图片、关闭按钮及视口边界；不调用模型。基线文件带操作系统名，当前仓库提供 Windows 基线。测试沿用隔离的 `VisionBrowser-*` 数据目录和 `8781` 端口，不能与图片输入测试同时占用该端口。

Python 环境缺少匹配浏览器时执行 `python -m playwright install chromium --only-shell`；前端环境执行 `npx playwright install chromium`。这是现有 Playwright 的浏览器运行依赖，无需安装额外插件。
