// Renders the screens a coding agent declared in "ui_captures" from the built portal, with its API mocks, and checks
// the layout. Run by the daemon outside the agent's sandbox, so the agent never starts a server, drives a browser or
// looks at screenshots itself.
//
//   node ui_capture.mjs <spec.json> <out-dir> <playwright-core dir>
//
// spec.json: {"worktree": "/abs", "widths": [360, 1280], "captures": [{name, dist, path, local_storage, mocks, focus}]}
// Exit 0: all screens rendered without layout problems. Exit 1: problems the agent must fix (one per line on stdout).
// Any other exit: the capture itself could not run (browser, install); the daemon reports it as BLOCKED.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const [specPath, outDir, playwrightDir] = process.argv.slice(2);
const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
const { chromium } = createRequire(path.join(playwrightDir, 'noop.js'))('playwright-core');
const ORIGIN = 'http://ui-capture.local';
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.webp': 'image/webp', '.gif': 'image/gif', '.ico': 'image/x-icon', '.json': 'application/json',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.txt': 'text/plain' };

function glob(pattern) {
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*\*/g, '\u0000').replace(/\*/g, '[^/]*');
  return new RegExp('^' + escaped.replace(/\u0000/g, '.*') + '$');
}

async function launch() {
  try {
    return await chromium.launch({ channel: 'chrome', headless: true });  // the installed Google Chrome
  } catch {
    return await chromium.launch({ headless: true });  // Playwright's own Chromium, if installed
  }
}

const problems = [];
const warnings = [];
let browser;
try {
  browser = await launch();
} catch (error) {
  console.error(`Cannot start a browser: ${error.message}`);
  process.exit(3);
}
fs.mkdirSync(outDir, { recursive: true });
const saved = [];

for (const capture of spec.captures) {
  const name = String(capture.name || 'screen').replace(/[^a-z0-9-]+/gi, '-').toLowerCase();
  const dist = path.resolve(spec.worktree, capture.dist || '');
  if (!dist.startsWith(spec.worktree) || !fs.existsSync(path.join(dist, 'index.html'))) {
    problems.push(`${name}: no built portal at ${capture.dist}/index.html (build it as part of your change, or fix "dist")`);
    continue;
  }
  const mocks = (capture.mocks || []).map((mock) => ({ ...mock, pattern: glob(mock.path) }));
  for (const width of spec.widths) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const storage = capture.local_storage || [];
    await page.addInitScript((items) => { for (const item of items) localStorage.setItem(item.key, item.value); }, storage);
    await page.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const mock = mocks.find((m) => (!m.method || m.method === '*' || m.method.toUpperCase() === request.method())
        && m.pattern.test(url.pathname));
      if (request.method() === 'OPTIONS') {
        return route.fulfill({ status: 204, headers: { 'Access-Control-Allow-Origin': ORIGIN,
          'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*', 'Access-Control-Allow-Credentials': 'true' } });
      }
      if (mock) {
        return route.fulfill({ status: mock.status || 200, contentType: mock.content_type || 'application/json',
          headers: { 'Access-Control-Allow-Origin': ORIGIN, 'Access-Control-Allow-Credentials': 'true' }, body: mock.body ?? '' });
      }
      if (url.origin === ORIGIN) {
        let file = path.join(dist, decodeURIComponent(url.pathname));
        if (!file.startsWith(dist) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
          file = path.extname(url.pathname) ? null : path.join(dist, 'index.html');  // SPA route
        }
        if (!file) return route.fulfill({ status: 404, body: '' });
        return route.fulfill({ status: 200, contentType: TYPES[path.extname(file)] || 'application/octet-stream',
          body: fs.readFileSync(file) });
      }
      if (/^(localhost|127\.0\.0\.1|\[::1\])$/.test(url.hostname) || url.pathname.includes('/api/')) {
        warnings.push(`${name}: unmocked ${request.method()} ${url.pathname}`);
        return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
      }
      return route.continue();  // web fonts and verified web images
    });
    try {
      await page.goto(ORIGIN + (capture.path || '/'), { waitUntil: 'networkidle', timeout: 30000 });
    } catch (error) {
      problems.push(`${name} @${width}px: page did not load: ${error.message.split('\n')[0]}`);
      await page.close();
      continue;
    }
    await page.waitForTimeout(300);
    const layout = await page.evaluate(() => {
      const found = [];
      const root = document.documentElement;
      if (root.scrollWidth > window.innerWidth + 1) {
        found.push(`horizontal overflow: page is ${root.scrollWidth}px wide in a ${window.innerWidth}px viewport`);
      }
      const label = (el) => {
        const text = (el.getAttribute('aria-label') || el.textContent || el.getAttribute('name') ||
          el.getAttribute('placeholder') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
        return `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${text ? ` "${text}"` : ''}`;
      };
      const controls = [...document.querySelectorAll('input:not([type=hidden]), select, textarea, button')]
        .filter((el) => { const s = getComputedStyle(el); const r = el.getBoundingClientRect();
          return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0; });
      for (let i = 0; i < controls.length; i++) {
        for (let j = i + 1; j < controls.length; j++) {
          const [a, b] = [controls[i], controls[j]];
          if (a.contains(b) || b.contains(a)) continue;
          const [r, s] = [a.getBoundingClientRect(), b.getBoundingClientRect()];
          const x = Math.min(r.right, s.right) - Math.max(r.left, s.left);
          const y = Math.min(r.bottom, s.bottom) - Math.max(r.top, s.top);
          const inside = (p, q) => p.left >= q.left - 1 && p.right <= q.right + 1 && p.top >= q.top - 1 && p.bottom <= q.bottom + 1;
          if (inside(r, s) || inside(s, r)) continue;  // an adornment such as a show-password button inside an input
          if (x > 1 && y > 1) found.push(`overlapping controls: ${label(a)} and ${label(b)}`);
        }
      }
      return found;
    });
    for (const item of [...layout, ...errors.map((message) => `page error: ${message}`)]) {
      problems.push(`${name} @${width}px: ${item}`);
    }
    const file = path.join(outDir, `${name}-${width}.png`);
    await page.screenshot({ path: file, fullPage: true });
    saved.push(file);
    if (capture.focus && width === Math.max(...spec.widths)) {
      try {
        await page.keyboard.press('Shift');  // keyboard modality first, so :focus-visible applies
        await page.focus(capture.focus, { timeout: 3000 });
        const focused = path.join(outDir, `${name}-focus.png`);
        await page.screenshot({ path: focused });
        saved.push(focused);
      } catch {
        problems.push(`${name}: focus selector ${capture.focus} not found`);
      }
    }
    await page.close();
  }
}
await browser.close();
for (const line of [...new Set(warnings)]) console.log(`warning: ${line}`);
for (const line of problems) console.log(line);
console.log(`SAVED ${JSON.stringify(saved)}`);
process.exit(problems.length ? 1 : 0);
