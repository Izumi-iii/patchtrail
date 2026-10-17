/* Real Chromium interactions. Reuses an existing Playwright installation. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');
const { once } = require('node:events');
const { chromium } = require(process.env.PATCHTRAIL_PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '../..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'patchtrail-browser-'));
const output = path.join(root, '.test-artifacts', 'webui');
fs.mkdirSync(output, { recursive: true });
const checks = [];
const errors = [];
const repository = path.join(temporary, 'python-sample');
fs.mkdirSync(repository);
function git(...args) {
  return execFileSync('git', ['-C', repository, ...args], { encoding: 'utf8', windowsHide: true }).trim();
}
git('init', '-q');
fs.writeFileSync(path.join(repository, 'calc.py'), 'def add(a, b):\n    return a + b\n');
fs.writeFileSync(path.join(repository, 'test_calc.py'), 'def test_add():\n    assert True\n');
git('add', '.');
git('-c', 'user.name=PatchTrail Test', '-c', 'user.email=test@localhost',
    '-c', `core.hooksPath=${os.devNull}`, 'commit', '-qm', 'fixture');
const head = git('rev-parse', 'HEAD');
const server = spawn(process.env.PATCHTRAIL_PYTHON || 'python',
  ['-u', 'app.py', '--port', '0', '--data-dir', path.join(temporary, 'state')],
  { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
let serverLog = '';
server.stderr.on('data', chunk => { serverLog = (serverLog + chunk).slice(-12000); });

async function run() {
  const url = await new Promise((resolve, reject) => {
    let text = '';
    const timeout = setTimeout(() => reject(new Error('Server did not start')), 15000);
    server.stdout.on('data', chunk => {
      text += chunk;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) { clearTimeout(timeout); resolve(match[0]); }
    });
    server.once('error', reject);
    server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Server exited: ${code}`)); });
  });
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.PATCHTRAIL_BROWSER_PATH || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => !document.getElementById('new-chat').disabled);
    assert.equal(await page.locator('#welcome').isVisible(), true);
    assert.equal(await page.locator('#send').isDisabled(), true);
    assert.match(await page.locator('.capability-note').textContent(), /尚未接入/);
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    checks.push('welcome and honest capability status');

    const firstId = await page.locator('.conversation-item.active').getAttribute('data-id');
    const input = page.locator('#message-input');
    await input.fill('复现步骤');
    await input.press('Shift+Enter');
    assert.match(await input.inputValue(), /\n/);
    await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true });
    assert.equal(await page.locator('.message.user').count(), 0);
    await page.selectOption('#mode-select', 'note');
    const unsafeText = '<img src=x onerror="window.injected=true"> 退款需求';
    await input.fill(unsafeText);
    await input.press('Enter');
    await page.waitForFunction(() => document.querySelectorAll('.message.assistant').length === 1);
    assert.equal(await page.locator('.message.user .message-content').textContent(), unsafeText);
    assert.equal(await page.locator('.message-content img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.equal(await page.locator('.message-preview-label').textContent(), '工作台预览 · 未调用模型');
    checks.push('IME and multiline input; stored text cannot execute HTML');

    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForSelector('.message.assistant');
    assert.equal(await page.locator('.message.user .message-content').textContent(), unsafeText);
    checks.push('messages and selected conversation survive reload');

    await page.click('#rename-chat');
    await page.fill('#rename-input', '退款检查');
    await page.locator('#rename-form [type=submit]').click();
    await page.waitForFunction(() => document.getElementById('conversation-title').textContent === '退款检查');
    await page.click('#repository-button');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#repository-dialog').isVisible(), false);
    assert.equal(await page.locator('#repository-button').evaluate(node => node === document.activeElement), true);
    checks.push('rename and dialog Escape focus restoration');

    await page.click('#repository-button');
    await page.fill('#repository-path', repository);
    await page.click('#save-repository');
    await page.waitForFunction(() => !document.getElementById('repository-dialog').open);
    await page.selectOption('#mode-select', 'inspect');
    await input.fill('检查仓库的提交和测试文件');
    await page.click('#send');
    await page.waitForSelector('.repository-result[data-status="ready"]');
    await page.locator('.repository-result summary').last().click();
    assert.match(await page.locator('.repository-result').last().textContent(), new RegExp(head));
    assert.match(await page.locator('.repository-result').last().textContent(), /test_calc.py/);
    assert.equal(git('status', '--porcelain'), '');
    checks.push('real repository metadata; original worktree unchanged');

    await page.click('#repository-button');
    await page.fill('#repository-path', path.join(temporary, 'does-not-exist'));
    await page.click('#save-repository');
    await page.waitForFunction(() => !document.getElementById('repository-dialog').open);
    await input.fill('检查无效路径');
    await input.press('Enter');
    await page.waitForSelector('.repository-result[data-status="invalid"]');
    assert.match(await page.locator('.message.assistant').last().textContent(), /未通过/);
    checks.push('invalid repository shown as failure, not a fake success');

    await page.click('#new-chat');
    await page.waitForFunction(id => document.querySelector('.conversation-item.active')?.dataset.id !== id, firstId);
    const secondId = await page.locator('.conversation-item.active').getAttribute('data-id');
    assert.equal(await page.locator('#welcome').isVisible(), true);
    assert.equal(await page.locator('.message').count(), 0);
    checks.push('new conversations start with isolated histories');

    let releaseLoad;
    let loadEntered;
    const loadGate = new Promise(resolve => { releaseLoad = resolve; });
    const loadStarted = new Promise(resolve => { loadEntered = resolve; });
    const firstRoute = `**/api/conversations/${firstId}`;
    await page.route(firstRoute, async route => {
      const response = await route.fetch(); loadEntered();
      await loadGate;
      try { await route.fulfill({ response }); } catch { /* Aborted on switch. */ }
    });
    await page.locator(`[data-id="${firstId}"]`).click();
    await loadStarted;
    await page.locator(`[data-id="${secondId}"]`).click();
    releaseLoad();
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('.conversation-item.active').getAttribute('data-id'), secondId);
    assert.equal(await page.locator('.message').count(), 0);
    await page.unroute(firstRoute);
    checks.push('late conversation fetch cannot replace current selection');

    let releaseReply;
    let replyEntered;
    const replyGate = new Promise(resolve => { releaseReply = resolve; });
    const replyStarted = new Promise(resolve => { replyEntered = resolve; });
    const secondMessages = `**/api/conversations/${secondId}/messages`;
    await page.route(secondMessages, async route => {
      const response = await route.fetch(); replyEntered();
      await replyGate; await route.fulfill({ response });
    });
    await page.selectOption('#mode-select', 'note');
    await input.fill('这个回复属于第二个会话');
    await page.click('#send');
    await replyStarted;
    assert.equal(await page.locator('#send').isDisabled(), true);
    assert.equal(await page.locator('#pending').isVisible(), true);
    await page.click('#new-chat');
    await page.waitForFunction(id => document.querySelector('.conversation-item.active')?.dataset.id !== id, secondId);
    const thirdId = await page.locator('.conversation-item.active').getAttribute('data-id');
    await input.fill('第三个会话还没发出的草稿');
    releaseReply();
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('.conversation-item.active').getAttribute('data-id'), thirdId);
    assert.equal(await input.inputValue(), '第三个会话还没发出的草稿');
    assert.equal(await page.locator('.message').count(), 0);
    await page.unroute(secondMessages);
    await page.locator(`[data-id="${secondId}"]`).click();
    await page.waitForSelector('.message.assistant');
    assert.equal(await page.locator('.message.user').count(), 1);
    await page.locator(`[data-id="${thirdId}"]`).click();
    await page.waitForFunction(() => document.getElementById('message-input').value === '第三个会话还没发出的草稿');
    checks.push('in-flight reply isolated from another conversation and draft');

    const thirdMessages = `**/api/conversations/${thirdId}/messages`;
    await page.route(thirdMessages, route => route.fulfill({ status: 503,
      contentType: 'application/json', body: JSON.stringify({ error: '测试中的服务异常' }) }));
    await input.fill('请求失败后保留这个需求');
    await page.click('#send');
    await page.waitForFunction(() => document.getElementById('message-input').value === '请求失败后保留这个需求');
    assert.match(await page.locator('#toast').textContent(), /服务异常/);
    assert.equal(await page.locator('.message.user').count(), 0);
    await page.unroute(thirdMessages);
    await page.click('#send');
    await page.waitForSelector('.message.assistant');
    checks.push('server error preserves input and supports retry');

    await page.route(thirdMessages, async route => {
      await route.fetch(); // Server commits successfully; the response is lost.
      await route.abort('failed');
    });
    await input.fill('响应丢失时不要重复保存');
    await page.click('#send');
    await page.waitForFunction(() => document.getElementById('message-input').value === '响应丢失时不要重复保存');
    await page.unroute(thirdMessages);
    await page.click('#send');
    await page.waitForFunction(() => document.querySelectorAll('.message.assistant').length === 2 &&
      document.getElementById('pending').hidden);
    assert.equal(await page.locator('.message.assistant').count(), 2);
    checks.push('lost-response retry is idempotent');

    const downloadReady = page.waitForEvent('download');
    await page.click('#export-chat');
    const download = await downloadReady;
    const exportPath = path.join(output, 'conversation.json');
    await download.saveAs(exportPath);
    assert.equal(JSON.parse(fs.readFileSync(exportPath, 'utf8')).messages.length, 4);
    checks.push('JSON export contains actual persisted conversation');

    await page.fill('#search', '退款检查');
    assert.equal(await page.locator('.conversation-item').count(), 1);
    await page.fill('#search', '不存在的关键词');
    assert.equal(await page.locator('.conversation-item').count(), 0);
    assert.match(await page.locator('.empty-list').textContent(), /没有匹配/);
    await page.fill('#search', '');
    await page.click('#delete-chat');
    await page.locator('#delete-form [data-close-dialog]').last().click();
    assert.equal(await page.locator('.message.user').count(), 2);
    await page.click('#delete-chat');
    await page.locator('#delete-form [type=submit]').click();
    await page.waitForFunction(id => !document.querySelector(`[data-id="${id}"]`), thirdId);
    checks.push('search and explicit delete confirmation');

    await page.setViewportSize({ width: 390, height: 844 });
    await page.click('#menu-toggle');
    assert.equal(await page.locator('#sidebar').isVisible(), true);
    await page.click('#sidebar-backdrop', { position: { x: 370, y: 350 } });
    assert.equal(await page.locator('#sidebar').evaluate(node => node.inert), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.waitForFunction(() => document.getElementById('toast').hidden);
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    checks.push('mobile sidebar, keyboard inertness, and no horizontal overflow');

    assert.deepEqual(errors, []);
    checks.push('no uncaught browser JavaScript errors');
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ passed: checks.length, checks, errors }, null, 2));
    console.log(JSON.stringify({ passed: checks.length, checks, artifacts: output }, null, 2));
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  console.error(error);
  console.error(serverLog);
  process.exitCode = 1;
}).finally(async () => {
  if (server.exitCode === null) {
    const exited = once(server, 'exit'); server.kill(); await exited;
  }
  fs.rmSync(temporary, { recursive: true, force: true });
});
