// A stand-in for the Clip Studio server, for the UI test. It serves public/ and answers the
// HTTP API described in docs/ARCHITECTURE.md with made-up data. No yt-dlp, no network, no real analysis.
//
// Run it by hand:   node tests/e2e/mock_server.mjs [port]      (default port 5412)
//
// Scenarios are switched through a control route, either as JSON or as a query string:
//   POST /__mock/control   {"scenario": "slow"}            or any of the flags below
//   GET  /__mock/control?scenario=render-one-failed
//   GET  /__mock/control?tools=no-deno&video=no-captions   (flags can be mixed)
//   GET  /__mock/log        every API request the page made, for assertions
//   POST /__mock/finish     lets slow jobs finish now
//
// Named scenarios: normal, missing-tools, analyze-error, slow, render-one-failed,
// no-captions, no-replays, upload, xss.
//
// Flags:
//   tools    ok | no-deno | no-ytdlp | no-ffmpeg | no-text | none
//   status   ok | down
//   analyze  ok | reject | fail | slow
//   render   ok | one-failed | all-failed | fail | slow | reject
//   video    normal | no-captions | no-replays | empty | long-words | xss |
//            plain-http | other-site | short-link | with-login  (the video address the analysis reports)
//   upload   ok | slow | reject
//   jobMs    how long a normal job takes (default 900)
//   dropMs   for this many milliseconds every job poll gets its connection cut (to test retry with backoff)

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.resolve(HERE, '..', '..', 'public');

// The same policy the brief asks the real server to send, so a violation shows up in the test.
export const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' https://fonts.googleapis.com",
  "font-src 'self' https://fonts.gstatic.com",
  "img-src 'self' data: https://i.ytimg.com https://*.ggpht.com",
  "media-src 'self' blob:",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join('; ');

export const XSS = {
  img: '<img src=x onerror="window.__xss=(window.__xss||0)+1">',
  script: '"><script>window.__xss=(window.__xss||0)+1</script>',
  svg: "<svg onload='window.__xss=(window.__xss||0)+1'>",
};
const xss = (label) => `${label} ${XSS.img}${XSS.script}${XSS.svg}`;

const PRESETS = {
  normal: {},
  'missing-tools': { tools: 'no-deno' },
  'analyze-error': { analyze: 'fail' },
  slow: { analyze: 'slow', render: 'slow' },
  'render-one-failed': { render: 'one-failed' },
  'no-captions': { video: 'no-captions' },
  'no-replays': { video: 'no-replays' },
  upload: { upload: 'slow' },
  xss: { video: 'xss' },
};
const OTHER_URLS = { 'plain-http': 'http://www.youtube.com/watch?v=mockvideo01', 'other-site': 'https://videos.example/watch?v=mockvideo01', 'short-link': 'https://youtu.be/mockvideo01', 'with-login': 'https://user:pw@www.youtube.com/watch?v=mockvideo01' };
const DEFAULTS = { tools: 'ok', status: 'ok', analyze: 'ok', render: 'ok', video: 'normal', upload: 'ok', jobMs: 900, dropMs: 0 };
const LIMITS = { minClipSeconds: 8, maxClipSeconds: 90, maxClips: 10, maxUploadBytes: 64 * 1024 * 1024, allowedHosts: ['youtube.com', 'youtu.be'] };
const VIDEO_EXTENSIONS = ['.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi'];
const MIME = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png', '.ico': 'image/x-icon' };

function ffmpegPath() {
  if (process.env.CLIPSTUDIO_FFMPEG) return process.env.CLIPSTUDIO_FFMPEG;
  for (const python of ['python3', 'python']) {
    try {
      const found = execFileSync(python, ['-c', 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
      if (found) return found;
    } catch { /* try the next one */ }
  }
  return 'ffmpeg';
}

/**
 * Makes the three-second upright test video the mock serves as every rendered short.
 * The real app writes H.264 and AAC. The Chromium build used for tests has no licence for those,
 * so this file uses VP9 and Opus in the same MP4 container. Seeking and Range behave the same.
 */
export function makeSampleVideo() {
  const dir = path.join(os.tmpdir(), 'clipstudio-e2e');
  const file = path.join(dir, 'sample-vp9.mp4');
  if (fs.existsSync(file) && fs.statSync(file).size > 1000) return file;
  fs.mkdirSync(dir, { recursive: true });
  const partial = `${file}.${process.pid}.tmp.mp4`;
  execFileSync(ffmpegPath(), [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', 'testsrc2=size=270x480:rate=25:duration=3',
    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
    '-c:v', 'libvpx-vp9', '-b:v', '300k', '-g', '25', '-pix_fmt', 'yuv420p',
    '-c:a', 'libopus', '-b:a', '48k', '-shortest', '-movflags', '+faststart', partial,
  ], { stdio: ['ignore', 'ignore', 'inherit'] });
  fs.renameSync(partial, file);
  return file;
}

function makeAnalysis(scenario, source, count) {
  const fromFile = source.kind === 'file';
  const bad = scenario.video === 'xss';
  const duration = 5025.4;
  const longText = 'So this is the part where the whole table nearly fell over. I had the clamps on wrong, the glue was already setting, and chat was shouting at me to flip it. I flipped it. It held. I could not believe it held. '.repeat(3).trim();
  const base = [
    { start: 1512.3, score: 100, reasons: ['Viewers replayed this part', 'Loud reaction'], text: longText, title: 'The table nearly fell over' },
    { start: 301.04, score: 44, reasons: ['Stronger than the rest of the video'], text: 'Okay, first cut.', title: 'First cut' },
    { start: 3920.5, score: 87, reasons: ['Loud reaction', 'Excited or fast speech'], text: 'No way. No way! Look at that grain. That is the best board I have ever cut, and I found it in a skip.', title: 'Best board I ever cut' },
    { start: 2204, score: 61, reasons: ['Excited or fast speech'], text: 'People ask why I do not use screws here. Three reasons, quickly.', title: 'Why no screws' },
    { start: 4630.2, score: 73, reasons: ['Viewers replayed this part'], text: '', title: 'The finished table' },
    { start: 860, score: 38, reasons: ['Stronger than the rest of the video'], text: 'Sanding. A lot of sanding.', title: 'Sanding' },
    { start: 1890, score: 35, reasons: ['Loud reaction'], text: 'That was the dog, sorry.', title: 'The dog joins in' },
    { start: 2710, score: 31, reasons: ['Excited or fast speech'], text: 'Glue goes everywhere if you rush.', title: 'Glue everywhere' },
    { start: 3300, score: 27, reasons: ['Stronger than the rest of the video'], text: 'Second coat of oil.', title: 'Second coat' },
    { start: 4410, score: 22, reasons: ['Stronger than the rest of the video'], text: 'Flipping it over for the last time.', title: 'Last flip' },
  ];
  const length = source.clipLength;
  let moments = base.slice(0, count).map((m, i) => ({
    id: `mom-${i + 1}`,
    start: m.start,
    end: Math.round((m.start + length) * 100) / 100,
    score: m.score,
    reasons: bad ? m.reasons.map((r) => xss(r)) : m.reasons,
    text: bad ? xss(`text ${i + 1}`) : m.text,
    title: bad ? xss(`t${i + 1}`).slice(0, 78) : m.title,
  }));
  let note = '';
  let signals = { replays: !fromFile, audio: true, speech: true };
  let captionLabel = 'en (automatic)';
  if (scenario.video === 'no-captions') { signals = { ...signals, speech: false }; captionLabel = ''; moments = moments.map((m) => ({ ...m, text: '', reasons: m.reasons.filter((r) => !/speech/.test(r)).concat(m.reasons.every((r) => /speech/.test(r)) ? ['Loud reaction'] : []) })); }
  if (scenario.video === 'no-replays') { signals = { ...signals, replays: false }; note = 'YouTube has no replay data for this video, so the moments come from the audio and the captions.'; }
  if (scenario.video === 'empty') { moments = []; note = 'The video is shorter than the clip length you chose.'; }
  if (scenario.video === 'long-words') {
    moments = moments.map((m) => ({ ...m, title: 'Pneumonoultramicroscopicsilicovolcanoconiosis'.repeat(2).slice(0, 80), text: 'W'.repeat(300), reasons: ['Supercalifragilisticexpialidocious'.repeat(3)] }));
    note = 'A'.repeat(200);
  }
  if (bad) { note = xss('note'); captionLabel = xss('cap'); }
  const title = fromFile ? source.name : 'Weekend stream: building an oak table from a skip find';
  return {
    video: {
      title: bad ? xss('title') : scenario.video === 'long-words' ? 'Longtitlewithoutanyspaces'.repeat(8) : title,
      channel: bad ? xss('channel') : fromFile ? '' : scenario.video === 'long-words' ? 'Channelnamewithoutspaces'.repeat(6) : 'Asha Builds',
      duration,
      url: bad ? 'javascript:window.__xss=(window.__xss||0)+1' : fromFile ? null : OTHER_URLS[scenario.video] || 'https://www.youtube.com/watch?v=mockvideo01',
      thumbnail: bad ? 'javascript:window.__xss=(window.__xss||0)+1' : fromFile ? '' : 'https://i.ytimg.com/vi/mockvideo01/hqdefault.jpg',
      wasLive: !fromFile,
    },
    signals,
    captionLabel,
    note,
    moments,
  };
}

export function startMock({ port = 5412 } = {}) {
  const sample = makeSampleVideo();
  const sampleSize = fs.statSync(sample).size;
  let seq = 0;
  let scenario = { ...DEFAULTS };
  let jobs = new Map();
  let uploads = new Map();
  let analyses = new Map();
  let log = [];
  let dropUntil = 0;
  const sockets = new Set();

  const reset = (flags = {}) => {
    const preset = flags.scenario ? PRESETS[flags.scenario] : {};
    if (!preset) throw new Error(`Unknown scenario: ${flags.scenario}`);
    if (!flags.keep) { jobs = new Map(); uploads = new Map(); analyses = new Map(); log = []; scenario = { ...DEFAULTS }; dropUntil = 0; }
    for (const [key, value] of Object.entries({ ...preset, ...flags })) {
      if (key === 'scenario' || key === 'keep') continue;
      if (!(key in DEFAULTS)) throw new Error(`Unknown flag: ${key}`);
      scenario[key] = typeof DEFAULTS[key] === 'number' ? Number(value) : String(value);
      if (key === 'dropMs') dropUntil = Date.now() + scenario.dropMs;
    }
    return scenario;
  };

  const headers = (extra = {}) => ({
    'Content-Security-Policy': CSP,
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Cache-Control': 'no-store',
    ...extra,
  });
  const json = (res, status, body) => {
    const data = Buffer.from(JSON.stringify(body));
    res.writeHead(status, headers({ 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': data.length }));
    res.end(data);
  };
  const fail = (res, status, code, message, hint) => json(res, status, { error: { status, code, message, ...(hint ? { hint } : {}) } });
  const readBody = (req, { slow = false, limit = 80 * 1024 * 1024 } = {}) => new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) { reject(new Error('too large')); req.destroy(); return; }
      chunks.push(chunk);
      if (slow) { req.pause(); setTimeout(() => req.resume(), 6); }
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
    req.on('aborted', () => reject(new Error('aborted')));
  });

  const newJob = (kind, extra) => {
    const id = `job-${kind}-${++seq}`;
    const mode = scenario[kind];
    const job = { id, kind, created: Date.now(), ms: mode === 'slow' ? 10 * 60 * 1000 : scenario.jobMs, mode, cancelled: false, ...extra };
    jobs.set(id, job);
    return job;
  };

  const describeJob = (job) => {
    const bad = scenario.video === 'xss';
    const out = { id: job.id, kind: job.kind };
    if (job.cancelled) return { ...out, state: 'cancelled', step: 'Cancelled', progress: job.cancelledAt };
    const elapsed = Date.now() - job.created;
    const progress = Math.min(1, elapsed / job.ms);
    if (elapsed < 60 && job.mode !== 'slow') return { ...out, state: 'queued', step: 'Waiting to start', progress: 0 };
    if (progress < 1) {
      let step;
      if (job.kind === 'analyze') step = progress < 0.34 ? 'Reading the video details' : progress < 0.67 ? 'Listening to the audio' : 'Finding the strongest moments';
      else step = `Making short ${Math.min(job.clips.length, 1 + Math.floor(progress * job.clips.length))} of ${job.clips.length}`;
      return { ...out, state: 'running', step: bad ? xss(step) : step, progress: Math.round(progress * 1000) / 1000 };
    }
    if (job.mode === 'fail') {
      const error = job.kind === 'analyze'
        ? { message: bad ? xss('error') : 'YouTube says this video is unavailable.', hint: bad ? xss('hint') : 'Check the link, or try another video.' }
        : { message: 'ffmpeg stopped before any short was finished.', hint: 'Look at clip-log.txt for the details, then try again.' };
      return { ...out, state: 'error', step: 'Stopped', progress: 1, error };
    }
    return { ...out, state: 'done', step: 'Finished', progress: 1, result: job.kind === 'analyze' ? analyzeResult(job) : renderResult(job) };
  };

  const analyzeResult = (job) => {
    if (!analyses.has(job.analysisId)) analyses.set(job.analysisId, makeAnalysis(job.scenario, job.source, job.count));
    return { analysisId: job.analysisId, ...analyses.get(job.analysisId) };
  };

  const renderResult = (job) => {
    const bad = job.scenario.video === 'xss';
    const analysis = analyses.get(job.analysisId);
    const channel = analysis ? analysis.video.channel : '';
    const clips = job.clips.map((clip, i) => {
      const failed = job.mode === 'all-failed' || (job.mode === 'one-failed' && i === 1);
      const name = bad ? `${String(i + 1).padStart(2, '0')}-${XSS.img}.mp4` : `${String(i + 1).padStart(2, '0')}-${(clip.title || 'clip').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'clip'}.mp4`;
      const common = { index: i + 1, name, start: clip.start, end: clip.end, seconds: Math.round((clip.end - clip.start) * 10) / 10 };
      if (failed) return { ...common, ok: false, error: bad ? xss('clip error') : 'ffmpeg ran out of memory while making this short. Close other programs and try again.', url: '', bytes: 0, caption: '' };
      return { ...common, ok: true, url: `/output/mock-run/${String(i + 1).padStart(2, '0')}.mp4`, bytes: sampleSize, caption: bad ? xss('caption') : [clip.title, channel ? `Clip from ${channel} on YouTube` : ''].filter(Boolean).join('\n') };
    });
    if (bad && clips.length > 1) clips[clips.length - 1] = { ...clips[clips.length - 1], url: 'javascript:window.__xss=(window.__xss||0)+1' };
    if (bad && clips.length > 2) clips[clips.length - 2] = { ...clips[clips.length - 2], url: '//evil.example/output/x.mp4' };
    return { folder: bad ? xss('C:\\Users\\Asha\\Videos') : 'C:\\Users\\Asha\\Videos\\Clip Studio\\weekend-stream-2026-11-21', clips };
  };

  const serveFile = (req, res, file, type) => {
    const size = fs.statSync(file).size;
    const range = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || '');
    if (!range) {
      res.writeHead(200, headers({ 'Content-Type': type, 'Content-Length': size, 'Accept-Ranges': 'bytes' }));
      if (req.method === 'HEAD') res.end(); else fs.createReadStream(file).pipe(res);
      return;
    }
    let start = range[1] === '' ? Math.max(0, size - Number(range[2])) : Number(range[1]);
    let end = range[1] === '' || range[2] === '' ? size - 1 : Math.min(size - 1, Number(range[2]));
    if (start > end || start >= size) {
      res.writeHead(416, headers({ 'Content-Range': `bytes */${size}` }));
      res.end();
      return;
    }
    res.writeHead(206, headers({ 'Content-Type': type, 'Content-Length': end - start + 1, 'Content-Range': `bytes ${start}-${end}/${size}`, 'Accept-Ranges': 'bytes' }));
    if (req.method === 'HEAD') res.end(); else fs.createReadStream(file, { start, end }).pipe(res);
  };

  const serveStatic = (req, res, pathname) => {
    let rel;
    try { rel = decodeURIComponent(pathname); } catch { return fail(res, 400, 'bad_path', 'That address is not valid.'); }
    if (rel.endsWith('/')) rel += 'index.html';
    const file = path.resolve(PUBLIC, `.${rel}`);
    if (file !== PUBLIC && !file.startsWith(PUBLIC + path.sep)) return fail(res, 404, 'not_found', 'Nothing is here.');
    if (!fs.existsSync(file) || !fs.statSync(file).isFile()) return fail(res, 404, 'not_found', 'Nothing is here.');
    const data = fs.readFileSync(file);
    res.writeHead(200, headers({ 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream', 'Content-Length': data.length }));
    res.end(req.method === 'HEAD' ? undefined : data);
  };

  const parseJson = (buffer) => { try { const v = JSON.parse(buffer.toString('utf8')); return v && typeof v === 'object' && !Array.isArray(v) ? v : null; } catch { return null; } };

  const handle = async (req, res) => {
    const url = new URL(req.url, `http://127.0.0.1:${port}`);
    const p = url.pathname;

    // ---- control routes, used only by the test
    if (p === '/__mock/control') {
      const flags = req.method === 'POST' ? parseJson(await readBody(req)) || {} : Object.fromEntries(url.searchParams);
      try { return json(res, 200, { scenario: reset(flags) }); } catch (error) { return fail(res, 400, 'bad_control', error.message); }
    }
    if (p === '/__mock/log') return json(res, 200, { log });
    if (p === '/__mock/finish') { for (const job of jobs.values()) { job.ms = 1; job.mode = job.mode === 'slow' ? 'ok' : job.mode; } return json(res, 200, { ok: true }); }

    if (p.startsWith('/api/') || p.startsWith('/output/')) log.push({ at: Date.now(), method: req.method, path: p, range: req.headers.range || null, contentType: req.headers['content-type'] || null, origin: req.headers.origin || null, filename: req.headers['x-filename'] || null });
    const entry = log[log.length - 1];

    if (p === '/api/status' && req.method === 'GET') {
      if (scenario.status === 'down') return fail(res, 500, 'internal', 'Something went wrong inside the app.');
      const bad = scenario.video === 'xss';
      const t = scenario.tools;
      const tools = {
        ytdlp: t === 'no-ytdlp' || t === 'none' ? null : bad ? xss('2026.11.01') : '2026.11.01',
        ffmpeg: t === 'no-ffmpeg' || t === 'none' ? null : bad ? xss('7.1') : '7.1',
        deno: t === 'no-deno' || t === 'none' ? null : bad ? xss('2.5.4') : '2.5.4',
        captions_filter: !(t === 'no-text' || t === 'no-ffmpeg' || t === 'none'),
      };
      return json(res, 200, { version: bad ? xss('1.0.0') : '1.0.0', tools, limits: LIMITS });
    }

    if (p === '/api/uploads' && req.method === 'POST') {
      let name;
      try { name = decodeURIComponent(req.headers['x-filename'] || ''); } catch { name = ''; }
      const ext = path.extname(name).toLowerCase();
      let body;
      try { body = await readBody(req, { slow: scenario.upload === 'slow', limit: LIMITS.maxUploadBytes }); } catch { if (!res.headersSent && !res.destroyed) fail(res, 413, 'too_large', 'That file is larger than the app accepts.'); return; }
      entry.bytes = body.length;
      entry.decodedName = name;
      if (scenario.upload === 'reject') return fail(res, 507, 'no_space', 'There is not enough free disk space to copy this file.', 'Free some space, then try again.');
      if (!name || !VIDEO_EXTENSIONS.includes(ext)) return fail(res, 400, 'bad_file_type', 'That file type is not accepted.', 'Choose an MP4, MOV, MKV, WebM, M4V or AVI video.');
      if (!body.length) return fail(res, 400, 'empty_upload', 'The file arrived empty.');
      const uploadId = `up-${++seq}`;
      uploads.set(uploadId, { name, bytes: body.length });
      return json(res, 201, { uploadId, name, bytes: body.length });
    }

    if (p === '/api/analyze' && req.method === 'POST') {
      const body = parseJson(await readBody(req));
      entry.body = body;
      if (!body) return fail(res, 400, 'bad_json', 'The request was not valid JSON.');
      if (body.permission !== true) return fail(res, 400, 'permission_required', 'Confirm that you made this video or have permission to clip it.');
      const hasUrl = typeof body.url === 'string' && body.url !== '';
      const hasUpload = typeof body.uploadId === 'string' && body.uploadId !== '';
      if (hasUrl === hasUpload) return fail(res, 400, 'one_source', 'Send either a link or an uploaded file, not both.');
      if (!Number.isInteger(body.clipLength) || body.clipLength < 8 || body.clipLength > 90) return fail(res, 400, 'bad_clip_length', 'The clip length must be a whole number from 8 to 90 seconds.');
      if (!Number.isInteger(body.count) || body.count < 1 || body.count > 10) return fail(res, 400, 'bad_count', 'The number of clips must be from 1 to 10.');
      let source;
      if (hasUrl) {
        let u = null;
        try { u = new URL(body.url); } catch { /* handled below */ }
        const host = u ? u.hostname.toLowerCase() : '';
        const ok = u && /^https?:$/.test(u.protocol) && !u.username && !u.password && LIMITS.allowedHosts.some((h) => [h, `www.${h}`, `m.${h}`, `music.${h}`].includes(host));
        if (!ok) return fail(res, 400, 'url_not_allowed', 'That link is not from a site this app accepts.', 'Paste a link from youtube.com or youtu.be.');
        source = { kind: 'url', url: body.url };
      } else {
        if (!uploads.has(body.uploadId)) return fail(res, 404, 'upload_not_found', 'That uploaded file is no longer in the app.', 'Choose the file again.');
        source = { kind: 'file', name: uploads.get(body.uploadId).name };
      }
      if (scenario.analyze === 'reject') return fail(res, 400, 'url_not_allowed', scenario.video === 'xss' ? xss('rejected') : 'That does not look like a video link.', scenario.video === 'xss' ? xss('hint') : 'Paste the full address of a YouTube video or finished stream.');
      const job = newJob('analyze', { analysisId: `an-${++seq}`, scenario: { ...scenario }, source: { ...source, clipLength: body.clipLength }, count: body.count });
      return json(res, 202, { jobId: job.id });
    }

    if (p === '/api/render' && req.method === 'POST') {
      const body = parseJson(await readBody(req));
      entry.body = body;
      if (!body) return fail(res, 400, 'bad_json', 'The request was not valid JSON.');
      const analysis = analyses.get(body.analysisId);
      if (!analysis) return fail(res, 404, 'analysis_not_found', 'That analysis is no longer in the app.', 'Analyse the video again.');
      if (!Array.isArray(body.clips) || !body.clips.length || body.clips.length > LIMITS.maxClips) return fail(res, 400, 'bad_clips', 'Send from 1 to 10 clips.');
      for (const clip of body.clips) {
        const okShape = clip && typeof clip === 'object' && typeof clip.start === 'number' && typeof clip.end === 'number' && typeof clip.title === 'string' && Object.keys(clip).every((k) => ['start', 'end', 'title'].includes(k));
        if (!okShape) return fail(res, 400, 'bad_clip', 'Each clip needs a start, an end and a title.');
        const length = clip.end - clip.start;
        if (clip.start < 0 || clip.end > analysis.video.duration + 0.05 || length < LIMITS.minClipSeconds - 0.001 || length > LIMITS.maxClipSeconds + 0.001) return fail(res, 400, 'bad_clip_times', 'A clip is outside the video or not between 8 and 90 seconds long.');
        if (clip.title.length > 80) return fail(res, 400, 'bad_title', 'A title is longer than 80 characters.');
      }
      const o = body.options;
      const okOptions = o && typeof o === 'object' && ['blur', 'fill'].includes(o.layout) && typeof o.zoom === 'number' && o.zoom >= 1 && o.zoom <= 2 && ['left', 'center', 'right'].includes(o.position) && typeof o.captions === 'boolean' && typeof o.showTitle === 'boolean' && Object.keys(o).length === 5;
      if (!okOptions) return fail(res, 400, 'bad_options', 'The render options are not valid.');
      if (scenario.render === 'reject') return fail(res, 409, 'busy', 'The app is still finishing another set of shorts.', 'Wait a moment, then try again.');
      const job = newJob('render', { analysisId: body.analysisId, clips: body.clips, scenario: { ...scenario } });
      return json(res, 202, { jobId: job.id });
    }

    const jobMatch = /^\/api\/jobs\/([^/]+)(\/cancel)?$/.exec(p);
    if (jobMatch) {
      const job = jobs.get(decodeURIComponent(jobMatch[1]));
      if (jobMatch[2] && req.method === 'POST') {
        if (!job) return fail(res, 404, 'job_not_found', 'That job is not in the app.');
        const now = describeJob(job);
        if (now.state === 'queued' || now.state === 'running') { job.cancelled = true; job.cancelledAt = now.progress; }
        const after = describeJob(job);
        return json(res, 200, { id: job.id, state: after.state });
      }
      if (!jobMatch[2] && req.method === 'GET') {
        if (Date.now() < dropUntil) { entry.dropped = true; req.socket.destroy(); return; }
        if (!job) return fail(res, 404, 'job_not_found', 'That job is not in the app.');
        return json(res, 200, describeJob(job));
      }
    }

    if (p.startsWith('/output/') && (req.method === 'GET' || req.method === 'HEAD')) {
      if (!/^\/output\/[\w-]+\/[\w-]+\.mp4$/.test(p)) return fail(res, 404, 'not_found', 'Nothing is here.');
      return serveFile(req, res, sample, 'video/mp4');
    }
    if (p.startsWith('/api/')) return fail(res, 404, 'not_found', 'Nothing is here.');
    if (req.method !== 'GET' && req.method !== 'HEAD') return fail(res, 405, 'method_not_allowed', 'That is not allowed here.');
    return serveStatic(req, res, p);
  };

  const server = http.createServer((req, res) => {
    handle(req, res).catch((error) => {
      if (!res.headersSent && !res.destroyed) fail(res, 500, 'internal', 'The mock server hit a problem.');
      console.error('[mock]', error);
    });
  });
  server.on('connection', (socket) => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });

  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => resolve({
      url: `http://127.0.0.1:${port}`,
      port,
      sample,
      sampleSize,
      control: (flags) => reset(flags),
      get log() { return log; },
      finishJobs() { for (const job of jobs.values()) { job.ms = 1; if (job.mode === 'slow') job.mode = 'ok'; } },
      close: () => new Promise((done) => { for (const socket of sockets) socket.destroy(); server.close(() => done()); }),
    }));
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.argv[2]) || 5412;
  const mock = await startMock({ port });
  console.log(`Mock Clip Studio server on ${mock.url}  (scenarios: ${mock.url}/__mock/control?scenario=slow)`);
}
