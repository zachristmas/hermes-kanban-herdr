// Needs: a dashboard with kanban-herdr enabled and HERDR_SOCKET_PATH pointing at the
// fake server (python3 tests/fake_herdr.py <sock>), and FAKE_HERDR_SOCK set to the same path.
// HERMES_TASK defaults to the sandbox task.
//
// Close/reopen (khd-close / khd-reopen) are NOT covered here: close_pane()/reopen_session() go
// through the herdr CLI directly (pane.close / tab.create / agent.start / agent.get -- see
// plugin_api.py's module docstring), which means the dashboard process this harness launches
// would need HERDR_BIN pointed at tests/stub_herdr_cli.py, not just HERDR_SOCKET_PATH -- that's
// how the dashboard is *started*, outside this spec file's control. tests/test_api.py exercises
// close/reopen directly instead (including a real stub-CLI round trip). Noted gap, not fixed here.
import net from "node:net";
import { test, expect } from "@playwright/test";

const TASK = process.env.HERMES_TASK || "t_3873db8c";
const SOCK = process.env.FAKE_HERDR_SOCK;

function fake(method, params) {
  return new Promise((resolve, reject) => {
    const s = net.createConnection(SOCK);
    let buf = "";
    s.on("data", (d) => { buf += d; if (buf.includes("\n")) { s.end(); resolve(JSON.parse(buf.split("\n")[0])); } });
    s.on("error", reject);
    s.write(JSON.stringify({ id: "e2e", method, params }) + "\n");
  });
}

async function open(page) {
  await page.goto("/kanban");
  await page.locator(`[data-task-id="${TASK}"]`).first().click();
  await page.locator('[data-testid="khd-list"]').waitFor();
}

test.beforeEach(async () => {
  expect(SOCK, "FAKE_HERDR_SOCK must be set").toBeTruthy();
  await fake("fake.reset", {});
  await fake("fake.add", { pane_id: "w1:p1", title: "claude cic-1", agent: "claude", status: "working", cwd: "/work/a", tokens: { kanban_task: TASK }, text: "line one\nline two\n" });
  await fake("fake.add", { pane_id: "w1:p2", title: "codex helper", agent: "codex", status: "idle", cwd: "/work/b", tokens: { kanban_task: TASK }, text: "second pane output\n" });
  await fake("fake.add", { pane_id: "w2:p1", title: "unrelated shell", status: "idle", cwd: "/home", text: "$ \n" });
});

test("lists tagged panes and not unrelated ones", async ({ page }) => {
  await open(page);
  await expect(page.locator('[data-testid="khd-pane"]')).toHaveCount(2);
  await expect(page.locator(".khd-section .hermes-kanban-section-head")).toContainText("(2)");
  await expect(page.locator('[data-testid="khd-pane"][data-pane-id="w2:p1"]')).toHaveCount(0);
});

test("accordion opens one pane and streams new output live", async ({ page }) => {
  await open(page);
  const first = page.locator('[data-testid="khd-pane"][data-pane-id="w1:p1"]');
  await first.locator('[data-testid="khd-pane-head"]').click();
  const term = first.locator('[data-testid="khd-term"]');
  await expect(term).toContainText("line two");
  await expect(first.locator('[data-testid="khd-live"]')).toHaveAttribute("data-state", "live");
  await fake("fake.append", { pane_id: "w1:p1", text: "appended live\n" });
  await expect(term).toContainText("appended live", { timeout: 4000 });
});

test("opening a second pane closes the first", async ({ page }) => {
  await open(page);
  const a = page.locator('[data-testid="khd-pane"][data-pane-id="w1:p1"]');
  const b = page.locator('[data-testid="khd-pane"][data-pane-id="w1:p2"]');
  await a.locator('[data-testid="khd-pane-head"]').click();
  await expect(a.locator('[data-testid="khd-term"]')).toBeVisible();
  await b.locator('[data-testid="khd-pane-head"]').click();
  await expect(b.locator('[data-testid="khd-term"]')).toContainText("second pane output");
  await expect(a.locator('[data-testid="khd-term"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="khd-term"]')).toHaveCount(1);
});

test("viewer is read-only (no input controls)", async ({ page }) => {
  await open(page);
  const a = page.locator('[data-testid="khd-pane"][data-pane-id="w1:p1"]');
  await a.locator('[data-testid="khd-pane-head"]').click();
  await expect(a.locator('[data-testid="khd-term"]')).toBeVisible();
  expect(await a.locator("input, textarea, [contenteditable=true]").count()).toBe(0);
});

test("link a pane through the picker, then unlink it", async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await open(page);
  await page.locator('[data-testid="khd-link"]').click();
  const pick = page.locator('[data-testid="khd-pick"][data-pane-id="w2:p1"]');
  await expect(pick).toBeVisible();
  await pick.click();
  await expect(page.locator('[data-testid="khd-pane"][data-pane-id="w2:p1"]')).toBeVisible();
  await expect(page.locator('[data-testid="khd-pane"]')).toHaveCount(3);
  const linked = page.locator('[data-testid="khd-pane"][data-pane-id="w2:p1"]');
  await linked.locator('[data-testid="khd-pane-head"]').click();
  await linked.locator('[data-testid="khd-unlink"]').click();
  await expect(page.locator('[data-testid="khd-pane"][data-pane-id="w2:p1"]')).toHaveCount(0);
});

test("a closed pane moves to history and the open view reports it", async ({ page }) => {
  await open(page);
  const a = page.locator('[data-testid="khd-pane"][data-pane-id="w1:p1"]');
  await a.locator('[data-testid="khd-pane-head"]').click();
  await expect(a.locator('[data-testid="khd-live"]')).toHaveAttribute("data-state", "live");
  await fake("fake.close", { pane_id: "w1:p1" });
  await fake("fake.append", { pane_id: "w1:p2", text: "poke\n" });
  await expect(page.locator('[data-testid="khd-closed"]')).toHaveCount(1, { timeout: 12000 });
});

test("phone width: section fits and pane opens", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await open(page);
  const a = page.locator('[data-testid="khd-pane"][data-pane-id="w1:p1"]');
  await a.scrollIntoViewIfNeeded();
  await a.locator('[data-testid="khd-pane-head"]').click();
  await expect(a.locator('[data-testid="khd-term"]')).toContainText("line one");
  const over = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  expect(over).toBe(false);
});

// Needs a second fake Herdr registered as a saved machine named "Remote box" (see README, "Testing").
test("panes on another saved machine can be linked, labelled and streamed", async ({ page }) => {
  test.skip(!process.env.HERDR_E2E_REMOTE, "set HERDR_E2E_REMOTE=1 with a saved machine configured");
  page.on("dialog", (d) => d.accept());
  await open(page);
  await page.locator('[data-testid="khd-link"]').click();
  const pick = page.locator('[data-testid="khd-pick"]').filter({ hasText: "Remote box" }).first();
  await expect(pick).toBeVisible();
  await expect(pick).toContainText("windows exec");
  await pick.click();
  const row = page.locator('[data-testid="khd-pane"]').filter({ hasText: "windows exec" });
  await expect(row).toBeVisible();
  await expect(row).toContainText("Remote box");
  await row.locator('[data-testid="khd-pane-head"]').click();
  await expect(row.locator('[data-testid="khd-term"]')).toContainText("remote one", { timeout: 10000 });
  await expect(row.locator('[data-testid="khd-live"]')).toHaveAttribute("data-state", "live");
  await row.locator('[data-testid="khd-unlink"]').click();
  await expect(row).toHaveCount(0);
});

test("scrolling up is not overridden by new output, and the reader's place is kept", async ({ page }) => {
  const rows = Array.from({ length: 120 }, (_, i) => `row ${String(i).padStart(3, "0")} of the log`).join("\n") + "\n";
  await fake("fake.append", { pane_id: "w1:p1", text: rows });
  await open(page);
  const a = page.locator('[data-testid="khd-pane"][data-pane-id="w1:p1"]');
  await a.locator('[data-testid="khd-pane-head"]').click();
  const term = a.locator('[data-testid="khd-term"]');
  await expect(term).toContainText("row 119");
  await expect.poll(() => term.evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight)).toBeLessThan(4); // follows at the bottom
  await term.evaluate((e) => { e.scrollTop = 120; });
  await expect(a.locator('[data-testid="khd-jump"]')).toBeVisible();
  for (let i = 0; i < 5; i++) {
    await fake("fake.append", { pane_id: "w1:p1", text: `new line ${i}\n` });
    await page.waitForTimeout(350);
  }
  await expect(term).toContainText("new line 4");
  expect(await term.evaluate((e) => e.scrollTop)).toBe(120);
  await a.locator('[data-testid="khd-jump"]').click();
  await expect.poll(() => term.evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight)).toBeLessThan(4);
  await expect(a.locator('[data-testid="khd-jump"]')).toBeHidden();
});

test("when old lines scroll off the top the reader keeps the same text in view", async ({ page }) => {
  const rows = Array.from({ length: 560 }, (_, i) => `row ${String(i).padStart(3, "0")} of the log`).join("\n") + "\n";
  await fake("fake.append", { pane_id: "w1:p1", text: rows }); // more than the 500-line window
  await open(page);
  const a = page.locator('[data-testid="khd-pane"][data-pane-id="w1:p1"]');
  await a.locator('[data-testid="khd-pane-head"]').click();
  const term = a.locator('[data-testid="khd-term"]');
  await expect(term).toContainText("row 559");
  await term.evaluate((e) => { e.scrollTop = 2000; });
  const topRow = () => term.evaluate((e) => {
    const kids = Array.from(e.children);
        const hit = kids.find((k) => k.getBoundingClientRect().bottom > e.getBoundingClientRect().top + 4);
    return hit ? hit.textContent : null;
  });
  const before = await topRow();
  await fake("fake.append", { pane_id: "w1:p1", text: "extra a\nextra b\nextra c\n" });
  await expect(term).toContainText("extra c");
  expect(await topRow()).toBe(before);
});

test("long lines wrap instead of scrolling sideways", async ({ page }) => {
  const long = "word ".repeat(120).trim();
  await fake("fake.append", { pane_id: "w1:p1", text: long + "\n" });
  await open(page);
  const a = page.locator('[data-testid="khd-pane"][data-pane-id="w1:p1"]');
  await a.locator('[data-testid="khd-pane-head"]').click();
  const term = a.locator('[data-testid="khd-term"]');
  await expect(term).toContainText("word word word");
  const m = await term.evaluate((e) => ({ sw: e.scrollWidth, cw: e.clientWidth, h: e.querySelector(".khd-line:nth-last-child(2)").getBoundingClientRect().height }));
  expect(m.sw).toBeLessThanOrEqual(m.cw + 1);
  expect(m.h).toBeGreaterThan(40); // the long line took several rows
});
