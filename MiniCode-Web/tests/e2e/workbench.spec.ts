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

test("pins and unpins a conversation from the global rail", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop navigation test");

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
      `data: ${JSON.stringify({ kind: "thinking_status", sequence: 2, timestamp: Date.now(), content: "正在分析任务和工作区上下文" })}\n\n`,
      `data: ${JSON.stringify({ kind: "assistant", sequence: 3, timestamp: Date.now(), content: "状态检查完成。" })}\n\n`,
      `data: ${JSON.stringify({ kind: "status", sequence: 4, timestamp: Date.now(), status: "completed", content: "任务已结束" })}\n\n`,
    ].join(""),
  }));

  await page.goto("/");
  const composer = page.getByLabel("任务内容");
  await composer.fill("检查状态");
  await composer.press("Enter");
  await expect(page.getByText("分析任务和工作区上下文完成", { exact: true })).toBeVisible();
  await expect(page.getByText("正在分析任务和工作区上下文", { exact: true })).toHaveCount(0);
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
});

test("configures a provider without exposing the key and completes a browser task", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop workflow test");
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "开始独立对话" })).toBeVisible();

  await page.getByRole("button", { name: "模型与 API" }).click();
  await expect(page.getByRole("dialog", { name: "模型与 API" })).toBeVisible();
  const secret = "sk-playwright-should-not-leak-1234";
  await page.getByLabel("模型提供商").selectOption("deepseek");
  await page.getByLabel("模型 ID").fill("deepseek-v4-pro");
  await page.getByLabel("Base URL").fill("https://api.deepseek.com");
  await page.getByLabel("API Key").fill(secret);
  await page.getByRole("button", { name: "保存设置" }).click();
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

  await page.setViewportSize({ width: 390, height: 844 });
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
