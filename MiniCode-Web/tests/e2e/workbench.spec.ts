import { expect, test } from "@playwright/test";

test("opens a project through the folder-picker button", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop workspace test");
  const secondWorkspace = process.env.PLAYWRIGHT_SECOND_WORKSPACE;
  test.skip(!secondWorkspace, "PLAYWRIGHT_SECOND_WORKSPACE is required");
  const secondWorkspaceName = secondWorkspace!.replace(/[\\/]+$/, "").split(/[\\/]/).at(-1)!;

  await page.route("**/api/workspaces/pick", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ cancelled: false, workspace: secondWorkspace }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "打开项目" }).click();
  const switcher = page.getByRole("dialog", { name: "打开项目" });
  await expect(switcher).toBeVisible();
  await switcher.getByRole("button", { name: /选择文件夹/ }).click();
  await expect(switcher).toBeHidden();
  await expect(page.getByRole("complementary", { name: "项目和对话" }).getByText(secondWorkspaceName, { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: "artifacts/minicode-web-workspace-picker.png", fullPage: true });
});

test("browses project files and displays the original source", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop file-browser test");

  await page.goto("/");
  const sourceProject = process.env.PLAYWRIGHT_SOURCE_PROJECT_NAME || "MiniCode-Python-repo";
  await page.getByRole("button", { name: `在 ${sourceProject} 中新建对话` }).click({ force: true });
  const inspector = page.locator("#workspace-inspector");
  await expect(inspector).toBeVisible();
  await inspector.getByRole("tab", { name: "文件" }).click();
  const tree = inspector.getByRole("tree", { name: "项目文件" });
  await expect(tree).toBeVisible();
  await tree.getByRole("button", { name: /README\.md/ }).click();
  const source = inspector.getByLabel("README.md 原文", { exact: true });
  await expect(source).toBeVisible();
  await expect(source).toContainText("MiniCode");
});

test("resizes, closes, and restores the inspector", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop inspector layout test");

  await page.goto("/");
  const inspector = page.locator("#workspace-inspector");
  const resizeHandle = page.getByRole("separator", { name: "调整检查器宽度" });
  await expect(inspector).toHaveAttribute("aria-hidden", "false");
  await expect(resizeHandle).toBeVisible();

  const before = await inspector.boundingBox();
  const handleBox = await resizeHandle.boundingBox();
  expect(before).not.toBeNull();
  expect(handleBox).not.toBeNull();
  await page.mouse.move(handleBox!.x + handleBox!.width / 2, handleBox!.y + 120);
  await page.mouse.down();
  await page.mouse.move(handleBox!.x - 96, handleBox!.y + 120, { steps: 6 });
  await page.mouse.up();

  const resized = await inspector.boundingBox();
  expect((resized?.width || 0) - (before?.width || 0)).toBeGreaterThan(70);
  await expect.poll(() => page.evaluate(() => localStorage.getItem("minicode.inspector.width"))).not.toBeNull();

  const toggle = page.locator(".workspace-inspector-toggle");
  await expect(toggle).toHaveAttribute("aria-label", "关闭检查器");
  const openToggleBox = await toggle.boundingBox();
  await toggle.click();
  await expect(inspector).toHaveAttribute("aria-hidden", "true");
  await expect(resizeHandle).toBeHidden();
  await expect(toggle).toHaveAttribute("aria-label", "打开检查器");
  const closedToggleBox = await toggle.boundingBox();
  expect(Math.abs((closedToggleBox?.x || 0) - (openToggleBox?.x || 0))).toBeLessThanOrEqual(1);
  expect(Math.abs((closedToggleBox?.y || 0) - (openToggleBox?.y || 0))).toBeLessThanOrEqual(1);

  await toggle.click();
  await expect(inspector).toHaveAttribute("aria-hidden", "false");
  await expect(toggle).toHaveAttribute("aria-label", "关闭检查器");
  const restored = await inspector.boundingBox();
  expect(Math.abs((restored?.width || 0) - (resized?.width || 0))).toBeLessThanOrEqual(1);
  await resizeHandle.focus();
  await resizeHandle.press("ArrowRight");
  const keyboardResized = await inspector.boundingBox();
  expect((restored?.width || 0) - (keyboardResized?.width || 0)).toBeGreaterThanOrEqual(15);
  const rail = await page.getByRole("complementary", { name: "项目和对话" }).boundingBox();
  expect(Math.abs((rail?.width || 0) - 292)).toBeLessThanOrEqual(1);
  expect(await page.evaluate(() => window.scrollX)).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: "artifacts/minicode-web-resizable-inspector.png", fullPage: true });
});

test("shows the real capability and context surfaces without horizontal overflow", async ({ page }, testInfo) => {
  await page.goto("/");
  const inspector = page.locator("#workspace-inspector");
  if (testInfo.project.name === "mobile") {
    await page.getByRole("button", { name: "打开检查器" }).click();
  }
  await expect(inspector).toHaveAttribute("aria-hidden", "false");
  await inspector.getByRole("tab", { name: "能力" }).click();
  await expect(inspector.getByRole("heading", { name: "工具面" })).toBeVisible({ timeout: 15_000 });
  await expect(inspector.getByText("read_file", { exact: true })).toBeVisible();
  await expect(inspector.getByText("Skills", { exact: true })).toBeVisible();
  await inspector.getByRole("tab", { name: "上下文" }).click();
  await expect(inspector.getByRole("heading", { name: "上下文占用" })).toBeVisible();
  await expect(inspector.getByRole("heading", { name: "指令层" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({
    path: `artifacts/minicode-web-context-${testInfo.project.name}.png`,
    fullPage: true,
  });
});

test("shows persisted runtime plan, verification, and live events", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop runtime inspection test");
  test.skip(process.env.PLAYWRIGHT_REAL_RUNTIME !== "1", "Requires an isolated real backend");

  await page.goto("/");
  await page.getByLabel("任务内容").fill("你现在是什么思考强度");
  await page.getByLabel("任务内容").press("Enter");
  await expect(page.getByText("已完成", { exact: true })).toBeVisible({ timeout: 25_000 });
  const inspector = page.locator("#workspace-inspector");
  await inspector.getByRole("tab", { name: "运行" }).click();
  await expect(inspector.getByRole("heading", { name: "执行拓扑" })).toBeVisible({ timeout: 15_000 });
  await expect(inspector.getByRole("heading", { name: "运行计划" })).toBeVisible();
  await expect(inspector.getByRole("heading", { name: "验证" })).toBeVisible();
  await expect(inspector.getByRole("heading", { name: "实时事件" })).toBeVisible();
  await expect(inspector.getByText("能力清单已同步", { exact: true })).toBeVisible();
  await page.screenshot({ path: "artifacts/minicode-web-runtime-inspection.png", fullPage: true });
});

test("pins and unpins a conversation from the global rail", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop navigation test");

  const conversation = {
    sessionId: "pin-test-session",
    createdAt: Date.now() - 2_000,
    updatedAt: Date.now(),
    firstMessage: "待置顶的对话",
    lastMessage: "测试回答",
    messageCount: 2,
    workspace: "C:\\Users\\test\\.mini-code\\standalone",
    scope: "standalone",
    projectPath: null,
    model: null,
    status: "completed",
    toolCallCount: 0,
  };
  let pinned = false;
  const navigation = () => ({
    projects: [],
    sessions: [conversation],
    pinnedSessionIds: pinned ? [conversation.sessionId] : [],
    pinnedWorkspacePaths: [],
  });
  await page.route("**/api/navigation", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(navigation()),
  }));
  await page.route("**/api/navigation/pins", (route) => {
    pinned = Boolean((route.request().postDataJSON() as { pinned?: boolean }).pinned);
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(navigation()),
    });
  });

  await page.goto("/");
  const rail = page.getByRole("complementary", { name: "项目和对话" });
  const pin = rail.getByRole("button", { name: /^置顶对话 / }).first();
  await expect(pin).toBeVisible();
  const label = await pin.getAttribute("aria-label");
  const conversationName = label!.replace(/^置顶对话 /, "");
  await pin.click();

  await expect(rail.getByText("置顶", { exact: true })).toBeVisible();
  const unpin = rail.getByRole("button", { name: `取消置顶对话 ${conversationName}` }).first();
  await expect(unpin).toBeVisible();
  await unpin.click();
  await expect(rail.getByRole("button", { name: `置顶对话 ${conversationName}` }).first()).toBeVisible();
});

test("groups project conversations under projects and keeps standalone conversations separate", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop conversation grouping test");
  const projectPath = "D:\\mock-workspaces\\alpha";
  const navigation = {
    projects: [{ name: "Alpha", path: projectPath }],
    sessions: [
      {
        sessionId: "project-1",
        createdAt: Date.now() - 20_000,
        updatedAt: Date.now() - 10_000,
        firstMessage: "项目问题一",
        lastMessage: "项目回答一",
        messageCount: 2,
        workspace: projectPath,
        scope: "project",
        projectPath,
        model: null,
        status: "completed",
        toolCallCount: 1,
      },
      {
        sessionId: "standalone-1",
        createdAt: Date.now() - 8_000,
        updatedAt: Date.now() - 4_000,
        firstMessage: "独立问题一",
        lastMessage: "独立回答一",
        messageCount: 2,
        workspace: "C:\\Users\\test\\.mini-code\\standalone",
        scope: "standalone",
        projectPath: null,
        model: null,
        status: "completed",
        toolCallCount: 0,
      },
    ],
    pinnedSessionIds: [],
    pinnedWorkspacePaths: [],
  };

  await page.route("**/api/navigation", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(navigation),
  }));
  await page.route("**/api/workspaces/open", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      workspace: projectPath,
      workspaceStatus: { isGitRepository: false, branch: "", files: [], summary: "非 Git 工作区" },
      recentWorkspaces: navigation.projects,
      sessions: [navigation.sessions[0]],
    }),
  }));

  await page.goto("/");
  const rail = page.getByRole("complementary", { name: "项目和对话" });
  const projects = rail.getByRole("region", { name: "项目" });
  const standalone = rail.getByRole("region", { name: "对话" });
  await projects.getByRole("button", { name: "展开项目 Alpha" }).click();
  await expect(projects.getByText("项目问题一", { exact: true })).toBeVisible();
  const nestedConversation = projects.locator(".conversation-item.is-nested").filter({ hasText: "项目问题一" });
  await expect(nestedConversation.getByRole("button", { name: /置顶对话/ })).toHaveCount(0);
  await expect(standalone.getByText("独立问题一", { exact: true })).toBeVisible();
  await expect(standalone.getByText("项目问题一", { exact: true })).toHaveCount(0);

  await projects.getByRole("button", { name: "在 Alpha 中新建对话" }).click({ force: true });
  await expect(page.getByRole("heading", { name: "把项目工作交给 MiniCode" })).toBeVisible();
  await expect(page.getByText("Alpha", { exact: true })).toBeVisible();

  await rail.getByRole("button", { name: "新对话 Ctrl N", exact: true }).click();
  await expect(page.getByRole("heading", { name: "开始独立对话" })).toBeVisible();
  await expect(page.getByText("这是独立对话，没有绑定项目文件。请从左侧项目下新建对话后浏览源码。", { exact: true })).toBeVisible();
});

test("settles the analysis row when a run reaches a terminal state", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop run-state test");

  await page.route("**/api/runs", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      run: {
        runId: "mock-run",
        sessionId: "mock-session",
        workspace: "C:\\Users\\test\\.mini-code\\standalone",
        promptPreview: "检查状态",
        status: "running",
        createdAt: Date.now(),
        updatedAt: Date.now(),
        eventCount: 0,
        scope: "standalone",
      },
    }),
  }));
  await page.route("**/api/runs/mock-run/events", (route) => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: { "Cache-Control": "no-cache" },
    body: [
      `data: ${JSON.stringify({ kind: "status", sequence: 1, timestamp: Date.now(), status: "running" })}\n\n`,
      `data: ${JSON.stringify({ kind: "thinking_status", sequence: 2, timestamp: Date.now(), content: "正在分析当前任务" })}\n\n`,
      `data: ${JSON.stringify({ kind: "assistant", sequence: 3, timestamp: Date.now(), content: "状态检查完成。" })}\n\n`,
      `data: ${JSON.stringify({ kind: "status", sequence: 4, timestamp: Date.now(), status: "completed", content: "任务已结束" })}\n\n`,
    ].join(""),
  }));

  await page.goto("/");
  const composer = page.getByLabel("任务内容");
  await composer.fill("检查状态");
  await composer.press("Enter");
  await expect(page.getByText("分析当前任务完成", { exact: true })).toBeVisible();
  await expect(page.getByText("正在分析当前任务", { exact: true })).toHaveCount(0);
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
});

test("switches away from a running conversation and reconnects when returning", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop conversation switching test");
  const now = Date.now();
  const runningSession = {
    sessionId: "running-session",
    createdAt: now - 8_000,
    updatedAt: now,
    firstMessage: "后台整理项目",
    lastMessage: "正在读取项目结构",
    messageCount: 1,
    workspace: "C:\\Users\\test\\.mini-code\\standalone",
    scope: "standalone",
    projectPath: null,
    model: "mock-model",
    status: "running",
    toolCallCount: 1,
  };
  const completedSession = {
    ...runningSession,
    sessionId: "completed-session",
    createdAt: now - 20_000,
    updatedAt: now - 12_000,
    firstMessage: "已经完成的问题",
    lastMessage: "已完成的回答。",
    messageCount: 2,
    status: "completed",
    toolCallCount: 0,
  };
  const run = {
    runId: "running-run",
    sessionId: runningSession.sessionId,
    workspace: runningSession.workspace,
    promptPreview: runningSession.firstMessage,
    status: "running",
    createdAt: now,
    updatedAt: now,
    eventCount: 2,
    executionProfile: "standard",
    scope: "standalone",
  };
  const events = [
    { kind: "status", sequence: 1, timestamp: now, status: "running", content: "任务已启动" },
    { kind: "assistant", sequence: 2, timestamp: now + 1, content: "后台任务仍在执行。" },
  ];
  const eventRequests: string[] = [];

  await page.route("**/api/navigation", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      projects: [],
      sessions: [runningSession, completedSession],
      pinnedSessionIds: [],
      pinnedWorkspacePaths: [],
    }),
  }));
  await page.route((url) => url.pathname === "/api/sessions/running-session", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      session: runningSession,
      messages: [{ role: "user", content: runningSession.firstMessage }],
    }),
  }));
  await page.route((url) => url.pathname === "/api/sessions/completed-session", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      session: completedSession,
      messages: [
        { role: "user", content: completedSession.firstMessage },
        { role: "assistant", content: completedSession.lastMessage },
      ],
    }),
  }));
  await page.route("**/api/sessions/running-session/run", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ run, events }),
  }));
  await page.route("**/api/sessions/completed-session/run", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ run: null, events: [] }),
  }));
  await page.route((url) => url.pathname === "/api/runs/running-run", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ run, events }),
  }));
  await page.route("**/api/runs/running-run/events*", (route) => {
    eventRequests.push(route.request().url());
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache" },
      body: ": keep-alive\n\n",
    });
  });

  await page.goto("/");
  const rail = page.getByRole("complementary", { name: "项目和对话" });
  await rail.locator(".rail-item-main").filter({ hasText: runningSession.firstMessage }).click();
  await expect(page.getByText("后台任务仍在执行。", { exact: true })).toBeVisible();
  await expect(rail.getByText("运行中", { exact: true })).toBeVisible();

  await rail.locator(".rail-item-main").filter({ hasText: completedSession.firstMessage }).click();
  await expect(page.getByText(completedSession.lastMessage, { exact: true })).toBeVisible();
  await expect(rail.getByText("运行中", { exact: true })).toBeVisible();

  await rail.locator(".rail-item-main").filter({ hasText: runningSession.firstMessage }).click();
  await expect(page.getByText("后台任务仍在执行。", { exact: true })).toBeVisible();
  await expect.poll(() => eventRequests.some((url) => new URL(url).searchParams.get("after") === "2")).toBe(true);
});

test("continues a stopped task in the same conversation", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop stopped-task recovery test");
  const now = Date.now();
  let sessionStatus = "stopped";
  let resumeRequest: Record<string, unknown> | null = null;
  const session = {
    sessionId: "stopped-session",
    createdAt: now - 20_000,
    updatedAt: now - 2_000,
    firstMessage: "整理未完成的模块",
    lastMessage: "整理未完成的模块",
    messageCount: 1,
    workspace: "C:\\Users\\test\\.mini-code\\standalone",
    scope: "standalone",
    projectPath: null,
    model: "mock-model",
    status: "stopped",
    toolCallCount: 1,
  };
  const stoppedRun = {
    runId: "stopped-run",
    sessionId: session.sessionId,
    workspace: session.workspace,
    promptPreview: session.firstMessage,
    status: "stopped",
    createdAt: now - 10_000,
    updatedAt: now - 2_000,
    eventCount: 2,
    executionProfile: "standard",
    scope: "standalone",
  };

  await page.route("**/api/navigation", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      projects: [],
      sessions: [{ ...session, status: sessionStatus }],
      pinnedSessionIds: [],
      pinnedWorkspacePaths: [],
    }),
  }));
  await page.route((url) => url.pathname === "/api/sessions/stopped-session", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      session: { ...session, status: sessionStatus },
      messages: [{ role: "user", content: session.firstMessage }],
    }),
  }));
  await page.route("**/api/sessions/stopped-session/run", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      run: stoppedRun,
      events: [
        { kind: "status", sequence: 1, timestamp: now - 10_000, status: "running" },
        { kind: "status", sequence: 2, timestamp: now - 2_000, status: "stopped", content: "任务已结束" },
      ],
    }),
  }));
  await page.route((url) => url.pathname === "/api/runs" && url.search === "", async (route) => {
    resumeRequest = route.request().postDataJSON() as Record<string, unknown>;
    sessionStatus = "running";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        run: {
          ...stoppedRun,
          runId: "resumed-run",
          promptPreview: String(resumeRequest.prompt || ""),
          status: "running",
          createdAt: Date.now(),
          updatedAt: Date.now(),
          eventCount: 0,
        },
      }),
    });
  });
  await page.route("**/api/runs/resumed-run/events*", (route) => {
    sessionStatus = "completed";
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache" },
      body: [
        `data: ${JSON.stringify({ kind: "assistant", sequence: 1, timestamp: Date.now(), content: "已经从中断处继续并完成。" })}\n\n`,
        `data: ${JSON.stringify({ kind: "status", sequence: 2, timestamp: Date.now(), status: "completed", content: "任务已结束" })}\n\n`,
      ].join(""),
    });
  });

  await page.goto("/");
  const rail = page.getByRole("complementary", { name: "项目和对话" });
  await rail.locator(".rail-item-main").filter({ hasText: session.firstMessage }).click();
  const continueButton = page.getByRole("button", { name: /继续上次任务/ });
  await expect(continueButton).toBeVisible();
  await continueButton.click();

  await expect(page.getByText("已经从中断处继续并完成。", { exact: true })).toBeVisible();
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
  expect(resumeRequest).toMatchObject({
    sessionId: session.sessionId,
    scope: "standalone",
    prompt: "继续上次任务。先确认已完成和未完成的部分，再从中断处接着处理。",
  });
  await expect(continueButton).toBeHidden();
});

test("explains failed network tools and command approval in plain language", async ({ page }, testInfo) => {
  const now = Date.now();
  await page.route("**/api/runs", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      run: {
        runId: "plain-approval-run",
        sessionId: "plain-approval-session",
        workspace: "C:\\Users\\test\\.mini-code\\standalone",
        promptPreview: "C罗近况",
        status: "running",
        createdAt: now,
        updatedAt: now,
        eventCount: 0,
        scope: "standalone",
      },
    }),
  }));
  await page.route("**/api/runs/plain-approval-run/events", (route) => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: { "Cache-Control": "no-cache" },
    body: [
      `data: ${JSON.stringify({ kind: "status", sequence: 1, timestamp: now, status: "running" })}\n\n`,
      `data: ${JSON.stringify({ kind: "tool_start", sequence: 2, timestamp: now + 1, toolName: "web_search", input: { query: "C罗近况" } })}\n\n`,
      `data: ${JSON.stringify({
        kind: "tool_result",
        sequence: 3,
        timestamp: now + 2,
        toolName: "web_search",
        isError: true,
        durationMs: 2218,
        output: "WEB_SEARCH_UNAVAILABLE\n联网搜索暂时不可用：公共搜索入口没有返回可用结果。\n- DuckDuckGo HTML: DuckDuckGo 要求人机验证",
      })}\n\n`,
      `data: ${JSON.stringify({
        kind: "approval_requested",
        sequence: 4,
        timestamp: now + 3,
        requestId: "approval-1",
        request: {
          kind: "command",
          summary: "mini-code wants approval for this command",
          details: ["cwd: C:\\Users\\test\\.mini-code\\standalone"],
          scope: "curl -s -L https://sports.sina.com.cn/",
          choices: [
            { label: "允许这次访问", decision: "allow_once" },
            { label: "以后自动允许这条命令", decision: "allow_always" },
            { label: "不访问", decision: "deny_once" },
            { label: "以后自动拒绝这条命令", decision: "deny_always" },
          ],
          title: "允许读取 sports.sina.com.cn 的网页？",
          action: "MiniCode 将通过 curl 访问 sports.sina.com.cn，读取公开网页内容。",
          impact: "只会联网读取并把网页内容交给当前任务；不会修改本机文件。",
          reason: "这里做的是网页读取，但 curl 属于可执行的本机命令，所以安全策略仍要求你确认。",
          target: "sports.sina.com.cn",
          targetLabel: "访问网站",
          operationType: "network_read",
          timeoutPolicy: "不确认时，任务会停在这里，不会自动执行。",
          technicalDetails: [
            { label: "完整命令", value: "curl -s -L https://sports.sina.com.cn/" },
            { label: "操作校验码", value: "abcdef1234567890" },
          ],
        },
      })}\n\n`,
    ].join(""),
  }));

  await page.goto("/");
  await page.getByLabel("任务内容").fill("C罗近况");
  await page.getByLabel("任务内容").press("Enter");

  const failedTool = page.locator("details.tool-error");
  await expect(failedTool).toBeVisible();
  await expect(failedTool).toHaveAttribute("open", "");
  await expect(failedTool.getByText("公共搜索入口没有返回可用结果，可能被限流或要求人机验证。")).toBeVisible();
  await expect(failedTool.getByText("错误详情", { exact: true })).toBeVisible();

  const approval = page.getByRole("region", { name: "需要审批" });
  await expect(approval.getByText("允许读取 sports.sina.com.cn 的网页？", { exact: true })).toBeVisible();
  await expect(approval.getByText("只会联网读取并把网页内容交给当前任务；不会修改本机文件。", { exact: true })).toBeVisible();
  await expect(approval.getByText("为什么会弹窗", { exact: true })).toBeVisible();
  await expect(approval.getByText("访问网站", { exact: true })).toBeVisible();
  await expect(approval.getByText("不确认时，任务会停在这里，不会自动执行。", { exact: true })).toBeVisible();
  await expect(approval.getByText("mini-code wants approval for this command", { exact: true })).toHaveCount(0);
  await expect(approval.getByRole("button", { name: "允许这次访问" })).toBeVisible();
  await expect(approval.getByRole("button", { name: "不访问" })).toBeVisible();
  await expect(approval.getByRole("button", { name: "以后自动允许这条命令" })).toBeHidden();

  await approval.getByText("查看命令与技术详情", { exact: true }).click();
  await expect(approval.getByText("curl -s -L https://sports.sina.com.cn/", { exact: true })).toBeVisible();
  await approval.getByText("更多授权范围", { exact: true }).click();
  await expect(approval.getByRole("button", { name: "以后自动允许这条命令" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({
    path: `artifacts/minicode-web-plain-approval-${testInfo.project.name}.png`,
    fullPage: true,
  });
});

test("stops a current-news lookup after one relevant search and shows a Chinese answer", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop current-news flow test");
  const now = Date.now();
  let submittedProfile = "";
  await page.route("**/api/runs", (route) => {
    submittedProfile = (route.request().postDataJSON() as { executionProfile?: string }).executionProfile || "";
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        run: {
          runId: "current-news-run",
          sessionId: "current-news-session",
          workspace: "C:\\Users\\test\\.mini-code\\standalone",
          promptPreview: "C罗近况",
          status: "running",
          createdAt: now,
          updatedAt: now,
          eventCount: 0,
          scope: "standalone",
          executionProfile: "ultra",
        },
      }),
    });
  });
  await page.route("**/api/runs/current-news-run/events", (route) => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: { "Cache-Control": "no-cache" },
    body: [
      `data: ${JSON.stringify({ kind: "status", sequence: 1, timestamp: now, status: "running" })}\n\n`,
      `data: ${JSON.stringify({ kind: "tool_start", sequence: 2, timestamp: now + 1, toolName: "web_search", input: { query: "C罗近况" } })}\n\n`,
      `data: ${JSON.stringify({
        kind: "tool_result",
        sequence: 3,
        timestamp: now + 2,
        toolName: "web_search",
        isError: false,
        durationMs: 820,
        output: "PROVIDER: Google News RSS\nPROVIDER_QUERY: Cristiano Ronaldo Al Nassr Portugal football\nRELEVANCE_FILTER: strong\nEVIDENCE_READY: cross-source dated news results",
      })}\n\n`,
      `data: ${JSON.stringify({
        kind: "runtime",
        sequence: 4,
        timestamp: now + 3,
        runtime: {
          category: "guard",
          phase: "verify",
          message: "已取得足够的相关网页证据；停止继续搜索，正在整理为中文回答。",
        },
      })}\n\n`,
      `data: ${JSON.stringify({
        kind: "assistant",
        sequence: 5,
        timestamp: now + 4,
        content: "根据多家带日期的足球新闻来源，C罗目前仍效力利雅得胜利，并继续代表葡萄牙队参赛。",
      })}\n\n`,
      `data: ${JSON.stringify({ kind: "done", sequence: 6, timestamp: now + 5, stopReason: "done" })}\n\n`,
      `data: ${JSON.stringify({ kind: "status", sequence: 7, timestamp: now + 6, status: "completed", content: "任务已结束" })}\n\n`,
    ].join(""),
  }));

  await page.goto("/");
  const profileButton = page.getByRole("button", { name: /^执行模式：(Standard|Ultra)$/ });
  if ((await profileButton.getAttribute("aria-label")) !== "执行模式：Ultra") {
    await profileButton.click();
    await page.getByRole("option", { name: /Ultra/ }).click();
  }
  await page.getByLabel("任务内容").fill("C罗近况");
  await page.getByLabel("任务内容").press("Enter");

  expect(submittedProfile).toBe("ultra");
  await expect(page.locator("details.tool-item")).toHaveCount(1);
  const researchStop = page.locator(".progress-item").filter({ hasText: "已取得足够的相关网页证据" });
  await expect(researchStop).toContainText("结果检查 · 核验 — 已取得足够的相关网页证据；停止继续搜索，正在整理为中文回答");
  await expect(page.getByText("根据多家带日期的足球新闻来源，C罗目前仍效力利雅得胜利，并继续代表葡萄牙队参赛。", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "需要审批" })).toHaveCount(0);
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
  await page.screenshot({ path: "artifacts/minicode-web-current-news-stop.png", fullPage: true });
});

test("selects Standard or Ultra beside the composer and sends the chosen profile", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop execution-profile test");

  await page.route("**/api/runs", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      run: {
        runId: "profile-run",
        sessionId: "profile-session",
        workspace: "C:\\Users\\test\\.mini-code\\standalone",
        promptPreview: "验证 Ultra",
        status: "running",
        createdAt: Date.now(),
        updatedAt: Date.now(),
        eventCount: 0,
        scope: "standalone",
        executionProfile: "ultra",
      },
    }),
  }));
  await page.route("**/api/runs/profile-run/events", (route) => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: { "Cache-Control": "no-cache" },
    body: `data: ${JSON.stringify({
      kind: "status",
      sequence: 1,
      timestamp: Date.now(),
      status: "completed",
      content: "任务已结束",
    })}\n\n`,
  }));

  await page.goto("/");
  const selector = page.getByRole("button", { name: /^执行模式：(Standard|Ultra)$/ });
  await expect(selector).toBeVisible();
  expect((await selector.boundingBox())?.height || 0).toBeGreaterThanOrEqual(44);

  await selector.focus();
  await selector.press("Enter");
  const menu = page.getByRole("listbox", { name: "选择执行模式" });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("option")).toHaveCount(2);
  await expect(menu.getByText("Deep", { exact: true })).toHaveCount(0);
  await page.screenshot({
    path: "artifacts/minicode-web-execution-profile-selector.png",
    fullPage: true,
  });
  await menu.getByRole("option", { name: /Ultra/ }).click();
  await expect(page.getByRole("button", { name: "执行模式：Ultra" })).toBeVisible();

  const requestPromise = page.waitForRequest(
    (request) => request.url().endsWith("/api/runs") && request.method() === "POST",
  );
  const composer = page.getByLabel("任务内容");
  await composer.fill("验证 Ultra");
  await composer.press("Enter");
  const request = await requestPromise;
  expect(request.postDataJSON()).toMatchObject({ executionProfile: "ultra" });
});

test("coalesces duplicate submissions dispatched in the same browser task", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop submission race test");
  let submissions = 0;

  await page.route(
    (url) => url.pathname === "/api/runs",
    async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue();
        return;
      }
      submissions += 1;
      await new Promise((resolve) => setTimeout(resolve, 150));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run: {
            runId: "single-submit-run",
            sessionId: "single-submit-session",
            workspace: "C:\\Users\\test\\.mini-code\\standalone",
            promptPreview: "只提交一次",
            status: "completed",
            createdAt: Date.now(),
            updatedAt: Date.now(),
            eventCount: 0,
            scope: "standalone",
            executionProfile: "standard",
          },
        }),
      });
    },
  );

  await page.goto("/");
  await page.getByLabel("任务内容").fill("只提交一次");
  await page.evaluate(() => {
    const button = document.querySelector<HTMLButtonElement>(
      'button[aria-label="发送任务"]',
    );
    if (!button) throw new Error("send button not found");
    button.click();
    button.click();
  });

  await expect.poll(() => submissions).toBe(1);
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
});

test("does not let a stale run-start response replace a newly selected conversation", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop stale-response test");
  const now = Date.now();
  const existing = {
    sessionId: "current-selection",
    createdAt: now - 4_000,
    updatedAt: now - 2_000,
    firstMessage: "保留当前会话",
    lastMessage: "当前会话回答。",
    messageCount: 2,
    workspace: "C:\\Users\\test\\.mini-code\\standalone",
    scope: "standalone",
    projectPath: null,
    model: "mock",
    status: "completed",
    toolCallCount: 0,
  };
  const navigation = {
    projects: [],
    sessions: [existing],
    pinnedSessionIds: [],
    pinnedWorkspacePaths: [],
  };

  await page.route("**/api/navigation", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(navigation),
  }));
  await page.route(
    (url) => url.pathname === "/api/sessions/current-selection",
    (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        session: existing,
        messages: [
          { role: "user", content: existing.firstMessage },
          { role: "assistant", content: existing.lastMessage },
        ],
      }),
    }),
  );
  await page.route("**/api/sessions/current-selection/run", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ run: null, events: [] }),
  }));
  await page.route("**/api/sessions/current-selection/inspect", (route) => route.fulfill({
    status: 404,
    contentType: "application/json",
    body: JSON.stringify({ detail: "not persisted in this fixture" }),
  }));
  await page.route(
    (url) => url.pathname === "/api/runs",
    async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run: {
            runId: "stale-start-run",
            sessionId: "stale-start-session",
            workspace: existing.workspace,
            promptPreview: "延迟启动",
            status: "running",
            createdAt: now,
            updatedAt: now,
            eventCount: 0,
            scope: "standalone",
            executionProfile: "standard",
          },
        }),
      });
    },
  );
  await page.route("**/api/runs/stale-start-run/events*", (route) => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: `data: ${JSON.stringify({
      kind: "assistant",
      sequence: 1,
      timestamp: now,
      content: "陈旧启动响应不应出现。",
    })}\n\n`,
  }));

  await page.goto("/");
  await page.getByLabel("任务内容").fill("延迟启动");
  await page.getByRole("button", { name: "发送任务" }).click();
  const rail = page.getByRole("complementary", { name: "项目和对话" });
  await rail.locator(".rail-item-main").filter({ hasText: existing.firstMessage }).click();
  await expect(page.getByText(existing.lastMessage, { exact: true })).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.getByText("陈旧启动响应不应出现。", { exact: true })).toHaveCount(0);
  await expect(page.getByText(existing.lastMessage, { exact: true })).toBeVisible();
});

test("keeps the accepted Ultra answer after switching conversations", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop Ultra persistence test");
  const now = Date.now();
  const sessions = [
    {
      sessionId: "profile-session",
      createdAt: now - 2_000,
      updatedAt: now,
      firstMessage: "Ultra 简单问题",
      lastMessage: "Ultra 简单回答保留。",
      messageCount: 2,
      workspace: "C:\\isolated-test-home\\.mini-code\\standalone",
      scope: "standalone",
      projectPath: null,
      model: "deepseek-v4-pro",
      status: "completed",
      toolCallCount: 0,
    },
    {
      sessionId: "other-session",
      createdAt: now - 4_000,
      updatedAt: now - 3_000,
      firstMessage: "另一个问题",
      lastMessage: "另一个回答。",
      messageCount: 2,
      workspace: "C:\\isolated-test-home\\.mini-code\\standalone",
      scope: "standalone",
      projectPath: null,
      model: "deepseek-v4-pro",
      status: "completed",
      toolCallCount: 0,
    },
  ];

  await page.route("**/api/navigation", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      projects: [],
      sessions,
      pinnedSessionIds: [],
      pinnedWorkspacePaths: [],
    }),
  }));
  await page.route("**/api/sessions/profile-session", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      session: sessions[0],
      messages: [
        { role: "user", content: "Ultra 简单问题" },
        { role: "assistant", content: "Ultra 简单回答保留。" },
      ],
    }),
  }));
  await page.route("**/api/sessions/other-session", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      session: sessions[1],
      messages: [
        { role: "user", content: "另一个问题" },
        { role: "assistant", content: "另一个回答。" },
      ],
    }),
  }));
  await page.route("**/api/runs", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      run: {
        runId: "persisted-ultra-run",
        sessionId: "profile-session",
        workspace: sessions[0].workspace,
        promptPreview: "Ultra 简单问题",
        status: "running",
        createdAt: now,
        updatedAt: now,
        eventCount: 0,
        scope: "standalone",
        executionProfile: "ultra",
      },
    }),
  }));
  await page.route("**/api/runs/persisted-ultra-run/events", (route) => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: { "Cache-Control": "no-cache" },
    body: [
      `data: ${JSON.stringify({ kind: "assistant", sequence: 1, timestamp: now, content: "Ultra 简单回答保留。" })}\n\n`,
      `data: ${JSON.stringify({ kind: "done", sequence: 2, timestamp: now, stopReason: "done" })}\n\n`,
      `data: ${JSON.stringify({ kind: "status", sequence: 3, timestamp: now, status: "completed", content: "任务已结束" })}\n\n`,
    ].join(""),
  }));

  await page.goto("/");
  const profileButton = page.getByRole("button", { name: /^执行模式：(Standard|Ultra)$/ });
  if ((await profileButton.getAttribute("aria-label")) !== "执行模式：Ultra") {
    await profileButton.click();
    await page.getByRole("option", { name: /Ultra/ }).click();
  }
  await page.getByLabel("任务内容").fill("Ultra 简单问题");
  await page.getByLabel("任务内容").press("Enter");
  await expect(page.getByText("Ultra 简单回答保留。", { exact: true })).toHaveCount(1);
  await expect(page.getByText(/Runtime completion gate rejected delivery/)).toHaveCount(0);

  const rail = page.getByRole("complementary", { name: "项目和对话" });
  await rail.locator(".rail-item-main").filter({ hasText: "另一个问题" }).click();
  await expect(page.getByText("另一个回答。", { exact: true })).toBeVisible();
  await rail.locator(".rail-item-main").filter({ hasText: "Ultra 简单问题" }).click();
  await expect(page.getByText("Ultra 简单回答保留。", { exact: true })).toHaveCount(1);
  await expect(page.getByText(/Runtime completion gate rejected delivery/)).toHaveCount(0);
});

test("configures a provider without exposing the key and completes a browser task", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop workflow test");
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.continue();
      return;
    }
    const payload = route.request().postDataJSON() as { model: string };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        provider: "deepseek",
        model: payload.model,
        baseUrl: "https://api.deepseek.com",
        baseUrlSource: "settings",
        apiKeyConfigured: true,
        apiKeyMask: "••••••••1234",
        apiKeySource: "settings",
        thinkingMode: "enabled",
        reasoningEffort: "high",
        executionProfile: "standard",
        settingsPath: "C:\\isolated-test-home\\.mini-code\\settings.json",
        providers: [
          {
            id: "deepseek",
            label: "DeepSeek",
            defaultBaseUrl: "https://api.deepseek.com",
            models: [
              "deepseek-v4-pro",
              "deepseek-v4-flash",
              "deepseek-v4-flash-vision-exp",
            ],
          },
        ],
      }),
    });
  });
  await page.route("**/api/runs", async (route) => {
    const payload = route.request().postDataJSON() as { executionProfile: "standard" | "ultra" };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        run: {
          runId: "browser-workflow-run",
          sessionId: "browser-workflow-session",
          workspace: "C:\\isolated-test-home\\.mini-code\\standalone",
          promptPreview: "hello from browser",
          status: "running",
          createdAt: Date.now(),
          updatedAt: Date.now(),
          eventCount: 0,
          scope: "standalone",
          executionProfile: payload.executionProfile,
        },
      }),
    });
  });
  await page.route("**/api/runs/browser-workflow-run/events", (route) => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: { "Cache-Control": "no-cache" },
    body: [
      `data: ${JSON.stringify({ kind: "assistant", sequence: 1, timestamp: Date.now(), content: "This is a minimal MiniCode Python shell." })}\n\n`,
      `data: ${JSON.stringify({ kind: "status", sequence: 2, timestamp: Date.now(), status: "completed", content: "任务已结束" })}\n\n`,
    ].join(""),
  }));

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "开始独立对话" })).toBeVisible();

  await page.getByRole("button", { name: "模型与 API" }).click();
  await expect(page.getByRole("dialog", { name: "模型与 API" })).toBeVisible();
  const secret = "sk-playwright-should-not-leak-1234";
  await page.getByLabel("模型提供商").selectOption("deepseek");
  const modelSelect = page.getByLabel("模型 ID");
  await expect(modelSelect.getByRole("option", { name: "deepseek-v4-flash", exact: true })).toBeAttached();
  await expect(modelSelect.getByRole("option", { name: "deepseek-v4-flash-vision-exp", exact: true })).toBeAttached();
  await modelSelect.selectOption("deepseek-v4-flash");
  await page.getByLabel("Base URL").fill("https://api.deepseek.com");
  await page.getByLabel("API Key").fill(secret);
  const settingsRequestPromise = page.waitForRequest(
    (request) => request.url().endsWith("/api/settings") && request.method() === "PUT",
  );
  await page.getByRole("button", { name: "保存设置" }).click();
  const settingsRequest = await settingsRequestPromise;
  expect(settingsRequest.postDataJSON()).toMatchObject({ model: "deepseek-v4-flash" });
  await expect(page.getByText("设置已保存到本机。")).toBeVisible();
  await expect(page.getByLabel("API Key")).toHaveValue("");
  await expect(page.locator("body")).not.toContainText(secret);
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual([]);
  await page.getByRole("button", { name: "关闭设置" }).last().click();

  const composer = page.getByLabel("任务内容");
  await composer.fill("hello from browser");
  await composer.press("Enter");
  await expect(page.locator("#main-content").getByText("hello from browser", { exact: true })).toBeVisible();
  await expect(page.getByText("This is a minimal MiniCode Python shell.")).toBeVisible({ timeout: 25_000 });
  await expect(page.getByText("已完成", { exact: true })).toBeVisible({ timeout: 10_000 });

  await page.screenshot({ path: "artifacts/minicode-web-desktop.png", fullPage: true });
  expect(consoleErrors).toEqual([]);
});

test("keeps the workbench usable at the required narrow widths", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "Mobile layout test");
  const widths = [320, 375, 414, 768];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/");
    await expect(page.locator("#main-content").getByRole("heading").first()).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `horizontal overflow at ${width}px`).toBeLessThanOrEqual(0);
  }

  await page.setViewportSize({ width: 320, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "打开检查器" }).click();
  const narrowInspector = page.locator("#workspace-inspector");
  await narrowInspector.getByRole("tab", { name: "能力" }).click();
  await expect(narrowInspector.getByRole("heading", { name: "工具面" })).toBeVisible({ timeout: 15_000 });
  expect(await narrowInspector.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0);
  await narrowInspector.getByRole("button", { name: "关闭检查器" }).click();

  await page.setViewportSize({ width: 390, height: 844 });
  const executionProfile = page.getByRole("button", { name: /^执行模式：(Standard|Ultra)$/ });
  const profileBox = await executionProfile.boundingBox();
  expect(profileBox?.height || 0).toBeGreaterThanOrEqual(44);
  expect(profileBox?.x || 0).toBeGreaterThan(160);
  await executionProfile.click();
  await expect(page.getByRole("listbox", { name: "选择执行模式" })).toBeVisible();
  expect(await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )).toBeLessThanOrEqual(0);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "打开任务栏" }).click();
  await expect(page.getByRole("complementary", { name: "项目和对话" })).toBeVisible();
  await page.getByRole("button", { name: "打开项目" }).click();
  const pickerButton = page.getByRole("button", { name: /选择文件夹/ });
  await expect(pickerButton).toBeVisible();
  const pickerBox = await pickerButton.boundingBox();
  expect(pickerBox?.height || 0).toBeGreaterThanOrEqual(44);
  const openMenuOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(openMenuOverflow).toBeLessThanOrEqual(0);
  await page.keyboard.press("Escape");
  const newTask = page.getByRole("button", { name: "新对话 Ctrl N", exact: true });
  const box = await newTask.boundingBox();
  expect(box?.height || 0).toBeGreaterThanOrEqual(44);
  await page.screenshot({ path: "artifacts/minicode-web-mobile.png", fullPage: true });
});
