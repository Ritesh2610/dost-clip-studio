// UI test for Dost Clip Studio. Drives the real page in Chromium against the mock server.
//
//   node tests/e2e/ui.test.mjs            (from the project root)
//   node tests/e2e/ui.test.mjs upload     (only tests whose name contains "upload")
//
// It starts and stops its own mock servers on ports 5412 and 5413. A test fails on any
// Content-Security-Policy violation, uncaught page error or console error, as well as on
// its own assertions.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { CSP, XSS, startMock } from './mock_server.mjs';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const PORT = 5412;
const SECOND_PORT = 5413;
const GOOD_URL = 'https://www.youtube.com/watch?v=mockvideo01';
const PERMISSION_LABEL = 'I made this video, or I have permission from its owner to clip it.';
const SHOTS = process.env.CLIPSTUDIO_SHOTS || path.join(os.tmpdir(), 'clipstudio-e2e', 'shots');
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64');

let mock;
let browser;
const tests = [];
const test = (name, fn) => tests.push({ name, fn });

// ------------------------------------------------------------------ harness

/** Opens the app in a fresh browser context and records anything that must fail the test. */
async function open({ flags = {}, width = 1280, height = 900, scale = 1, context: contextOptions = {}, base = mock.url, keep = false, goto = true } = {}) {
  if (base === mock.url) mock.control(keep ? { ...flags, keep } : { jobMs: 250, ...flags });
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: scale, ...contextOptions });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const problems = [];
  page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const where = message.location().url || '';
    // The browser prints a line for every HTTP error answer. Error answers from the API are part of the tests.
    if (/^Failed to load resource/.test(message.text()) && /\/api\//.test(where)) return;
    problems.push(`console error: ${message.text()} (${where})`);
  });
  await page.exposeBinding('__reportCsp', (_source, detail) => problems.push(`CSP violation: ${detail}`));
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => window.__reportCsp(`${e.violatedDirective} blocked ${e.blockedURI || 'inline'} at line ${e.lineNumber}`));
    window.__shifts = [];
    try { new PerformanceObserver((list) => { for (const entry of list.getEntries()) window.__shifts.push(entry.value); }).observe({ type: 'layout-shift', buffered: true }); } catch { /* not supported */ }
  });
  // Nothing outside this computer is reachable from the test, so the two outside hosts the policy allows are answered here.
  await page.route('https://fonts.googleapis.com/**', (route) => route.fulfill({ status: 200, contentType: 'text/css', body: '' }));
  await page.route('https://i.ytimg.com/**', (route) => route.fulfill({ status: 200, contentType: 'image/png', body: PNG }));
  let response = null;
  if (goto) {
    response = await page.goto(`${base}/`);
    await page.waitForFunction(() => !document.querySelector('#tools').textContent.includes('Checking'));
  }
  return { page, context, problems, response, close: () => context.close() };
}

const text = async (page, selector) => ((await page.locator(selector).first().textContent()) || '').replace(/\s+/g, ' ').trim();
const visible = (page, selector) => page.locator(selector).first().isVisible();
const apiLog = (method, pathPart) => mock.log.filter((e) => e.method === method && e.path.includes(pathPart));
const card = (page, n) => page.locator('#moments > li').nth(n - 1);
const startField = (page, n) => card(page, n).locator('.cs-times input').nth(0);
const endField = (page, n) => card(page, n).locator('.cs-times input').nth(1);
const titleField = (page, n) => card(page, n).locator('.cs-m-title input');
const cardAlert = (page, n) => card(page, n).locator('.cs-m-state [role="alert"]');
const locked = (page) => page.locator('#moments-lock').evaluate((node) => node.disabled);

async function fillSource(page, url = GOOD_URL) {
  await page.fill('#url', url);
  await page.check('#permission');
}
async function analyze(page, url = GOOD_URL) {
  await fillSource(page, url);
  await page.click('#analyze-go');
}
async function toMoments(page, url = GOOD_URL) {
  await analyze(page, url);
  await page.waitForSelector('#stage-moments:not([hidden])');
}
async function toResults(page) {
  await page.click('#render-go');
  await page.waitForSelector('#results:not([hidden])');
}

/** Fails when the page scrolls sideways or any visible element sticks out past the right edge. */
async function assertNoOverflow(page, where) {
  const report = await page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const wide = [];
    for (const node of document.body.querySelectorAll('*')) {
      if (node.closest('.cs-sr, .cs-skip, .cs-timeline, .cs-art, .cs-mini')) continue;
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const rect = node.getBoundingClientRect();
      if (rect.width && rect.right > width + 0.5) wide.push(`${node.tagName.toLowerCase()}${node.id ? `#${node.id}` : ''}.${node.className} right=${Math.round(rect.right)}`);
    }
    return { scrollWidth: document.documentElement.scrollWidth, width, wide: wide.slice(0, 5) };
  });
  assert.ok(report.scrollWidth <= report.width, `${where}: the page scrolls sideways (${report.scrollWidth} > ${report.width})`);
  assert.deepEqual(report.wide, [], `${where}: elements stick out past the right edge`);
}

/** Every control a person can use must have a name a screen reader can say. */
async function assertAllLabelled(page, where) {
  const missing = await page.evaluate(() => {
    const out = [];
    const named = (ids) => ids.split(/\s+/).every((id) => { const n = document.getElementById(id); return n && n.textContent.trim(); });
    for (const node of document.querySelectorAll('input, textarea, select, meter, progress, button, a[href], video, [role="button"]')) {
      if (node.type === 'hidden' || node.type === 'file' || node.closest('[hidden]')) continue;
      const ok = (node.labels && node.labels.length && [...node.labels].some((l) => l.textContent.trim()))
        || (node.getAttribute('aria-label') || '').trim()
        || (node.hasAttribute('aria-labelledby') && named(node.getAttribute('aria-labelledby')))
        || (/^(BUTTON|A|DIV)$/.test(node.tagName) && node.textContent.trim());
      if (!ok) out.push(`${node.tagName.toLowerCase()}#${node.id}`);
    }
    return out;
  });
  assert.deepEqual(missing, [], `${where}: controls without a label`);
}

/** The copy rules from the brief, checked on what is actually on screen. */
async function assertCopyRules(page, where) {
  const found = await page.evaluate(() => {
    const problems = [];
    const words = document.body.innerText;
    if (/[\u2014\u2013]/.test(words)) problems.push('a long dash');
    if (/\u00B7|\u2022/.test(words)) problems.push('a middle dot');
    if (/[\u2190-\u21FF\u27A1\u2794]/.test(words)) problems.push('an arrow');
    if (/\p{Extended_Pictographic}/u.test(words)) problems.push('an emoji');
    const caps = words.match(/\b[A-Z]{4,}\b/g);
    if (caps) problems.push(`all-caps words: ${[...new Set(caps)].join(', ')}`);
    for (const node of document.body.querySelectorAll('*')) if (getComputedStyle(node).textTransform === 'uppercase') { problems.push(`uppercase styling on ${node.tagName}`); break; }
    for (const node of document.querySelectorAll('button, .k-btn')) if (/[>\u203A\u00BB]\s*$/.test(node.textContent)) problems.push(`arrow in button: ${node.textContent}`);
    return problems;
  });
  assert.deepEqual(found, [], `${where}: copy rules`);
}

async function assertNoInjection(page, where) {
  const report = await page.evaluate(() => ({
    flag: typeof window.__xss,
    nodes: document.querySelectorAll('img[src="x"], svg, script:not([src="kit.js"]):not([src="app.js"]), [onerror], [onload], iframe, object, embed').length,
    scripts: document.scripts.length,
  }));
  assert.equal(report.flag, 'undefined', `${where}: injected code ran`);
  assert.equal(report.nodes, 0, `${where}: server text became HTML`);
  assert.equal(report.scripts, 2, `${where}: unexpected script elements`);
}

async function assertFocusVisible(page, what) {
  const outline = await page.evaluate(() => { const s = getComputedStyle(document.activeElement); return { style: s.outlineStyle, width: parseFloat(s.outlineWidth) }; });
  assert.ok(outline.style !== 'none' && outline.width >= 2, `no visible focus on ${what}: ${JSON.stringify(outline)}`);
}

/** Presses Tab until the wanted element has focus, the way a keyboard user would get there. */
async function tabTo(page, selector, max = 250) {
  for (let i = 0; i < max; i += 1) {
    await page.keyboard.press('Tab');
    if (await page.evaluate((s) => Boolean(document.activeElement && document.activeElement.matches(s)), selector)) {
      await assertFocusVisible(page, selector);
      return;
    }
  }
  throw new Error(`Tab never reached ${selector}`);
}

async function until(read, what, ms = 5000) {
  const end = Date.now() + ms;
  for (;;) {
    const value = read();
    if (value) return value;
    if (Date.now() > end) throw new Error(`Timed out waiting: ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

async function shot(page, name, options = {}) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, name), ...options });
}

// --------------------------------------------------------------------- tests

test('harness: the mock sends the policy and a violation is caught', async () => {
  const { page, problems, response, close } = await open();
  assert.equal(response.headers()['content-security-policy'], CSP);
  assert.equal(response.headers()['x-content-type-options'], 'nosniff');
  assert.equal(response.headers()['referrer-policy'], 'no-referrer');
  assert.deepEqual(problems, [], 'a clean page load reports nothing');
  await page.evaluate(() => document.body.setAttribute('style', 'color: red'));
  await page.waitForTimeout(100);
  assert.ok(problems.some((p) => p.startsWith('CSP violation')), 'an inline style attribute is reported as a violation');
  assert.ok(problems.some((p) => p.startsWith('console error')), 'and as a console error');
  problems.length = 0;
  const viaRoute = await (await fetch(`${mock.url}/__mock/control?scenario=render-one-failed&tools=no-deno`)).json();
  assert.equal(viaRoute.scenario.render, 'one-failed');
  assert.equal(viaRoute.scenario.tools, 'no-deno');
  const bad = await fetch(`${mock.url}/__mock/control?scenario=nope`);
  assert.equal(bad.status, 400);
  await close();
});

test('harness: the mock video really loads and answers Range requests', async () => {
  const whole = await fetch(`${mock.url}/output/run/01.mp4`);
  assert.equal(whole.status, 200);
  assert.equal(whole.headers.get('accept-ranges'), 'bytes');
  assert.equal((await whole.arrayBuffer()).byteLength, mock.sampleSize);
  const part = await fetch(`${mock.url}/output/run/01.mp4`, { headers: { Range: 'bytes=100-199' } });
  assert.equal(part.status, 206);
  assert.equal(part.headers.get('content-range'), `bytes 100-199/${mock.sampleSize}`);
  assert.equal((await part.arrayBuffer()).byteLength, 100);
  const tail = await fetch(`${mock.url}/output/run/01.mp4`, { headers: { Range: 'bytes=-50' } });
  assert.equal(tail.headers.get('content-range'), `bytes ${mock.sampleSize - 50}-${mock.sampleSize - 1}/${mock.sampleSize}`);
  const past = await fetch(`${mock.url}/output/run/01.mp4`, { headers: { Range: `bytes=${mock.sampleSize + 5}-` } });
  assert.equal(past.status, 416);
  assert.equal((await fetch(`${mock.url}/output/../kit.js`)).status, 200, 'a normalised path is just a static file');
  assert.equal((await fetch(`${mock.url}/%2e%2e/%2e%2e/package.json`)).status, 404, 'nothing outside public/ is served');
});

test('source: the status line lists the installed tools in plain words', async () => {
  const { page, problems, close } = await open();
  const line = await text(page, '#tools');
  assert.match(line, /yt-dlp 2026\.11\.01 \(opens video links\)/);
  assert.match(line, /ffmpeg 7\.1 \(cuts and renders video\)/);
  assert.match(line, /Deno 2\.5\.4 \(helper for YouTube links\)/);
  assert.equal(await page.locator('#tools').getAttribute('role'), 'status');
  assert.equal(await page.locator('#tools .cs-warn').count(), 0);
  assert.equal(await text(page, '#version'), 'Version 1.0.0');
  assert.equal(await text(page, '#url-help'), 'Accepted sites: youtube.com and youtu.be.');
  assert.equal(await text(page, 'label[for="url"]'), 'Link to a YouTube video or finished stream');
  assert.equal(await text(page, 'label[for="permission"]'), PERMISSION_LABEL);
  assert.equal(await page.locator('#permission').isChecked(), false);
  assert.deepEqual(await page.evaluate(() => { const l = document.querySelector('#clip-length'); const c = document.querySelector('#clip-count'); return [l.min, l.max, l.value, c.min, c.max, c.value]; }), ['10', '90', '30', '1', '10', '5']);
  assert.equal(await text(page, '#analyze-go'), 'Find the strongest moments');
  for (const stage of ['progress', 'moments', 'results']) assert.equal(await visible(page, `#stage-${stage}`), false, `${stage} is hidden at the start`);
  assert.equal(await text(page, '#steps [aria-current="step"]'), 'Source');
  const notes = await text(page, '.cs-before');
  assert.match(notes, /more likely to hold attention\. It cannot make a clip popular\./);
  assert.match(notes, /Instagram says accounts that mostly repost other people's videos are not recommended to new viewers/);
  assert.match(notes, /Music in a clip can still be claimed separately/);
  await assertAllLabelled(page, 'stage 1');
  await assertCopyRules(page, 'stage 1');
  assert.deepEqual(problems, []);
  await close();
});

test('source: a missing tool gets a clear warning and a next step', async () => {
  let session = await open({ flags: { scenario: 'missing-tools' } });
  let warning = await text(session.page, '#tools .cs-warn');
  assert.equal(warning, 'Deno is missing, so YouTube links will probably fail. Run start.sh again so it can be installed.');
  assert.match(await text(session.page, '#tools'), /yt-dlp 2026\.11\.01/);
  assert.doesNotMatch(await text(session.page, '#tools .k-meta'), /Deno/);
  assert.deepEqual(session.problems, []);
  await session.close();

  // On Windows, the main target, the file to run is start.bat.
  session = await open({ flags: { tools: 'no-deno' }, context: { userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36' } });
  warning = await text(session.page, '#tools .cs-warn');
  assert.equal(warning, 'Deno is missing, so YouTube links will probably fail. Run start.bat (Windows) or start.sh (macOS, Linux) again so it can be installed.');
  await session.close();

  session = await open({ flags: { tools: 'none' } });
  const all = await session.page.locator('#tools .cs-warn').allTextContents();
  assert.equal(all.length, 3);
  assert.match(all[0], /^ffmpeg is missing, so videos cannot be read or rendered\. Run start\.sh again/);
  assert.match(all[1], /^yt-dlp is missing, so video links cannot be opened\. You can still choose a file\./);
  assert.match(all[2], /^Deno is missing/);
  await analyze(session.page);
  assert.match(await text(session.page, '#source-error'), /^ffmpeg is missing, so the app cannot read videos yet\. Run start\.sh again so it can be installed\./);
  assert.equal(apiLog('POST', '/api/analyze').length, 0, 'nothing is sent without ffmpeg');
  assert.deepEqual(session.problems, []);
  await session.close();

  session = await open({ flags: { tools: 'no-ytdlp' } });
  await analyze(session.page);
  assert.match(await text(session.page, '#source-error'), /^yt-dlp is missing, so links cannot be opened\..*Or choose a video file instead\.$/);
  assert.equal(apiLog('POST', '/api/analyze').length, 0);
  await session.close();

  session = await open({ flags: { tools: 'no-text' } });
  assert.match(await text(session.page, '#tools .cs-warn'), /^This copy of ffmpeg cannot draw text on video, so captions and titles are switched off\. Run start\.sh again so the right one can be installed\.$/);
  await toMoments(session.page);
  assert.equal(await session.page.locator('#opt-captions').isDisabled(), true);
  assert.equal(await session.page.locator('#opt-title').isDisabled(), true);
  assert.match(await text(session.page, '#title-help'), /cannot draw text on video/);
  await toResults(session.page);
  const options = apiLog('POST', '/api/render')[0].body.options;
  assert.equal(options.captions, false);
  assert.equal(options.showTitle, false);
  assert.deepEqual(session.problems, []);
  await session.close();

  session = await open({ flags: { status: 'down' } });
  assert.match(await text(session.page, '#tools .cs-warn'), /^The app is not answering, so the installed tools could not be checked\./);
  assert.deepEqual(session.problems, []);
  await session.close();
});

test('source: validation stops bad input before anything is sent', async () => {
  const { page, problems, close } = await open();
  const expectError = async (pattern, focusId) => {
    await page.click('#analyze-go');
    assert.match(await text(page, '#source-error'), pattern);
    if (focusId) assert.equal(await page.evaluate(() => document.activeElement.id), focusId);
    assert.equal(await visible(page, '#stage-progress'), false);
  };
  assert.equal(await page.locator('#source-error').getAttribute('role'), 'alert');

  await expectError(/^Add a video first: paste a link or choose a file\.$/, 'url');
  assert.equal(await page.locator('#url').getAttribute('aria-invalid'), 'true');

  for (const bad of ['not a link', 'youtube.com/watch?v=1', 'ftp://youtube.com/watch?v=1', 'javascript:alert(1)', 'https://']) {
    await page.fill('#url', bad);
    assert.equal(await text(page, '#source-error'), '', 'typing clears the old message');
    await expectError(/^This is not a full web address\. Paste the whole link, starting with https:\/\/$/, 'url');
  }
  await page.fill('#url', 'https://user:secret@www.youtube.com/watch?v=1');
  await expectError(/user name or password in it\. Remove that part/, 'url');
  for (const bad of ['https://vimeo.com/12345', 'https://youtube.com.evil.example/watch?v=1', 'https://notyoutube.com/watch?v=1', 'https://evil.example/?u=youtube.com']) {
    await page.fill('#url', bad);
    await expectError(/^This link is from a site the app does not accept\. Accepted sites: youtube\.com and youtu\.be\.$/, 'url');
  }

  await page.fill('#url', GOOD_URL);
  await expectError(/^Tick the box to confirm that you made this video or have permission to clip it\.$/, 'permission');
  assert.equal(await page.locator('#permission').getAttribute('aria-invalid'), 'true');
  assert.equal(await page.locator('#url').getAttribute('aria-invalid'), null);

  // A file of the wrong kind, an empty one, and then a link together with a good file.
  const input = page.locator('#file-drop input[type="file"]');
  await input.setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') });
  assert.match(await text(page, '#source-error'), /^That file type is not accepted\. Choose an MP4, MOV, MKV, WebM, M4V or AVI video\.$/);
  assert.equal(await visible(page, '#file-row'), false);
  await input.setInputFiles({ name: 'empty.mp4', mimeType: 'video/mp4', buffer: Buffer.alloc(0) });
  assert.match(await text(page, '#source-error'), /^That file is empty \(0 bytes\)\. Choose a different file\.$/);
  await input.setInputFiles({ name: 'clip.MP4', mimeType: 'video/mp4', buffer: Buffer.alloc(2048, 1) });
  assert.equal(await visible(page, '#file-row'), true);
  assert.equal(await text(page, '#file-name'), 'clip.MP4');
  assert.equal(await text(page, '#file-size'), '2.0 KB');
  await page.check('#permission');
  await expectError(/^There is both a link and a file\. Use one of them: clear the link or remove the file\.$/, 'url');
  await page.click('#file-remove');
  assert.equal(await visible(page, '#file-row'), false);

  assert.equal(mock.log.filter((e) => e.method === 'POST').length, 0, 'no request left the page');
  assert.deepEqual(problems, []);
  await close();

  // The server can still say no. Its words and its next step are shown, and the form works again.
  const second = await open({ flags: { analyze: 'reject' } });
  await analyze(second.page);
  await second.page.waitForFunction(() => document.querySelector('#source-error').textContent !== '');
  assert.equal(await text(second.page, '#source-error'), 'That does not look like a video link.Paste the full address of a YouTube video or finished stream.');
  assert.equal(await second.page.locator('#analyze-go').isDisabled(), false);
  assert.equal(await visible(second.page, '#stage-progress'), false);
  assert.equal(await second.page.locator('#url').getAttribute('aria-invalid'), 'true');
  assert.deepEqual(second.problems, []);
  await second.close();
});

test('normal run: source, progress, moments and results', async () => {
  const { page, problems, close } = await open({ flags: { scenario: 'normal', jobMs: 2500 }, width: 1360, height: 900, scale: 2 });
  await analyze(page);

  // ---- stage 2
  await page.waitForSelector('#stage-progress:not([hidden])');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'h-progress', 'focus moves to the new stage');
  assert.equal(await text(page, '#steps [aria-current="step"]'), 'Progress');
  assert.equal(await page.locator('#analyze-go').isDisabled(), true);
  assert.deepEqual(apiLog('POST', '/api/analyze')[0].body, { clipLength: 30, count: 5, permission: true, url: GOOD_URL });
  assert.match(apiLog('POST', '/api/analyze')[0].contentType, /^application\/json/);
  assert.equal(await page.evaluate(() => { const bar = document.querySelector('#an-bar'); return bar.tagName === 'PROGRESS' && bar.labels.length === 1 && bar.labels[0].textContent; }), 'Progress of the search');
  assert.equal(await page.locator('#an-step').getAttribute('role'), 'status');
  await page.waitForFunction(() => /Reading the video details|Listening to the audio|Finding the strongest moments/.test(document.querySelector('#an-step').textContent));
  await page.waitForFunction(() => { const v = document.querySelector('#an-bar').value; return v > 0 && v < 1; });
  assert.match(await text(page, '#an-pct'), /^\d{1,2}%$/);
  assert.equal(await visible(page, '#an-cancel'), true);
  await assertAllLabelled(page, 'stage 2');
  await assertCopyRules(page, 'stage 2');

  // ---- stage 3
  await page.waitForSelector('#stage-moments:not([hidden])');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'h-moments');
  assert.equal(await text(page, '#steps [aria-current="step"]'), 'Moments');
  assert.equal(await text(page, '#an-step'), 'Finished. Found 5 moments.');
  assert.equal(await visible(page, '#an-cancel'), false);
  assert.equal(await page.locator('#analyze-go').isDisabled(), false);
  const polls = apiLog('GET', '/api/jobs/');
  assert.ok(polls.length >= 3 && polls.length <= 7, `polled ${polls.length} times in about 2.5 s`);
  const gaps = polls.slice(1).map((p, i) => p.at - polls[i].at);
  assert.ok(gaps.every((g) => g >= 600 && g <= 1300), `poll gaps are about 700 ms: ${gaps}`);

  assert.equal(await text(page, '#video-title'), 'Weekend stream: building an oak table from a skip find');
  assert.equal(await text(page, '#video-meta'), 'From Asha Builds. 1:23:45 long. Finished stream.');
  await page.waitForSelector('#video-thumb:not([hidden]) img');
  assert.deepEqual(await page.locator('#signals li').allTextContents(), ['Replay data from YouTube: found', 'Audio loudness: measured', 'Captions: en (automatic)']);
  assert.equal(await visible(page, '#analysis-note'), false);

  assert.equal(await page.locator('#moments > li').count(), 5);
  assert.deepEqual(await page.locator('#moments meter').evaluateAll((meters) => meters.map((m) => m.value)), [100, 87, 73, 61, 44], 'strongest first');
  assert.equal(await page.locator('#moments input[type="checkbox"]:checked').count(), 5, 'all ticked by default');
  assert.equal(await text(page, '#render-go'), 'Make 5 shorts');
  for (const role of [['checkbox', 'Include Moment 1'], ['textbox', 'Start Moment 1'], ['textbox', 'End Moment 1'], ['textbox', 'Title, shown at the top of the short Moment 1'], ['meter', 'Score Moment 1'], ['link', 'Watch this part on YouTube Moment 1']]) {
    assert.equal(await page.getByRole(role[0], { name: role[1], exact: true }).count(), 1, `${role[0]} named "${role[1]}"`);
  }
  assert.match(await text(page, '#moments > li:nth-child(1) .cs-score'), /^Score ?100 out of 100 ?100 ?compared with the rest of this video$/);
  assert.deepEqual(await card(page, 1).locator('.cs-reasons li').allTextContents(), ['Viewers replayed this part', 'Loud reaction']);
  assert.equal(await startField(page, 1).inputValue(), '25:12.3');
  assert.equal(await endField(page, 1).inputValue(), '25:42.3');
  assert.equal(await startField(page, 2).inputValue(), '1:05:20.5');
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-len'), 'Length: 30.0 seconds');

  // the link to the original
  const link = card(page, 1).locator('.cs-m-links a');
  assert.equal(await link.getAttribute('href'), 'https://www.youtube.com/watch?v=mockvideo01&t=1512s');
  assert.equal(await link.getAttribute('target'), '_blank');
  assert.equal(await link.getAttribute('rel'), 'noopener noreferrer');
  assert.equal(await card(page, 5).locator('.cs-m-links a').getAttribute('href'), 'https://www.youtube.com/watch?v=mockvideo01&t=301s');

  // times: live length and each validation message
  const alertOf = async (n) => ((await cardAlert(page, n).textContent()) || '').trim();
  const waitAlert = (n, want) => page.waitForFunction(([i, expected]) => document.querySelectorAll('#moments > li')[i].querySelector('.cs-m-state [role="alert"]').textContent === expected, [n - 1, want]);
  await startField(page, 1).fill('25:20');
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-len'), 'Length: 22.3 seconds');
  assert.equal(await link.getAttribute('href'), 'https://www.youtube.com/watch?v=mockvideo01&t=1520s');
  assert.equal(await alertOf(1), '');
  // A half-typed time is not called an error straight away. The message comes after a pause, or on leaving the field.
  const whileTyping = await endField(page, 1).evaluate((field) => {
    field.value = '25:2';
    field.dispatchEvent(new Event('input', { bubbles: true }));
    const li = field.closest('li');
    return [li.querySelector('[role="alert"]').textContent, li.querySelector('.cs-len').textContent, field.getAttribute('aria-invalid')];
  });
  assert.deepEqual(whileTyping, ['', 'Length: not set yet', null]);
  await endField(page, 1).focus();
  await page.keyboard.press('Tab');
  assert.equal(await alertOf(1), 'Write the end like 1:35 or 1:35.5.', 'leaving the field shows the message at once');
  await endField(page, 1).fill('25:22');
  await waitAlert(1, 'Too short. A clip must be at least 8 seconds long.');
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-len'), 'Length: 2.0 seconds');
  assert.equal(await endField(page, 1).getAttribute('aria-invalid'), 'true');
  assert.equal(await startField(page, 1).getAttribute('aria-invalid'), null);
  await endField(page, 1).fill('27:10');
  await waitAlert(1, 'Too long. A clip can be at most 90 seconds long.');
  await endField(page, 1).fill('25:99');
  await waitAlert(1, 'Write the end like 1:35 or 1:35.5.');
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-len'), 'Length: not set yet');
  await endField(page, 1).fill('');
  await waitAlert(1, 'Enter an end time, like 1:35.');
  await endField(page, 1).fill('1:30:00');
  await waitAlert(1, 'The end is past the end of the video (1:23:45.4).');
  await startField(page, 1).fill('1:25:00');
  await waitAlert(1, 'The start is past the end of the video (1:23:45.4).');
  assert.equal(await startField(page, 1).getAttribute('aria-invalid'), 'true');
  await startField(page, 1).fill('abc');
  await waitAlert(1, 'Write the start like 1:05 or 1:05.5.');
  await startField(page, 1).fill('25:50');
  await endField(page, 1).fill('25:40');
  await waitAlert(1, 'The end must be after the start.');

  // an invalid ticked moment stops the render and says which one
  await page.click('#render-go');
  assert.equal(await text(page, '#render-error'), 'Moment 1 cannot be made yet. The end must be after the start.Fix its times, or untick it.');
  assert.equal(await page.locator('#render-error').getAttribute('role'), 'alert');
  assert.equal(await endField(page, 1).evaluate((node) => node === document.activeElement), true);
  assert.equal(apiLog('POST', '/api/render').length, 0);

  await startField(page, 1).fill('25:10.5');
  await endField(page, 1).fill('25:45');
  assert.equal(await alertOf(1), '');
  assert.equal(await text(page, '#render-error'), '');
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-len'), 'Length: 34.5 seconds');
  await endField(page, 2).fill('3950.5'); // plain seconds work too
  assert.equal(await text(page, '#moments > li:nth-child(2) .cs-len'), 'Length: 30.0 seconds');

  // title with a counter and a hard limit
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-count'), '26 of 80 characters');
  await titleField(page, 1).fill('x'.repeat(100));
  assert.equal((await titleField(page, 1).inputValue()).length, 80);
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-count'), '80 of 80 characters');
  await titleField(page, 1).fill('My hook');
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-count'), '7 of 80 characters');

  // transcript: three lines, then all of it
  const more = card(page, 1).locator('.cs-linkbtn');
  const words = card(page, 1).locator('.cs-text');
  assert.equal(await more.isVisible(), true);
  assert.equal(await more.getAttribute('aria-expanded'), 'false');
  const lineHeight = await words.evaluate((node) => parseFloat(getComputedStyle(node).lineHeight));
  const closed = await words.evaluate((node) => node.clientHeight);
  assert.ok(Math.abs(closed - 3 * lineHeight) < 2, `closed transcript is three lines tall (${closed} vs ${3 * lineHeight})`);
  await more.click();
  assert.equal(await more.getAttribute('aria-expanded'), 'true');
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-linkbtn'), 'Show less');
  assert.ok(await words.evaluate((node) => node.clientHeight) > closed + lineHeight);
  await more.click();
  assert.equal(await words.evaluate((node) => node.clientHeight), closed);
  assert.equal(await card(page, 2).locator('.cs-linkbtn').isVisible(), false, 'short text needs no toggle');
  assert.equal(await text(page, '#moments > li:nth-child(3) .cs-text'), 'No words were found for this part.');

  await page.evaluate(() => window.scrollTo({ top: document.querySelector('#stage-moments').getBoundingClientRect().top + window.scrollY - 16, behavior: 'instant' }));
  await shot(page, 'clip-moments.png');

  // include boxes drive the button
  await card(page, 5).locator('input[type="checkbox"]').uncheck();
  assert.equal(await text(page, '#render-go'), 'Make 4 shorts');
  for (const n of [1, 2, 3, 4]) await card(page, n).locator('input[type="checkbox"]').uncheck();
  assert.equal(await text(page, '#render-go'), 'Make 0 shorts');
  assert.equal(await page.locator('#render-go').isDisabled(), true);
  for (const n of [1, 2, 3]) await card(page, n).locator('input[type="checkbox"]').check();
  await card(page, 3).locator('input[type="checkbox"]').uncheck();
  await card(page, 4).locator('input[type="checkbox"]').check();
  assert.equal(await text(page, '#render-go'), 'Make 3 shorts');
  assert.equal(await page.locator('#render-go').isDisabled(), false);
  assert.equal(await page.locator('#timeline span').count(), 5);
  assert.equal(await page.locator('#timeline span.off').count(), 2);

  // a moment of your own
  await page.click('#add-moment');
  assert.equal(await page.locator('#moments > li').count(), 6);
  assert.equal(await startField(page, 6).evaluate((node) => node === document.activeElement), true, 'focus lands in the new start field');
  assert.equal(await alertOf(6), '', 'a blank new moment does not complain yet');
  assert.equal(await text(page, '#moments > li:nth-child(6) .cs-score'), 'You added this moment, so it has no score.');
  assert.equal(await text(page, '#render-go'), 'Make 4 shorts');
  await page.click('#render-go');
  assert.match(await text(page, '#render-error'), /^Moment 6 cannot be made yet\. Enter a start time, like 1:05\./);
  await startField(page, 6).fill('10:00');
  await endField(page, 6).fill('10:20');
  assert.equal(await alertOf(6), '');
  assert.equal(await text(page, '#moments > li:nth-child(6) .cs-len'), 'Length: 20.0 seconds');
  assert.equal(await card(page, 6).locator('.cs-m-links a').getAttribute('href'), 'https://www.youtube.com/watch?v=mockvideo01&t=600s');
  await page.click('#add-moment');
  assert.equal(await page.locator('#moments > li').count(), 7);
  await page.getByRole('button', { name: 'Remove moment 7' }).click();
  assert.equal(await page.locator('#moments > li').count(), 6);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'add-moment');
  assert.equal(await startField(page, 6).inputValue(), '10:00', 'the other added moment is untouched');

  // render options
  assert.equal(await text(page, 'label:has(input[value="blur"])'), 'Blurred background, whole picture visible');
  assert.equal(await text(page, 'label:has(input[value="fill"])'), 'Fill the frame, sides cropped');
  assert.equal(await visible(page, '#opt-zoom'), true);
  assert.equal(await visible(page, '#opt-position'), false);
  assert.deepEqual(await page.evaluate(() => { const z = document.querySelector('#zoom'); return [z.min, z.max, z.value]; }), ['1', '2', '1.3']);
  await page.focus('#zoom');
  await page.keyboard.press('Home');
  for (let i = 0; i < 10; i += 1) await page.keyboard.press('ArrowRight');
  assert.equal(await text(page, '#zoom-out'), '1.50 times');
  await page.check('input[name="layout"][value="fill"]');
  assert.equal(await visible(page, '#opt-zoom'), false);
  assert.equal(await visible(page, '#opt-position'), true);
  assert.deepEqual(await page.locator('#opt-position label').allTextContents(), ['Left', 'Centre', 'Right']);
  await page.check('input[name="position"][value="right"]');
  assert.equal(await page.locator('#opt-captions').isEnabled(), true);
  assert.equal(await text(page, 'label[for="opt-captions"]'), 'Burn captions into the video');
  assert.equal(await text(page, 'label[for="opt-title"]'), 'Show the title at the top');
  await page.uncheck('#opt-captions');
  await assertAllLabelled(page, 'stage 3');
  await assertCopyRules(page, 'stage 3');

  // ---- stage 4
  await page.click('#render-go');
  await page.waitForSelector('#stage-results:not([hidden])');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'h-results');
  assert.equal(await text(page, '#steps [aria-current="step"]'), 'Results');
  assert.deepEqual(apiLog('POST', '/api/render')[0].body, {
    analysisId: mock.log.find((e) => e.path === '/api/render').body.analysisId,
    clips: [
      { start: 1510.5, end: 1545, title: 'My hook' },
      { start: 3920.5, end: 3950.5, title: 'Best board I ever cut' },
      { start: 2204, end: 2234, title: 'Why no screws' },
      { start: 600, end: 620, title: '' },
    ],
    options: { layout: 'fill', zoom: 1.5, position: 'right', captions: false, showTitle: true },
  });
  assert.match(apiLog('POST', '/api/render')[0].body.analysisId, /^an-\d+$/);
  assert.equal(await page.evaluate(() => { const bar = document.querySelector('#rn-bar'); return bar.tagName === 'PROGRESS' && bar.labels.length === 1 && bar.labels[0].textContent; }), 'Progress of the shorts');
  await page.waitForFunction(() => /^Making short \d of 4$/.test(document.querySelector('#rn-step').textContent));
  assert.equal(await visible(page, '#rn-cancel'), true);
  assert.equal(await locked(page), true, 'moments are locked while the shorts are made');
  assert.equal(await visible(page, '#restart-row'), false);

  await page.waitForSelector('#results:not([hidden])');
  assert.equal(await text(page, '#rn-step'), 'Finished. 4 shorts are ready below.');
  assert.equal(await locked(page), false);
  assert.equal(await page.locator('#clips > li').count(), 4);
  const players = await page.locator('#clips video').evaluateAll((videos) => videos.map((v) => ({
    controls: v.controls, inline: v.hasAttribute('playsinline'), preload: v.getAttribute('preload'), src: v.getAttribute('src'), box: [v.getBoundingClientRect().width, v.getBoundingClientRect().height],
  })));
  assert.equal(players.length, 4);
  for (const [i, p] of players.entries()) {
    assert.deepEqual([p.controls, p.inline, p.preload, p.src], [true, true, 'metadata', `/output/mock-run/0${i + 1}.mp4`]);
    assert.ok(Math.abs(p.box[0] / p.box[1] - 9 / 16) < 0.01, `player ${i + 1} is 9:16 (${p.box})`);
    assert.ok(p.box[1] <= 0.7 * 900 + 1, `player ${i + 1} is at most 70vh tall`);
  }
  // the players really load the file, and seeking works through Range requests
  await page.waitForFunction(() => [...document.querySelectorAll('#clips video')].every((v) => v.readyState >= 1 && Math.abs(v.duration - 3) < 0.2 && v.videoWidth === 270 && !v.error));
  const landed = await page.locator('#clips video').first().evaluate((video) => new Promise((resolve, reject) => {
    video.addEventListener('seeked', () => resolve(video.currentTime), { once: true });
    video.addEventListener('error', () => reject(new Error('video error while seeking')), { once: true });
    video.currentTime = 2.2;
  }));
  assert.ok(Math.abs(landed - 2.2) < 0.05);
  assert.ok(mock.log.some((e) => e.path === '/output/mock-run/01.mp4' && e.range), 'the player asked for a byte range');

  const first = page.locator('#clips > li').first();
  assert.equal(await text(page, '#clips > li:nth-child(1) h3'), 'Short 1');
  assert.match(await text(page, '#clips > li:nth-child(1) .k-meta'), /^01-my-hook\.mp4\. 0:34 long, \d+(\.\d)? KB\.$/);
  const download = first.locator('a[download]');
  assert.equal(await text(page, '#clips > li:nth-child(1) a[download]'), 'Download');
  assert.equal(await download.getAttribute('download'), '01-my-hook.mp4');
  assert.equal(await download.getAttribute('href'), '/output/mock-run/01.mp4');
  const [saved] = await Promise.all([page.waitForEvent('download'), download.click()]);
  assert.equal(saved.suggestedFilename(), '01-my-hook.mp4');
  const caption = first.locator('textarea');
  assert.equal(await caption.evaluate((node) => node.readOnly && node.labels[0].textContent), 'Suggested caption');
  assert.equal(await caption.inputValue(), 'My hook\nClip from Asha Builds on YouTube');
  await first.getByRole('button', { name: 'Copy caption of short 1' }).click();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'My hook\nClip from Asha Builds on YouTube');
  assert.equal(await text(page, '.k-toast'), 'Copied.');
  assert.equal(await page.locator('.k-toast').getAttribute('role'), 'status');
  assert.equal(await text(page, '#folder'), 'C:\\Users\\Asha\\Videos\\Clip Studio\\weekend-stream-2026-11-21');
  assert.match(await text(page, '#folder-row'), /^The files are saved in this folder on this computer/);
  await page.click('#folder-copy');
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'C:\\Users\\Asha\\Videos\\Clip Studio\\weekend-stream-2026-11-21');
  await assertAllLabelled(page, 'stage 4');
  await assertCopyRules(page, 'stage 4');
  await assertNoOverflow(page, 'desktop, all stages open');

  await page.evaluate(() => window.scrollTo({ top: document.querySelector('#clips').getBoundingClientRect().top + window.scrollY - 150, behavior: 'instant' }));
  await page.locator('.k-toast').waitFor({ state: 'detached' });
  await shot(page, 'clip-results.png');

  // making them again with other choices replaces the cards
  await card(page, 2).locator('input[type="checkbox"]').uncheck();
  await toResults(page);
  await page.waitForFunction(() => document.querySelectorAll('#clips > li').length === 3);

  // ---- start again
  assert.equal(await text(page, '#restart'), 'Start again with another video');
  await page.click('#restart');
  for (const stage of ['progress', 'moments', 'results']) assert.equal(await visible(page, `#stage-${stage}`), false);
  assert.equal(await page.inputValue('#url'), '');
  assert.equal(await page.locator('#permission').isChecked(), false);
  assert.equal(await page.locator('#clips > li').count(), 0);
  assert.equal(await page.locator('#moments > li').count(), 0);
  assert.equal(await page.evaluate(() => sessionStorage.getItem('clipstudio.session')), null);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'url');
  assert.equal(await text(page, '#steps [aria-current="step"]'), 'Source');
  await toMoments(page, 'https://youtu.be/mockvideo01');
  assert.equal(await page.locator('#moments > li').count(), 5, 'a second video works after starting again');
  assert.deepEqual(problems, []);
  await close();
});

test('analyze error: the reason and the next step are shown, and a new try works', async () => {
  const { page, problems, close } = await open({ flags: { scenario: 'analyze-error' } });
  await analyze(page);
  await page.waitForFunction(() => document.querySelector('#an-error').textContent !== '');
  assert.equal(await page.locator('#an-error').getAttribute('role'), 'alert');
  assert.equal(await text(page, '#an-error'), 'YouTube says this video is unavailable.Check the link, or try another video.');
  assert.equal(await text(page, '#an-error .cs-hint'), 'Check the link, or try another video.');
  assert.equal(await text(page, '#an-step'), 'Stopped because of a problem.');
  assert.equal(await visible(page, '#an-cancel'), false);
  assert.equal(await visible(page, '#stage-moments'), false);
  assert.equal(await page.locator('#analyze-go').isDisabled(), false);
  assert.equal(await page.evaluate(() => sessionStorage.getItem('clipstudio.session')), null);
  const pollsAfterError = apiLog('GET', '/api/jobs/').length;
  await page.waitForTimeout(1600);
  assert.equal(apiLog('GET', '/api/jobs/').length, pollsAfterError, 'polling stops after an error');

  mock.control({ analyze: 'ok', keep: true });
  await page.click('#analyze-go');
  await page.waitForSelector('#stage-moments:not([hidden])');
  assert.equal(await text(page, '#an-error'), '');
  assert.deepEqual(problems, []);
  await close();
});

test('slow job: Cancel stops the analysis and the render', async () => {
  const { page, problems, close } = await open({ flags: { scenario: 'slow' } });
  await analyze(page);
  await page.waitForFunction(() => /Reading the video details/.test(document.querySelector('#an-step').textContent));
  assert.equal(await text(page, '#an-cancel'), 'Cancel');
  const jobId = apiLog('GET', '/api/jobs/')[0].path.split('/').pop();
  assert.equal(await page.evaluate(() => JSON.parse(sessionStorage.getItem('clipstudio.session')).analyzeJob), jobId);
  await page.focus('#an-cancel');
  await page.click('#an-cancel');
  await page.waitForFunction(() => document.querySelector('#an-step').textContent === 'Cancelled. Nothing was analysed.');
  assert.deepEqual(apiLog('POST', '/cancel').map((e) => e.path), [`/api/jobs/${jobId}/cancel`]);
  assert.equal(await visible(page, '#an-cancel'), false);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'h-progress', 'focus is not lost when the button goes away');
  assert.equal(await page.locator('#analyze-go').isDisabled(), false);
  assert.equal(await visible(page, '#stage-moments'), false);
  assert.equal(await page.evaluate(() => sessionStorage.getItem('clipstudio.session')), null);
  const pollsAfterCancel = apiLog('GET', '/api/jobs/').length;
  await page.waitForTimeout(1600);
  assert.equal(apiLog('GET', '/api/jobs/').length, pollsAfterCancel, 'polling stops after a cancel');

  // Now let an analysis through, then cancel the render.
  await page.click('#analyze-go');
  await page.waitForFunction(() => /Reading the video details/.test(document.querySelector('#an-step').textContent));
  mock.finishJobs();
  await page.waitForSelector('#stage-moments:not([hidden])');
  await page.click('#render-go');
  await page.waitForFunction(() => /^Making short 1 of 5$/.test(document.querySelector('#rn-step').textContent));
  assert.equal(await page.locator('#render-go').isDisabled(), true);
  await page.click('#rn-cancel');
  await page.waitForFunction(() => document.querySelector('#rn-step').textContent === 'Cancelled. No shorts were made.');
  assert.equal(apiLog('POST', '/cancel').length, 2);
  assert.equal(await visible(page, '#results'), false);
  assert.equal(await visible(page, '#restart-row'), true);
  assert.equal(await locked(page), false);
  assert.equal(await page.locator('#render-go').isDisabled(), false);
  assert.equal(await page.evaluate(() => JSON.parse(sessionStorage.getItem('clipstudio.session')).renderJob), null);
  assert.deepEqual(problems, []);
  await close();
});

test('render: one failed clip shows its error instead of a player', async () => {
  let session = await open({ flags: { scenario: 'render-one-failed' } });
  let page = session.page;
  await page.locator('#clip-count').fill('3');
  await toMoments(page);
  assert.equal(apiLog('POST', '/api/analyze')[0].body.count, 3);
  await toResults(page);
  assert.equal(await page.locator('#clips > li').count(), 3);
  assert.equal(await text(page, '#rn-step'), 'Finished. 2 of 3 shorts are ready. 1 could not be made, and the reason is on its card.');
  const failed = page.locator('#clips > li').nth(1);
  assert.equal(await failed.locator('video').count(), 0);
  assert.equal(await failed.locator('a[download]').count(), 0);
  assert.equal(await text(page, '#clips > li:nth-child(2) .cs-failed'), 'This short could not be made.ffmpeg ran out of memory while making this short. Close other programs and try again.');
  assert.equal(await page.locator('#clips > li:nth-child(1) video, #clips > li:nth-child(3) video').count(), 2);
  await page.waitForFunction(() => [...document.querySelectorAll('#clips video')].every((v) => v.readyState >= 1));
  assert.equal(await visible(page, '#folder-row'), true);
  assert.deepEqual(session.problems, []);
  await session.close();

  session = await open({ flags: { render: 'all-failed' } });
  page = session.page;
  await page.locator('#clip-count').fill('2');
  await toMoments(page);
  await toResults(page);
  assert.equal(await text(page, '#rn-step'), 'Finished, but none of the 2 shorts could be made. The reason is on each card.');
  assert.equal(await page.locator('#clips video').count(), 0);
  assert.equal(await visible(page, '#folder-row'), false);
  assert.deepEqual(session.problems, []);
  await session.close();

  // The whole render job failing, and the app refusing to start one.
  session = await open({ flags: { render: 'fail' } });
  page = session.page;
  await toMoments(page);
  await page.click('#render-go');
  await page.waitForFunction(() => document.querySelector('#rn-error').textContent !== '');
  assert.equal(await text(page, '#rn-error'), 'ffmpeg stopped before any short was finished.Look at clip-log.txt for the details, then try again.');
  assert.equal(await visible(page, '#results'), false);
  assert.equal(await page.locator('#render-go').isDisabled(), false);
  mock.control({ render: 'reject', keep: true });
  await page.click('#render-go');
  await page.waitForFunction(() => document.querySelector('#render-error').textContent !== '');
  assert.equal(await text(page, '#render-error'), 'The app is still finishing another set of shorts.Wait a moment, then try again.');
  assert.equal(await page.locator('#render-go').isDisabled(), false);
  assert.deepEqual(session.problems, []);
  await session.close();
});

test('no captions: said in plain words, and burning captions is switched off', async () => {
  const { page, problems, close } = await open({ flags: { scenario: 'no-captions' } });
  await toMoments(page);
  assert.deepEqual(await page.locator('#signals li').allTextContents(), ['Replay data from YouTube: found', 'Audio loudness: measured', 'Captions: none found, so clips will have no captions']);
  assert.deepEqual(await page.locator('#signals li').evaluateAll((items) => items.map((li) => li.classList.contains('on'))), [true, true, false]);
  const box = page.locator('#opt-captions');
  assert.equal(await box.isDisabled(), true);
  assert.equal(await box.isChecked(), false);
  assert.equal(await text(page, '#captions-help'), 'No captions were found for this video, so there is nothing to burn in.');
  assert.match(await box.getAttribute('aria-describedby'), /captions-help/);
  assert.equal(await page.locator('#opt-title').isEnabled(), true);
  assert.equal(await text(page, '#moments > li:nth-child(1) .cs-text'), 'No words were found for this part.');
  await toResults(page);
  assert.equal(apiLog('POST', '/api/render')[0].body.options.captions, false);
  assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('clipstudio.options') || '{}').captions), undefined, 'the saved preference is not overwritten');
  assert.deepEqual(problems, []);
  await close();
});

test('no replay data and no moments: both are explained', async () => {
  let session = await open({ flags: { scenario: 'no-replays' } });
  await toMoments(session.page);
  assert.equal((await session.page.locator('#signals li').allTextContents())[0], 'Replay data from YouTube: not available for this video');
  assert.equal(await text(session.page, '#analysis-note'), 'YouTube has no replay data for this video, so the moments come from the audio and the captions.');
  assert.equal(await session.page.locator('#moments > li').count(), 5);
  assert.deepEqual(session.problems, []);
  await session.close();

  session = await open({ flags: { video: 'empty' } });
  const page = session.page;
  await toMoments(page);
  assert.equal(await text(page, '#analysis-note'), 'The video is shorter than the clip length you chose.');
  assert.equal(await text(page, '#an-step'), 'Finished. No strong moments were found.');
  assert.equal(await page.locator('#moments > li').count(), 0);
  assert.equal(await text(page, '#render-go'), 'Make 0 shorts');
  assert.equal(await page.locator('#render-go').isDisabled(), true);
  await page.click('#add-moment');
  await startField(page, 1).fill('0:10');
  await endField(page, 1).fill('0:25');
  assert.equal(await text(page, '#render-go'), 'Make 1 short');
  await toResults(page);
  assert.equal(await text(page, '#rn-step'), 'Finished. 1 short is ready below.');
  assert.deepEqual(apiLog('POST', '/api/render')[0].body.clips, [{ start: 10, end: 25, title: '' }]);
  assert.deepEqual(session.problems, []);
  await session.close();
});

test('upload flow: progress, the file name header, and stopping', async () => {
  const { page, problems, close } = await open({ flags: { scenario: 'upload' } });
  const name = 'मेरा वीडियो 100%.mp4';
  const size = 24 * 1024 * 1024;
  const input = page.locator('#file-drop input[type="file"]');
  assert.equal(await page.locator('#file-drop').getAttribute('role'), 'button');
  assert.match(await text(page, '#file-drop'), /^Choose a video file from this computer/);
  await input.setInputFiles({ name, mimeType: 'video/mp4', buffer: Buffer.alloc(size, 7) });
  assert.equal(await text(page, '#file-name'), name);
  assert.equal(await text(page, '#file-size'), '24.0 MB');
  assert.equal(apiLog('POST', '/api/uploads').length, 0, 'nothing is copied before the button is pressed');
  await page.check('#permission');

  // stop half way
  await page.click('#analyze-go');
  await page.waitForFunction(() => { const v = document.querySelector('#upload-bar').value; return v > 0 && v < 1; });
  assert.equal(await page.evaluate(() => { const bar = document.querySelector('#upload-bar'); return bar.tagName === 'PROGRESS' && bar.labels[0].textContent; }), 'Copying the file into the app');
  assert.match(await text(page, '#upload-pct'), /^\d{1,2}%$/);
  assert.equal(await text(page, '#file-remove'), 'Stop copying');
  assert.equal(await page.locator('#analyze-go').isDisabled(), true);
  await page.click('#file-remove');
  await page.waitForFunction(() => document.querySelector('#upload-status').textContent === 'Copying stopped. The file was not added.');
  assert.equal(await page.locator('#upload-status').getAttribute('role'), 'status');
  assert.equal(await text(page, '#file-remove'), 'Remove this file');
  assert.equal(await page.locator('#analyze-go').isDisabled(), false);
  assert.equal(apiLog('POST', '/api/analyze').length, 0);
  assert.equal(await visible(page, '#stage-progress'), false);

  // all the way
  await page.click('#analyze-go');
  await page.waitForFunction(() => { const v = document.querySelector('#upload-bar').value; return v > 0.2 && v < 1; });
  await page.waitForSelector('#stage-moments:not([hidden])');
  const upload = apiLog('POST', '/api/uploads').pop();
  assert.equal(upload.filename, encodeURIComponent(name), 'X-Filename is percent-encoded UTF-8');
  assert.equal(upload.decodedName, name);
  assert.equal(upload.bytes, size, 'the whole file arrived as the body');
  assert.equal(upload.contentType, 'application/octet-stream');
  const body = apiLog('POST', '/api/analyze')[0].body;
  assert.match(body.uploadId, /^up-\d+$/);
  assert.deepEqual(Object.keys(body).sort(), ['clipLength', 'count', 'permission', 'uploadId']);
  assert.equal(await text(page, '#video-title'), name);
  assert.equal(await text(page, '#video-meta'), '1:23:45 long.');
  assert.equal((await page.locator('#signals li').allTextContents())[0], 'Replay data from YouTube: not available for a file from this computer');
  assert.equal(await page.locator('#moments a').count(), 0, 'a file has no link to watch');
  assert.equal(await visible(page, '#video-thumb'), false);
  await toResults(page);
  assert.equal(await page.locator('#clips textarea').first().inputValue(), 'The table nearly fell over', 'no credit line without a channel');

  // the same file is not copied twice
  const before = apiLog('POST', '/api/uploads').length;
  await page.click('#analyze-go');
  await page.waitForSelector('#stage-moments:not([hidden])');
  assert.equal(apiLog('POST', '/api/uploads').length, before);
  assert.equal(apiLog('POST', '/api/analyze').length, 2);

  // dropping a file works too, and the app can refuse a file
  mock.control({ upload: 'reject', keep: true });
  await page.click('#file-remove');
  assert.equal(await visible(page, '#file-row'), false);
  await page.evaluate(() => {
    const data = new DataTransfer();
    data.items.add(new File([new Uint8Array(5000)], 'dropped clip.webm', { type: 'video/webm' }));
    document.querySelector('#file-drop').dispatchEvent(new DragEvent('drop', { dataTransfer: data, bubbles: true, cancelable: true }));
  });
  assert.equal(await text(page, '#file-name'), 'dropped clip.webm');
  await page.click('#analyze-go');
  await page.waitForFunction(() => document.querySelector('#source-error').textContent !== '');
  assert.equal(await text(page, '#source-error'), 'There is not enough free disk space to copy this file.Free some space, then try again.');
  assert.equal(await page.locator('#analyze-go').isDisabled(), false);
  assert.equal(await visible(page, '#upload'), false);
  await page.click('#file-remove');
  assert.equal(await visible(page, '#file-row'), false);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'file-drop');
  assert.deepEqual(problems, []);
  await close();

  // a file over the size limit never leaves the browser
  const big = await open();
  await big.page.evaluate(() => {
    const file = new File([new Uint8Array(10)], 'huge.mp4', { type: 'video/mp4' });
    Object.defineProperty(file, 'size', { value: 65 * 1024 * 1024 });
    const data = new DataTransfer();
    data.items.add(file);
    const event = new DragEvent('drop', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'dataTransfer', { value: { files: [file] } });
    document.querySelector('#file-drop').dispatchEvent(event);
  });
  assert.equal(await text(big.page, '#source-error'), 'That file is 65.0 MB, which is over the limit of 64.0 MB. Choose a smaller file.');
  assert.deepEqual(big.problems, []);
  await big.close();
});

test('hostile text from the server stays text everywhere', async () => {
  let session = await open({ flags: { scenario: 'xss', jobMs: 1500 } });
  let page = session.page;
  assert.ok((await text(page, '#tools')).includes(XSS.img), 'tool versions are shown as text');
  assert.ok((await text(page, '#version')).includes(XSS.script));
  await assertNoInjection(page, 'status');
  await analyze(page);
  await page.waitForFunction((needle) => document.querySelector('#an-step').textContent.includes(needle), XSS.img);
  await assertNoInjection(page, 'progress step');
  await page.waitForSelector('#stage-moments:not([hidden])');
  for (const selector of ['#video-title', '#video-meta', '#analysis-note', '#signals li:nth-child(3)', '#moments > li:nth-child(1) .cs-reasons li', '#moments > li:nth-child(1) .cs-text']) {
    assert.ok((await text(page, selector)).includes(XSS.img), `${selector} shows the markup as text`);
  }
  assert.ok((await titleField(page, 1).inputValue()).includes('<img src=x'), 'the title field holds the raw text');
  assert.equal(await page.locator('#moments a').count(), 0, 'a javascript: video address never becomes a link');
  assert.equal(await page.locator('#video-thumb img').count(), 0, 'a javascript: thumbnail is never loaded');
  await assertNoInjection(page, 'moments');
  await assertNoOverflow(page, 'moments with hostile text');
  await page.click('#render-go');
  await page.waitForFunction((needle) => document.querySelector('#rn-step').textContent.includes(needle), XSS.img);
  await page.waitForSelector('#results:not([hidden])');
  assert.equal(await page.locator('#clips > li').count(), 5);
  assert.ok((await text(page, '#clips > li:nth-child(1) .k-meta')).includes(XSS.img), 'file names are text');
  assert.ok((await page.locator('#clips > li:nth-child(1) textarea').inputValue()).includes(XSS.script), 'captions are text');
  assert.ok((await page.locator('#clips > li:nth-child(1) a[download]').getAttribute('download')).includes('<img'));
  assert.ok((await text(page, '#folder')).includes(XSS.svg));
  // clips 4 and 5 came back with addresses outside /output/: no player and no link is made for them
  assert.equal(await page.locator('#clips video').count(), 3);
  assert.deepEqual(await page.locator('#clips video').evaluateAll((v) => v.map((x) => x.getAttribute('src'))), ['/output/mock-run/01.mp4', '/output/mock-run/02.mp4', '/output/mock-run/03.mp4']);
  assert.equal(await page.locator('#clips a').evaluateAll((links) => links.every((a) => a.getAttribute('href').startsWith('/output/mock-run/'))), true);
  assert.match(await text(page, '#clips > li:nth-child(5) .cs-failed'), /file address this page will not open/);
  await assertNoInjection(page, 'results');
  await assertNoOverflow(page, 'results with hostile text');
  assert.deepEqual(session.problems, []);
  await session.close();

  // failed clip errors, job errors and refusals carry server text as well
  session = await open({ flags: { video: 'xss', render: 'all-failed' } });
  page = session.page;
  await toMoments(page);
  await toResults(page);
  assert.ok((await text(page, '#clips > li:nth-child(1) .cs-failed')).includes(XSS.img));
  await assertNoInjection(page, 'failed clips');
  await session.close();

  session = await open({ flags: { video: 'xss', analyze: 'fail' } });
  page = session.page;
  await analyze(page);
  await page.waitForFunction(() => document.querySelector('#an-error').textContent !== '');
  assert.ok((await text(page, '#an-error')).includes(XSS.img));
  assert.ok((await text(page, '#an-error .cs-hint')).includes(XSS.script));
  await assertNoInjection(page, 'job error');
  assert.deepEqual(session.problems, []);
  await session.close();

  session = await open({ flags: { video: 'xss', analyze: 'reject' } });
  page = session.page;
  await analyze(page);
  await page.waitForFunction(() => document.querySelector('#source-error').textContent !== '');
  assert.ok((await text(page, '#source-error')).includes(XSS.img));
  await assertNoInjection(page, 'refusal');
  assert.deepEqual(session.problems, []);
  await session.close();

  // an uploaded file's own name is hostile too
  session = await open();
  page = session.page;
  const name = `${XSS.img}.mp4`;
  await page.locator('#file-drop input[type="file"]').setInputFiles({ name, mimeType: 'video/mp4', buffer: Buffer.alloc(3000, 1) });
  assert.equal(await text(page, '#file-name'), name);
  await page.check('#permission');
  await page.click('#analyze-go');
  await page.waitForSelector('#stage-moments:not([hidden])');
  assert.equal(await text(page, '#video-title'), name);
  await assertNoInjection(page, 'file name');
  assert.deepEqual(session.problems, []);
  await session.close();
});

test('the link to the original is made only for https on an accepted site', async () => {
  for (const [video, expected] of [['plain-http', null], ['other-site', null], ['with-login', null], ['short-link', 'https://youtu.be/mockvideo01?t=1512s']]) {
    const { page, problems, close } = await open({ flags: { video } });
    await toMoments(page);
    assert.equal(await page.locator('#moments > li').count(), 5);
    if (expected) assert.equal(await card(page, 1).locator('.cs-m-links a').getAttribute('href'), expected);
    else assert.equal(await page.locator('#moments a').count(), 0, `${video}: no link`);
    assert.deepEqual(problems, []);
    await close();
  }
});

test('reload: the running job, the edits and the results all come back', async () => {
  const { page, problems, close } = await open({ flags: { scenario: 'slow' } });
  await analyze(page);
  await page.waitForFunction(() => /Reading the video details/.test(document.querySelector('#an-step').textContent));
  const jobId = apiLog('GET', '/api/jobs/')[0].path.split('/').pop();

  // (a) reload while the analysis runs
  let mark = mock.log.length;
  await page.reload();
  await page.waitForFunction(() => /Reading the video details/.test(document.querySelector('#an-step').textContent));
  assert.equal(await visible(page, '#stage-progress'), true);
  assert.ok(mock.log.slice(mark).some((e) => e.method === 'GET' && e.path === `/api/jobs/${jobId}`), 'the same job is polled again');
  assert.equal(mock.log.slice(mark).filter((e) => e.method === 'POST').length, 0, 'nothing is started twice');
  assert.equal(await page.locator('#analyze-go').isDisabled(), true);
  assert.equal(await visible(page, '#an-cancel'), true);
  assert.equal(await text(page, '#steps [aria-current="step"]'), 'Progress');
  assert.equal(await page.inputValue('#url'), GOOD_URL, 'the link is back in its field');
  assert.equal(await page.locator('#permission').isChecked(), false, 'the permission tick is not remembered');
  mock.finishJobs();
  await page.waitForSelector('#stage-moments:not([hidden])');

  // (b) reload with edits in the moments
  await titleField(page, 1).fill('Kept after reload');
  await startField(page, 2).fill('1:05:25');
  await card(page, 3).locator('input[type="checkbox"]').uncheck();
  await page.click('#add-moment');
  await startField(page, 6).fill('2:00');
  await endField(page, 6).fill('2:30');
  await page.click('#add-moment'); // left blank on purpose
  await page.reload();
  await page.waitForSelector('#stage-moments:not([hidden])');
  assert.equal(await page.locator('#moments > li').count(), 7);
  assert.equal(await titleField(page, 1).inputValue(), 'Kept after reload');
  assert.equal(await startField(page, 1).inputValue(), '25:12.3');
  assert.equal(await startField(page, 2).inputValue(), '1:05:25');
  assert.equal(await text(page, '#moments > li:nth-child(2) .cs-len'), 'Length: 25.5 seconds');
  assert.equal(await card(page, 3).locator('input[type="checkbox"]').isChecked(), false);
  assert.equal(await startField(page, 6).inputValue(), '2:00');
  assert.equal(await endField(page, 6).inputValue(), '2:30');
  assert.equal(await startField(page, 7).inputValue(), '');
  assert.equal(((await cardAlert(page, 7).textContent()) || ''), '', 'the blank added moment still does not complain');
  assert.equal(await text(page, '#render-go'), 'Make 6 shorts');
  assert.equal(await text(page, '#an-step'), 'Finished. Found 7 moments.');
  assert.equal(await page.evaluate(() => document.activeElement === document.body), true, 'a reload does not move focus');
  await page.getByRole('button', { name: 'Remove moment 7' }).click();

  // (c) reload while the shorts are being made
  await page.click('#render-go');
  await page.waitForFunction(() => /^Making short 1 of 5$/.test(document.querySelector('#rn-step').textContent));
  const renderId = apiLog('GET', '/api/jobs/job-render')[0].path.split('/').pop();
  mark = mock.log.length;
  await page.reload();
  await page.waitForFunction(() => /^Making short 1 of 5$/.test(document.querySelector('#rn-step').textContent));
  assert.ok(mock.log.slice(mark).some((e) => e.path === `/api/jobs/${renderId}`));
  assert.equal(mock.log.slice(mark).filter((e) => e.method === 'POST').length, 0);
  assert.equal(await visible(page, '#stage-moments'), true);
  assert.equal(await locked(page), true);
  assert.equal(await titleField(page, 1).inputValue(), 'Kept after reload');
  assert.equal(await text(page, '#steps [aria-current="step"]'), 'Results');
  mock.finishJobs();
  await page.waitForSelector('#results:not([hidden])');
  assert.equal(await page.locator('#clips > li').count(), 5);
  assert.equal(await page.locator('#clips textarea').first().inputValue(), 'Kept after reload\nClip from Asha Builds on YouTube');

  // (d) reload with the results on screen
  await page.reload();
  await page.waitForSelector('#results:not([hidden])');
  assert.equal(await page.locator('#clips > li').count(), 5);
  await page.waitForFunction(() => [...document.querySelectorAll('#clips video')].every((v) => v.readyState >= 1));
  assert.equal(await visible(page, '#restart-row'), true);

  // (e) the app was restarted in between, so the job is gone
  mock.control({});
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#an-error').textContent !== '');
  assert.equal(await text(page, '#an-error'), 'The earlier job is no longer in the app, most likely because the app was restarted.Start again with the video.');
  assert.equal(await page.evaluate(() => sessionStorage.getItem('clipstudio.session')), null);
  assert.equal(await page.locator('#analyze-go').isDisabled(), false);
  assert.equal(await visible(page, '#stage-moments'), false);
  assert.deepEqual(problems, []);
  await close();
});

test('polling backs off on network errors and then recovers', async () => {
  const { page, problems, close } = await open({ flags: { jobMs: 1500 } });
  await analyze(page);
  await page.waitForSelector('#stage-progress:not([hidden])');
  await page.waitForFunction(() => document.querySelector('#an-step').textContent !== 'Starting.');
  // Count the page's own attempts: the browser quietly retries a cut connection once, so the server sees more.
  await page.evaluate(() => {
    window.__attempts = [];
    const original = window.fetch;
    window.fetch = (...args) => { if (String(args[0]).includes('/api/jobs/')) window.__attempts.push(Date.now()); return original(...args); };
  });
  mock.control({ dropMs: 6000, keep: true });
  await page.waitForFunction(() => document.querySelector('#an-step').textContent.startsWith('Lost contact with the app. Trying again.'));
  assert.equal(await text(page, '#an-step .cs-hint'), 'Check that the Clip Studio window is still open.');
  await page.waitForSelector('#stage-moments:not([hidden])', { timeout: 20000 });
  assert.ok(apiLog('GET', '/api/jobs/').filter((p) => p.dropped).length >= 3);
  const attempts = await page.evaluate(() => window.__attempts);
  const waits = attempts.slice(1).map((t, i) => t - attempts[i]);
  assert.ok(waits.length >= 3, `at least three retries: ${waits}`);
  assert.ok(waits[0] >= 1000 && waits.slice(1).every((w, i) => w > waits[i] * 1.4), `each wait is longer than the one before: ${waits}`);
  assert.equal(await text(page, '#an-step'), 'Finished. Found 5 moments.');
  assert.deepEqual(problems, []);
  await close();
});

test('the app stops answering: the page says so instead of hanging', async () => {
  const second = await startMock({ port: SECOND_PORT });
  const { page, problems, close } = await open({ base: second.url });
  assert.match(await text(page, '#tools'), /yt-dlp/);
  await second.close();
  await analyze(page);
  await page.waitForFunction(() => document.querySelector('#source-error').textContent !== '');
  assert.equal(await text(page, '#source-error'), 'The app is not answering. Check that the Clip Studio window is still open, then try again.');
  assert.equal(await page.locator('#url').getAttribute('aria-invalid'), null, 'the link is not blamed');
  assert.equal(await page.locator('#analyze-go').isDisabled(), false);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'analyze-go');
  assert.deepEqual(problems, []);
  await close();
});

test('choices are remembered between visits', async () => {
  const { page, problems, close } = await open();
  await page.locator('#clip-length').fill('45');
  await page.locator('#clip-count').fill('2');
  assert.equal(await text(page, '#clip-length-out'), '45 seconds');
  assert.equal(await text(page, '#clip-count-out'), '2 clips');
  assert.equal(await page.locator('#clip-length').getAttribute('aria-valuetext'), '45 seconds');
  await page.locator('#clip-count').fill('1');
  assert.equal(await text(page, '#clip-count-out'), '1 clip');
  await page.locator('#clip-count').fill('2');
  await toMoments(page);
  assert.deepEqual(apiLog('POST', '/api/analyze')[0].body, { clipLength: 45, count: 2, permission: true, url: GOOD_URL });
  assert.equal(await endField(page, 1).inputValue(), '25:57.3');
  await page.check('input[name="layout"][value="fill"]');
  await page.check('input[name="position"][value="left"]');
  await page.uncheck('#opt-title');
  await page.uncheck('#opt-captions');
  assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('clipstudio.options'))), { clipLength: 45, count: 2, layout: 'fill', zoom: 1.3, position: 'left', captions: false, showTitle: false });

  await page.evaluate(() => sessionStorage.clear());
  await page.reload();
  await page.waitForFunction(() => !document.querySelector('#tools').textContent.includes('Checking'));
  assert.equal(await page.inputValue('#clip-length'), '45');
  assert.equal(await page.inputValue('#clip-count'), '2');
  assert.equal(await text(page, '#clip-length-out'), '45 seconds');
  assert.equal(await page.locator('#permission').isChecked(), false, 'the permission tick is never remembered');
  await toMoments(page);
  assert.equal(await page.locator('input[name="layout"][value="fill"]').isChecked(), true);
  assert.equal(await page.locator('input[name="position"][value="left"]').isChecked(), true);
  assert.equal(await visible(page, '#opt-position'), true);
  assert.equal(await page.locator('#opt-title').isChecked(), false);
  assert.equal(await page.locator('#opt-captions').isChecked(), false);

  // damaged or hostile saved data falls back to the defaults
  await page.evaluate(() => { sessionStorage.setItem('clipstudio.session', '{not json'); localStorage.setItem('clipstudio.options', '{"clipLength":"<b>","count":9999,"layout":"sideways","zoom":"big","position":7}'); });
  await page.reload();
  await page.waitForFunction(() => !document.querySelector('#tools').textContent.includes('Checking'));
  assert.equal(await page.inputValue('#clip-length'), '30');
  assert.equal(await page.inputValue('#clip-count'), '10', 'a number that is too big is pulled back into range');
  assert.equal(await visible(page, '#stage-progress'), false);
  await page.evaluate(() => localStorage.setItem('clipstudio.options', 'garbage'));
  await page.reload();
  await page.waitForFunction(() => !document.querySelector('#tools').textContent.includes('Checking'));
  assert.equal(await page.inputValue('#clip-length'), '30');
  assert.deepEqual(problems, []);
  await close();
});

test('cards do not move when their messages change', async () => {
  for (const width of [1360, 800, 390]) {
    const { page, problems, close } = await open({ width, height: 900 });
    await toMoments(page);
    await page.waitForTimeout(700); // let the reveal settle and the "recent input" window pass
    const boxes = () => page.evaluate(() => [...document.querySelectorAll('#moments > li, #add-moment, #options, #render-go')].map((node) => { const r = node.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top + window.scrollY), Math.round(r.width), Math.round(r.height)]; }));
    const before = await boxes();
    await page.evaluate(() => { window.__shifts.length = 0; });
    await endField(page, 1).fill('25:15'); // too short
    await startField(page, 2).fill('nonsense');
    await endField(page, 3).fill('5:00:00'); // past the end
    await startField(page, 4).fill('1:25:00'); // start past the end
    await endField(page, 5).fill('');
    await titleField(page, 1).fill('A much longer title than the one that was there before, to change the counter');
    await card(page, 2).locator('input[type="checkbox"]').uncheck();
    await page.click('#render-go'); // puts a message beside the button
    assert.match(await text(page, '#render-error'), /^Moment 1 cannot be made yet/);
    assert.equal(((await cardAlert(page, 1).textContent()) || ''), 'Too short. A clip must be at least 8 seconds long.');
    await page.check('input[name="layout"][value="fill"]');
    await page.check('input[name="layout"][value="blur"]');
    await page.waitForFunction(() => [...document.querySelectorAll('#moments [role="alert"]')].filter((node) => node.textContent).length === 5);
    assert.deepEqual(await boxes(), before, `nothing moved or changed size at ${width}px`);
    const shift = await page.evaluate(() => window.__shifts.reduce((sum, value) => sum + value, 0));
    assert.ok(shift < 0.001, `layout shift at ${width}px was ${shift}`);
    assert.deepEqual(problems, []);
    await close();
  }
});

test('390px: no stage scrolls sideways', async () => {
  const { page, problems, close } = await open({ width: 390, height: 844, scale: 2, flags: { scenario: 'slow' } });
  assert.equal(await visible(page, '.cs-art'), false);
  await page.locator('#file-drop input[type="file"]').setInputFiles({ name: `${'averyveryverylongfilenamewithoutanybreaks'.repeat(4)}.mp4`, mimeType: 'video/mp4', buffer: Buffer.alloc(1500, 1) });
  await page.click('#analyze-go'); // shows a validation message as well
  await assertNoOverflow(page, 'stage 1 with a long file name and a message');
  await page.click('#file-remove');
  await analyze(page);
  await page.waitForFunction(() => /Reading the video details/.test(document.querySelector('#an-step').textContent));
  await assertNoOverflow(page, 'stage 2');
  mock.finishJobs();
  await page.waitForSelector('#stage-moments:not([hidden])');
  await endField(page, 1).fill('1:30:00');
  await page.click('#add-moment');
  await page.check('input[name="layout"][value="fill"]');
  await page.click('#render-go');
  await assertNoOverflow(page, 'stage 3 with messages');
  await card(page, 6).locator('input[type="checkbox"]').uncheck();
  await endField(page, 1).fill('25:42.3');
  mock.control({ render: 'one-failed', jobMs: 4000, keep: true });
  await page.click('#render-go');
  await page.waitForFunction(() => /^Making short/.test(document.querySelector('#rn-step').textContent));
  await assertNoOverflow(page, 'stage 4 while working');
  mock.finishJobs();
  await page.waitForSelector('#results:not([hidden])');
  await page.waitForFunction(() => [...document.querySelectorAll('#clips video')].every((v) => v.readyState >= 1));
  await assertNoOverflow(page, 'stage 4 with results');
  const player = await page.locator('#clips video').first().evaluate((v) => [v.getBoundingClientRect().width, v.getBoundingClientRect().height]);
  assert.ok(player[1] <= 0.7 * 844 + 1 && Math.abs(player[0] / player[1] - 9 / 16) < 0.01, `phone player keeps 9:16 inside 70vh (${player})`);
  await assertAllLabelled(page, '390px');
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, 'clip-mobile.png', { fullPage: true });
  assert.deepEqual(problems, []);
  await close();

  // unbroken words from the server, at phone width
  const long = await open({ width: 390, height: 844, flags: { video: 'long-words' } });
  await toMoments(long.page);
  await assertNoOverflow(long.page, 'stage 3 with unbroken words');
  await toResults(long.page);
  await assertNoOverflow(long.page, 'stage 4 with unbroken words');
  assert.deepEqual(long.problems, []);
  await long.close();

  // the same at a tablet width, where the two-column layouts switch
  const mid = await open({ width: 720, height: 900, flags: { video: 'long-words' } });
  await toMoments(mid.page);
  await toResults(mid.page);
  await assertNoOverflow(mid.page, '720px, all stages');
  await mid.close();
});

test('keyboard only: the whole flow without a mouse', async () => {
  const { page, problems, close } = await open({ width: 1280, height: 800 });
  // Listen for the file chooser from the start: switching the listener on takes a moment, and a key press can beat it.
  const choosers = [];
  page.on('filechooser', (chooser) => choosers.push(chooser));
  await page.evaluate(() => 0);
  // stage 1
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.className), 'cs-skip', 'the first stop is the skip link');
  await assertFocusVisible(page, 'skip link');
  await tabTo(page, '#url');
  await page.keyboard.type('https://youtu.be/mockvideo01');
  await tabTo(page, '#file-drop');
  await page.keyboard.press('Enter');
  const chooser = await until(() => choosers[0], 'the file chooser opens from the keyboard');
  assert.equal(chooser.isMultiple(), false);
  await page.keyboard.press('Space');
  await until(() => choosers[1], 'the file chooser opens with the space bar too');
  await tabTo(page, '#clip-length');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await tabTo(page, '#clip-count');
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.press('ArrowLeft');
  await tabTo(page, '#permission');
  await page.keyboard.press('Space');
  await tabTo(page, '#analyze-go');
  await page.keyboard.press('Enter');
  await page.waitForSelector('#stage-moments:not([hidden])');
  assert.deepEqual(apiLog('POST', '/api/analyze')[0].body, { clipLength: 32, count: 3, permission: true, url: 'https://youtu.be/mockvideo01' });

  // stage 3: focus is on its heading, so the next Tab stops are inside it
  assert.equal(await page.evaluate(() => document.activeElement.id), 'h-moments');
  await tabTo(page, '#moments > li:nth-child(1) input[type="checkbox"]');
  await page.keyboard.press('Space');
  assert.equal(await text(page, '#render-go'), 'Make 2 shorts');
  await tabTo(page, '#moments > li:nth-child(2) .cs-times input');
  await page.keyboard.press('Control+A');
  await page.keyboard.type('1:05:22');
  await tabTo(page, '#moments > li:nth-child(2) .cs-m-title input');
  await page.keyboard.press('Control+A');
  await page.keyboard.type('Typed by hand');
  await tabTo(page, '#moments > li:nth-child(1) .cs-linkbtn, #moments > li:nth-child(2) .cs-linkbtn, #moments > li:nth-child(2) .cs-m-links a');
  await tabTo(page, '#add-moment');
  await tabTo(page, 'input[name="layout"]');
  await page.keyboard.press('ArrowDown');
  assert.equal(await page.locator('input[name="layout"][value="fill"]').isChecked(), true);
  await tabTo(page, 'input[name="position"]');
  await page.keyboard.press('ArrowLeft');
  await tabTo(page, '#opt-captions');
  await page.keyboard.press('Space');
  await tabTo(page, '#opt-title');
  await tabTo(page, '#render-go');
  await page.keyboard.press('Enter');
  await page.waitForSelector('#results:not([hidden])');
  const body = apiLog('POST', '/api/render')[0].body;
  assert.deepEqual(body.clips, [{ start: 3922, end: 3952.5, title: 'Typed by hand' }, { start: 301.04, end: 333.04, title: 'First cut' }]);
  assert.deepEqual(body.options, { layout: 'fill', zoom: 1.3, position: 'left', captions: false, showTitle: true });

  // stage 4
  assert.equal(await page.evaluate(() => document.activeElement.id), 'h-results');
  await tabTo(page, '#clips > li:nth-child(1) video');
  await tabTo(page, '#clips > li:nth-child(1) textarea');
  await tabTo(page, '#clips > li:nth-child(1) a[download]');
  const [saved] = await Promise.all([page.waitForEvent('download'), page.keyboard.press('Enter')]);
  assert.equal(saved.suggestedFilename(), '01-typed-by-hand.mp4');
  await tabTo(page, '#clips > li:nth-child(1) button');
  await page.keyboard.press('Enter');
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'Typed by hand\nClip from Asha Builds on YouTube');
  await tabTo(page, '#folder-copy');
  await tabTo(page, '#restart');
  await page.keyboard.press('Enter');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'url');
  assert.equal(await visible(page, '#stage-results'), false);
  assert.deepEqual(problems, []);
  await close();

  // Cancel is reachable by keyboard while a job runs
  const slow = await open({ flags: { scenario: 'slow' } });
  await fillSource(slow.page);
  await slow.page.focus('#analyze-go');
  await slow.page.keyboard.press('Enter');
  await slow.page.waitForFunction(() => /Reading the video details/.test(document.querySelector('#an-step').textContent));
  assert.equal(await slow.page.evaluate(() => document.activeElement.id), 'h-progress');
  await slow.page.keyboard.press('Tab');
  assert.equal(await slow.page.evaluate(() => document.activeElement.id), 'an-cancel', 'Cancel is the very next stop');
  await assertFocusVisible(slow.page, 'Cancel');
  await slow.page.keyboard.press('Enter');
  await slow.page.waitForFunction(() => document.querySelector('#an-step').textContent === 'Cancelled. Nothing was analysed.');
  assert.deepEqual(slow.problems, []);
  await slow.close();
});

test('reduced motion: nothing animates and the flow still completes', async () => {
  const motion = async (reduced) => {
    const { page, problems, close } = await open({ context: { reducedMotion: reduced ? 'reduce' : 'no-preference' }, flags: { jobMs: 300 } });
    await page.evaluate(() => {
      window.__motion = { animations: [], smooth: [] };
      document.addEventListener('animationstart', (e) => window.__motion.animations.push(e.animationName));
      const original = Element.prototype.scrollIntoView;
      Element.prototype.scrollIntoView = function patched(options) { window.__motion.smooth.push(options && options.behavior); return original.call(this, options); };
    });
    await toMoments(page);
    await toResults(page);
    const seen = await page.evaluate(() => window.__motion);
    const bar = await page.evaluate(() => getComputedStyle(document.querySelector('#rn-bar'), '::-webkit-progress-value').transitionDuration);
    assert.equal(await page.locator('#clips > li').count(), 5);
    assert.deepEqual(problems, []);
    await close();
    return { ...seen, bar };
  };
  const normal = await motion(false);
  assert.ok(normal.animations.includes('cs-in'), 'with motion allowed, a new stage fades in');
  assert.ok(normal.smooth.every((b) => b === 'smooth') && normal.smooth.length >= 3);
  const reduced = await motion(true);
  assert.deepEqual(reduced.animations, [], 'no animation runs when reduced motion is asked for');
  assert.ok(reduced.smooth.every((b) => b === 'auto') && reduced.smooth.length >= 3, 'scrolling to a stage jumps instead of gliding');
  if (normal.bar !== reduced.bar) assert.equal(reduced.bar, '0s', 'the progress bar does not glide');
});

// -------------------------------------------------------------------- runner

async function main() {
  const only = process.argv[2] || '';
  mock = await startMock({ port: PORT });
  browser = await chromium.launch();
  let passed = 0;
  const failed = [];
  const started = Date.now();
  for (const { name, fn } of tests) {
    if (only && !name.includes(only)) continue;
    const t0 = Date.now();
    try {
      await fn();
      passed += 1;
      console.log(`ok   ${name} (${Date.now() - t0} ms)`);
    } catch (error) {
      failed.push(name);
      console.log(`FAIL ${name}\n     ${String(error && error.stack ? error.stack : error).split('\n').join('\n     ')}`);
      for (const context of browser.contexts()) await context.close().catch(() => {});
    }
  }
  await browser.close();
  await mock.close();
  console.log(`\n${passed} passed, ${failed.length} failed, in ${((Date.now() - started) / 1000).toFixed(1)} s`);
  if (!only) console.log(`Screenshots: ${SHOTS}`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((error) => { console.error(error); process.exit(1); });
