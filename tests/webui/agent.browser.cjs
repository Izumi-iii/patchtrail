/* Actual browser + actual subprocess Agent, with a test-only scripted model. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { chromium } = require(process.env.PATCHTRAIL_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const base = path.join(root, '.test-artifacts', 'b');
fs.mkdirSync(base, { recursive: true });
const temporary = fs.mkdtempSync(path.join(base, 'w-'));
const output = path.join(root, '.test-artifacts', 'web-agent');
fs.mkdirSync(output, { recursive: true });
const python = process.env.PATCHTRAIL_PYTHON || path.join(root, '.venv', 'Scripts', 'python.exe');
let server;
let port;
let browser;
let serverLog = '';
const errors = [];
const checks = [];
async function start(existingPort = 0) {
  server = spawn(python, ['-u', 'tests/webui/agent_server.py', '--data', temporary, '--port', String(existingPort)],
    { cwd: root, windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  server.stderr.on('data', data => { serverLog = (serverLog + data).slice(-10000); });
  const url = await new Promise((resolve, reject) => {
    let text = '';
    const timer = setTimeout(() => reject(new Error('Server startup timeout')), 15000);
    server.stdout.on('data', data => { text += data; const match = text.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) { clearTimeout(timer); resolve(match[0]); } });
    server.once('exit', code => { clearTimeout(timer); reject(new Error(`Server exit ${code}`)); });
  });
  port = Number(new URL(url).port);
  return url;
}
async function stop() {
  if (server && server.exitCode === null) { const exited = once(server, 'exit'); server.kill(); await exited; }
}
async function run() {
  let url = await start();
  browser = await chromium.launch({ headless: true, executablePath: process.env.PATCHTRAIL_BROWSER_PATH || undefined });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.waitForFunction(() => !document.getElementById('new-chat').disabled);
  assert.equal(await page.inputValue('#mode-select'), 'repair');
  await page.screenshot({ path: path.join(output, 'welcome.png'), fullPage: true });
  await page.fill('#message-input', '修复退款金额：应为原价减去优惠，保持无优惠行为。');
  await page.click('#send');
  const card = page.locator('.job-card');
  await card.waitFor();
  await card.getByRole('button', { name: '暂停执行', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.job-card')?.dataset.state === 'SUSPENDED', null, { timeout: 30000 });
  checks.push('repair submission and cooperative pause use a real subprocess');
  await stop();
  url = await start(port);
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelector('.job-card')?.dataset.state === 'SUSPENDED');
  await card.getByRole('button', { name: '继续执行', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.job-card')?.dataset.state === 'WAITING_APPROVAL', null, { timeout: 60000 });
  checks.push('web server restart preserves paused job and resumes the same Agent checkpoint');
  assert.equal(await card.getByRole('button', { name: '批准补丁', exact: true }).isDisabled(), true);
  await card.getByRole('button', { name: '查看补丁', exact: true }).click();
  await page.locator('#diff-dialog').waitFor({ state: 'visible' });
  assert.match(await page.textContent('#diff-content'), /original - discount/);
  assert.match(await page.textContent('#diff-target'), /repository/);
  await page.screenshot({ path: path.join(output, 'diff.png'), fullPage: true });
  await page.getByRole('button', { name: '完成查看', exact: true }).click();
  await card.getByRole('button', { name: '批准补丁', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.job-card')?.dataset.state === 'APPROVED');
  const jobId = await card.getAttribute('data-job-id');
  const detail = await page.evaluate(async id => (await fetch(`/api/jobs/${id}`)).json(), jobId);
  const target = path.join(detail.repository_path, 'shop/refund.py');
  assert.match(fs.readFileSync(target, 'utf8'), /return original\n/);
  await card.getByRole('button', { name: '应用已批准补丁', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.job-card')?.dataset.state === 'APPLIED');
  assert.match(fs.readFileSync(target, 'utf8'), /return original - discount/);
  checks.push('diff review enables approval; approval and actual file application remain separate');
  await page.screenshot({ path: path.join(output, 'applied.png'), fullPage: true });
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelector('.job-card')?.dataset.state === 'APPLIED');
  assert.equal(await page.locator('.message.user').count(), 1);
  checks.push('reload preserves results without duplicate submissions');
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
  checks.push('mobile conversation has no horizontal overflow');
  assert.deepEqual(errors, []);
  const calls = fs.existsSync(path.join(temporary, 'scripted-calls.jsonl')) ?
    fs.readFileSync(path.join(temporary, 'scripted-calls.jsonl'), 'utf8').trim().split('\n').length : 0;
  assert.equal(calls, 3);
  checks.push('exactly three scripted calls, no paid model and no uncaught browser errors');
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ checks, errors, scriptedCalls: calls }, null, 2));
  console.log(JSON.stringify({ passed: checks.length, checks, output }, null, 2));
}
run().catch(error => { console.error(error); console.error(serverLog); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  await stop();
  // Every worker has completed before this owned temporary directory is removed.
  if (!process.exitCode) fs.rmSync(temporary, { recursive: true, force: true });
});
