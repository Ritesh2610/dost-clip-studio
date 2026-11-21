'use strict';
/* Dost Clip Studio: the page logic.
   Text that comes from the server (titles, names, messages) is only ever placed with
   textContent or as a form value, never as HTML. Styles are set through classes and CSSOM. */
(() => {
  const { $, $$, el } = K;

  const DEFAULT_LIMITS = { minClipSeconds: 8, maxClipSeconds: 90, maxClips: 10, maxUploadBytes: 4 * 1024 ** 3, allowedHosts: ['youtube.com', 'youtu.be'] };
  const DEFAULT_OPTIONS = { clipLength: 30, count: 5, layout: 'blur', zoom: 1.3, position: 'center', captions: true, showTitle: true };
  const VIDEO_EXTENSIONS = ['.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi'];
  const OPTIONS_KEY = 'clipstudio.options';
  const SESSION_KEY = 'clipstudio.session';
  const TITLE_MAX = 80;
  const POLL_MS = 700;
  const POLL_MAX_MS = 8000;
  const MESSAGE_DELAY_MS = 600;
  const STARTER = /Windows|Win32|Win64/i.test(`${navigator.platform} ${navigator.userAgent}`) ? 'start.bat' : 'start.sh';
  const INSTALL_HINT = `Run ${STARTER} again so it can be installed.`;
  const NOT_ANSWERING = 'The app is not answering. Check that the Clip Studio window is still open, then try again.';
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  const text = (value) => (typeof value === 'string' ? value : typeof value === 'number' && Number.isFinite(value) ? String(value) : '');
  const num = (value, fallback = 0) => (typeof value === 'number' && Number.isFinite(value) ? value : fallback);
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const listWords = (items) => (items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`);

  const state = {
    limits: { ...DEFAULT_LIMITS },
    tools: null,
    options: loadOptions(),
    file: null,
    sourceUrl: '',
    uploadId: null,
    uploadXhr: null,
    busy: null, // 'analyze' | 'render' | null
    analysis: null,
    cards: [],
    jobs: { analyze: null, render: null },
    watch: { analyze: null, render: null },
    nextKey: 1,
  };

  // ------------------------------------------------------------ small helpers

  class ApiError extends Error {
    constructor(message, { status = 0, code = '', hint = '' } = {}) {
      super(message);
      this.status = status;
      this.code = code;
      this.hint = hint;
    }
  }

  function errorFromAnswer(status, data) {
    const e = data && typeof data === 'object' ? data.error : null;
    const message = e && text(e.message) ? text(e.message) : `The app answered with an error (${status}). Look at the Clip Studio window for details.`;
    return new ApiError(message, { status, code: e ? text(e.code) : '', hint: e ? text(e.hint) : '' });
  }

  async function api(method, path, body) {
    let response;
    try {
      response = await fetch(path, {
        method,
        cache: 'no-store',
        headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new ApiError(NOT_ANSWERING, { code: 'network' });
    }
    let data = null;
    try { data = await response.json(); } catch { /* not JSON, handled below */ }
    if (!response.ok) throw errorFromAnswer(response.status, data);
    if (!data || typeof data !== 'object') throw new ApiError('The app gave an answer this page could not read. Reload the page and try again.', { code: 'bad_answer' });
    return data;
  }

  function loadOptions() {
    const saved = K.load(OPTIONS_KEY, null);
    const o = { ...DEFAULT_OPTIONS };
    if (!saved || typeof saved !== 'object') return o;
    if (Number.isInteger(saved.clipLength)) o.clipLength = clamp(saved.clipLength, 10, 90);
    if (Number.isInteger(saved.count)) o.count = clamp(saved.count, 1, 10);
    if (saved.layout === 'blur' || saved.layout === 'fill') o.layout = saved.layout;
    if (typeof saved.zoom === 'number' && Number.isFinite(saved.zoom)) o.zoom = clamp(saved.zoom, 1, 2);
    if (['left', 'center', 'right'].includes(saved.position)) o.position = saved.position;
    if (typeof saved.captions === 'boolean') o.captions = saved.captions;
    if (typeof saved.showTitle === 'boolean') o.showTitle = saved.showTitle;
    return o;
  }
  const saveOptions = () => K.save(OPTIONS_KEY, state.options);

  function loadSession() {
    try {
      const raw = sessionStorage.getItem(SESSION_KEY);
      const data = raw ? JSON.parse(raw) : null;
      return data && typeof data === 'object' ? data : {};
    } catch { return {}; }
  }
  function saveSession() {
    const data = {
      url: state.sourceUrl,
      analyzeJob: state.jobs.analyze,
      renderJob: state.jobs.render,
      analysisId: state.analysis ? state.analysis.id : null,
      cards: state.analysis ? state.cards.map((c) => ({
        id: c.id, custom: c.custom, on: c.on, title: c.title,
        startText: c.startText, endText: c.endText, startEdited: c.startEdited, endEdited: c.endEdited,
      })) : null,
    };
    try {
      if (data.analyzeJob || data.renderJob) sessionStorage.setItem(SESSION_KEY, JSON.stringify(data));
      else sessionStorage.removeItem(SESSION_KEY);
    } catch { /* storage is blocked: the page still works, it just cannot resume after a reload */ }
  }

  /** "1:05", "1:05.5", "1:02:03" or plain seconds. Returns seconds, null when empty, NaN when unreadable. */
  function parseTime(raw) {
    const s = String(raw).trim().replace(',', '.');
    if (!s) return null;
    let m = /^(\d{1,2}):([0-5]\d):([0-5]\d(?:\.\d{1,2})?)$/.exec(s);
    if (m) return Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
    m = /^(\d{1,3}):([0-5]\d(?:\.\d{1,2})?)$/.exec(s);
    if (m) return Number(m[1]) * 60 + Number(m[2]);
    if (/^\d{1,6}(?:\.\d{1,2})?$/.test(s)) return Number(s);
    return NaN;
  }

  /** 65.5 -> "1:05.5"; with tenths off, 65.5 -> "1:05". Hours appear only when needed. */
  function formatTime(seconds, tenths = true) {
    const total = Math.max(0, tenths ? Math.round(seconds * 10) / 10 : Math.floor(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total - h * 3600) / 60);
    const s = total - h * 3600 - m * 60;
    const ss = tenths ? s.toFixed(1).padStart(4, '0') : String(s).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
  }

  function hostAllowed(hostname) {
    const host = String(hostname).toLowerCase().replace(/\.$/, '');
    return state.limits.allowedHosts.some((allowed) => [allowed, `www.${allowed}`, `m.${allowed}`, `music.${allowed}`].includes(host));
  }

  /** The video address from the server, or '' unless it is a plain https address on an accepted site. */
  function safeVideoUrl(raw) {
    if (typeof raw !== 'string' || !raw || raw.length > 2000) return '';
    let u;
    try { u = new URL(raw); } catch { return ''; }
    if (u.protocol !== 'https:' || u.username || u.password || u.port || !hostAllowed(u.hostname)) return '';
    return u.href;
  }

  /** A rendered file address from the server, or '' unless it points into /output/ on this app. */
  function safeOutputUrl(raw) {
    if (typeof raw !== 'string' || !raw.startsWith('/output/')) return '';
    let u;
    try { u = new URL(raw, window.location.origin); } catch { return ''; }
    return u.origin === window.location.origin && u.pathname.startsWith('/output/') ? u.pathname + u.search : '';
  }

  function safeThumbnail(raw) {
    if (typeof raw !== 'string' || !raw) return '';
    let u;
    try { u = new URL(raw); } catch { return ''; }
    if (u.protocol !== 'https:') return '';
    return u.hostname === 'i.ytimg.com' || u.hostname.endsWith('.ggpht.com') ? u.href : '';
  }

  /** Puts a message and an optional next step into a status or alert element. */
  function setMessage(node, message, hint = '') {
    const parts = [];
    if (message) parts.push(document.createTextNode(message));
    if (hint) parts.push(el('span', { class: 'cs-hint' }, hint));
    node.replaceChildren(...parts);
  }

  // ------------------------------------------------------------------ stages

  const STAGES = ['source', 'progress', 'moments', 'results'];

  function setStep(name) {
    const at = STAGES.indexOf(name);
    $$('#steps li').forEach((li, i) => {
      li.classList.toggle('done', i < at);
      if (i === at) li.setAttribute('aria-current', 'step');
      else li.removeAttribute('aria-current');
    });
  }

  function reveal(name, { focus = true } = {}) {
    const section = $(`#stage-${name}`);
    const wasHidden = section.hidden;
    section.hidden = false;
    if (wasHidden && focus) {
      section.classList.add('cs-in');
      section.addEventListener('animationend', () => section.classList.remove('cs-in'), { once: true });
    }
    if (focus) {
      $('h2', section).focus({ preventScroll: true });
      section.scrollIntoView({ behavior: reducedMotion.matches ? 'auto' : 'smooth', block: 'start' });
    }
  }

  function hide(name) { $(`#stage-${name}`).hidden = true; }

  function progressView(prefix) {
    const root = $(`#${prefix}`);
    const step = $(`#${prefix}-step`);
    const bar = $(`#${prefix}-bar`);
    const pct = $(`#${prefix}-pct`);
    const cancel = $(`#${prefix}-cancel`);
    const error = $(`#${prefix}-error`);
    let last = null;
    const view = {
      cancel,
      reset() {
        root.classList.remove('is-over');
        cancel.disabled = false;
        view.cancelling(false);
        view.progress(0);
        view.step('Starting.');
        setMessage(error, '');
      },
      /** Only touches the page when the words change, so screen readers hear each step once. */
      step(words, hint = '') {
        const key = `${words}\n${hint}`;
        if (key === last) return;
        last = key;
        setMessage(step, words, hint);
      },
      progress(fraction) {
        const p = clamp(num(fraction), 0, 1);
        bar.value = p;
        const label = `${Math.round(p * 100)}%`;
        if (pct.textContent !== label) pct.textContent = label;
      },
      /** A second press does nothing, but the button keeps keyboard focus until the job has stopped. */
      cancelling(on) {
        if (on) cancel.setAttribute('aria-disabled', 'true');
        else cancel.removeAttribute('aria-disabled');
      },
      /** The job is over (done, failed or cancelled): the Cancel button goes away. */
      over() {
        const hadFocus = document.activeElement === cancel;
        root.classList.add('is-over');
        cancel.disabled = true;
        if (hadFocus) $('h2', root.closest('section')).focus({ preventScroll: true });
      },
      fail(message, hint = '') {
        view.step('Stopped because of a problem.');
        setMessage(error, message, hint);
        view.over();
      },
    };
    return view;
  }

  const views = { analyze: progressView('an'), render: progressView('rn') };

  // ---------------------------------------------------------------- status

  async function loadStatus() {
    try {
      const status = await api('GET', '/api/status');
      const tools = status.tools && typeof status.tools === 'object' ? status.tools : {};
      state.tools = { ytdlp: text(tools.ytdlp), ffmpeg: text(tools.ffmpeg), deno: text(tools.deno), captionsFilter: tools.captions_filter !== false };
      const limits = status.limits && typeof status.limits === 'object' ? status.limits : {};
      for (const key of ['minClipSeconds', 'maxClipSeconds', 'maxClips', 'maxUploadBytes']) {
        if (typeof limits[key] === 'number' && limits[key] > 0) state.limits[key] = limits[key];
      }
      const hosts = Array.isArray(limits.allowedHosts) ? limits.allowedHosts.filter((h) => typeof h === 'string' && /^[a-z0-9.-]{1,100}$/i.test(h)).map((h) => h.toLowerCase()) : [];
      if (hosts.length) state.limits.allowedHosts = hosts;
      if (text(status.version)) $('#version').textContent = `Version ${text(status.version)}`;
    } catch {
      state.tools = null;
    }
    applyLimits();
    renderTools();
    syncTextOptions();
  }

  function applyLimits() {
    const length = $('#clip-length');
    length.min = String(Math.max(10, Math.ceil(state.limits.minClipSeconds)));
    length.max = String(Math.min(90, Math.floor(state.limits.maxClipSeconds)));
    $('#clip-count').max = String(clamp(Math.floor(state.limits.maxClips), 1, 10));
    $('#url-help').textContent = `Accepted sites: ${listWords(state.limits.allowedHosts)}.`;
    syncSourceOutputs();
  }

  function renderTools() {
    const box = $('#tools');
    const t = state.tools;
    if (!t) {
      box.replaceChildren(el('p', { class: 'cs-warn' }, 'The app is not answering, so the installed tools could not be checked. Check that the Clip Studio window is still open, then reload this page.'));
      return;
    }
    const lines = [];
    const have = [];
    if (t.ytdlp) have.push(`yt-dlp ${t.ytdlp} (opens video links)`);
    if (t.ffmpeg) have.push(`ffmpeg ${t.ffmpeg} (cuts and renders video)`);
    if (t.deno) have.push(`Deno ${t.deno} (helper for YouTube links)`);
    if (have.length) lines.push(el('p', { class: 'k-meta' }, `Installed on this computer: ${listWords(have)}.`));
    if (!t.ffmpeg) lines.push(el('p', { class: 'cs-warn' }, `ffmpeg is missing, so videos cannot be read or rendered. ${INSTALL_HINT}`));
    if (!t.ytdlp) lines.push(el('p', { class: 'cs-warn' }, `yt-dlp is missing, so video links cannot be opened. You can still choose a file. ${INSTALL_HINT}`));
    if (!t.deno) lines.push(el('p', { class: 'cs-warn' }, `Deno is missing, so YouTube links will probably fail. ${INSTALL_HINT}`));
    if (t.ffmpeg && !t.captionsFilter) lines.push(el('p', { class: 'cs-warn' }, `This copy of ffmpeg cannot draw text on video, so captions and titles are switched off. ${INSTALL_HINT.replace('it can', 'the right one can')}`));
    box.replaceChildren(...lines);
  }

  // ---------------------------------------------------------------- source

  const sourceError = $('#source-error');

  function syncSourceOutputs() {
    const length = $('#clip-length');
    const count = $('#clip-count');
    const lengthWords = `${length.value} seconds`;
    const countWords = plural(Number(count.value), 'clip');
    $('#clip-length-out').textContent = lengthWords;
    $('#clip-count-out').textContent = countWords;
    length.setAttribute('aria-valuetext', lengthWords);
    count.setAttribute('aria-valuetext', countWords);
  }

  function fileProblem(file) {
    const dot = file.name.lastIndexOf('.');
    const ext = dot < 0 ? '' : file.name.slice(dot).toLowerCase();
    if (!VIDEO_EXTENSIONS.includes(ext)) return 'That file type is not accepted. Choose an MP4, MOV, MKV, WebM, M4V or AVI video.';
    if (!file.size) return 'That file is empty (0 bytes). Choose a different file.';
    if (file.size > state.limits.maxUploadBytes) return `That file is ${K.bytes(file.size)}, which is over the limit of ${K.bytes(state.limits.maxUploadBytes)}. Choose a smaller file.`;
    return '';
  }

  function chooseFile(files) {
    if (state.busy) return;
    const file = files[0];
    const problem = fileProblem(file);
    if (problem) { showSourceError(problem, $('#file-drop')); return; }
    clearSourceError();
    stopUpload();
    state.file = file;
    state.uploadId = null;
    $('#file-name').textContent = file.name;
    $('#file-size').textContent = K.bytes(file.size);
    $('#upload').hidden = true;
    $('#upload-status').textContent = '';
    $('#file-row').hidden = false;
    $('#file-remove').focus();
  }

  function removeFile() {
    stopUpload();
    state.file = null;
    state.uploadId = null;
    $('#file-row').hidden = true;
    $('#file-name').textContent = '';
    $('#file-size').textContent = '';
    $('#upload-status').textContent = '';
  }

  function stopUpload() {
    if (state.uploadXhr) state.uploadXhr.abort();
    state.uploadXhr = null;
  }

  function setUploadProgress(fraction) {
    const p = clamp(fraction, 0, 1);
    $('#upload-bar').value = p;
    $('#upload-pct').textContent = `${Math.round(p * 100)}%`;
  }

  /** Sends the file to the app. fetch cannot report how much has been sent, so this uses XMLHttpRequest. */
  function uploadFile(file) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      state.uploadXhr = xhr;
      xhr.open('POST', '/api/uploads');
      xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.addEventListener('progress', (e) => { if (e.lengthComputable && e.total) setUploadProgress(e.loaded / e.total); });
      xhr.addEventListener('load', () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { /* handled below */ }
        if (xhr.status >= 200 && xhr.status < 300 && data && text(data.uploadId)) resolve(text(data.uploadId));
        else if (xhr.status >= 200 && xhr.status < 300) reject(new ApiError('The app gave an answer this page could not read. Reload the page and try again.', { code: 'bad_answer' }));
        else reject(errorFromAnswer(xhr.status, data));
      });
      xhr.addEventListener('error', () => reject(new ApiError(NOT_ANSWERING, { code: 'network' })));
      xhr.addEventListener('abort', () => reject(new ApiError('', { code: 'aborted' })));
      xhr.send(file);
    });
  }

  function clearSourceError() {
    setMessage(sourceError, '');
    for (const node of [$('#url'), $('#permission')]) node.removeAttribute('aria-invalid');
  }

  function showSourceError(message, field, hint = '') {
    clearSourceError();
    setMessage(sourceError, message, hint);
    if (field) {
      if (field.matches('input')) field.setAttribute('aria-invalid', 'true');
      field.focus();
    }
  }

  /** Returns { message, field } for the first problem, or null when the form can be sent. */
  function sourceProblem() {
    const urlField = $('#url');
    const raw = urlField.value.trim();
    const t = state.tools;
    if (raw && state.file) return { message: 'There is both a link and a file. Use one of them: clear the link or remove the file.', field: urlField };
    if (!raw && !state.file) return { message: 'Add a video first: paste a link or choose a file.', field: urlField };
    if (raw) {
      let u = null;
      try { u = new URL(raw); } catch { /* handled below */ }
      if (!u || !/^https?:$/.test(u.protocol) || !u.hostname || /\s/.test(raw)) return { message: 'This is not a full web address. Paste the whole link, starting with https://', field: urlField };
      if (raw.length > 2000) return { message: 'This link is too long. Paste the plain link to the video.', field: urlField };
      if (u.username || u.password) return { message: 'This link has a user name or password in it. Remove that part and try again.', field: urlField };
      if (!hostAllowed(u.hostname)) return { message: `This link is from a site the app does not accept. Accepted sites: ${listWords(state.limits.allowedHosts)}.`, field: urlField };
    } else {
      const problem = fileProblem(state.file);
      if (problem) return { message: problem, field: $('#file-drop') };
    }
    if (!$('#permission').checked) return { message: 'Tick the box to confirm that you made this video or have permission to clip it.', field: $('#permission') };
    if (t && !t.ffmpeg) return { message: `ffmpeg is missing, so the app cannot read videos yet. ${INSTALL_HINT}`, field: null };
    if (t && raw && !t.ytdlp) return { message: `yt-dlp is missing, so links cannot be opened. ${INSTALL_HINT} Or choose a video file instead.`, field: null };
    return null;
  }

  function setBusy(kind) {
    state.busy = kind;
    $('#analyze-go').disabled = Boolean(kind);
    $('#moments-lock').disabled = kind === 'render';
    syncFileButton();
    updateMakeButton();
  }

  /** While the file is being copied the button stops the copy; otherwise it removes the file. */
  function syncFileButton() {
    const button = $('#file-remove');
    button.textContent = state.uploadXhr ? 'Stop copying' : 'Remove this file';
    button.disabled = Boolean(state.busy) && !state.uploadXhr;
  }

  /** Forgets the last analysis and its results, ready for a new one. */
  function clearAnalysis() {
    state.watch.analyze = null;
    state.watch.render = null;
    state.jobs.analyze = null;
    state.jobs.render = null;
    state.analysis = null;
    state.cards = [];
    for (const video of $$('#clips video')) { video.pause(); video.removeAttribute('src'); video.load(); }
    $('#clips').replaceChildren();
    $('#moments').replaceChildren();
    hide('progress');
    hide('moments');
    hide('results');
    saveSession();
  }

  async function startAnalysis(event) {
    event.preventDefault();
    if (state.busy) return;
    const problem = sourceProblem();
    if (problem) { showSourceError(problem.message, problem.field); return; }
    clearSourceError();
    clearAnalysis();
    setStep('source');

    const body = { clipLength: Number($('#clip-length').value), count: Number($('#clip-count').value), permission: true };
    const usingFile = Boolean(state.file);
    let jobId = '';
    setBusy('analyze');
    try {
      if (usingFile) {
        if (!state.uploadId) {
          setUploadProgress(0);
          $('#upload').hidden = false;
          $('#upload-status').textContent = 'Copying the file into the app. Keep this page open.';
          const pending = uploadFile(state.file);
          syncFileButton();
          state.uploadId = await pending;
          state.uploadXhr = null;
          syncFileButton();
          setUploadProgress(1);
          $('#upload-status').textContent = 'The file is in the app.';
        }
        body.uploadId = state.uploadId;
      } else {
        body.url = $('#url').value.trim();
      }
      state.sourceUrl = body.url || '';
      const answer = await api('POST', '/api/analyze', body);
      jobId = text(answer.jobId);
      if (!jobId) throw new ApiError('The app gave an answer this page could not read. Reload the page and try again.', { code: 'bad_answer' });
    } catch (error) {
      state.uploadXhr = null;
      if (usingFile) {
        // An upload id that the app refused is not worth keeping: the next try copies the file again.
        state.uploadId = null;
        $('#upload').hidden = true;
      }
      setBusy(null);
      if (error.code === 'aborted') {
        $('#upload-status').textContent = 'Copying stopped. The file was not added.';
        $('#file-remove').focus();
      } else {
        if (usingFile) $('#upload-status').textContent = '';
        // Only a refused link is the link field's fault; anything else leaves the field alone.
        const field = !usingFile && error.status === 400 && error.code !== 'permission_required' ? $('#url') : null;
        showSourceError(error.message, field, error.hint);
        if (!field) $('#analyze-go').focus();
      }
      return;
    }

    state.jobs.analyze = jobId;
    saveSession();
    views.analyze.reset();
    reveal('progress');
    setStep('progress');
    watchJob('analyze', jobId);
  }

  // ------------------------------------------------------------------- jobs

  function watchJob(kind, id) {
    const token = {};
    state.watch[kind] = token;
    const view = views[kind];
    let delay = POLL_MS;
    let misses = 0;
    const live = () => state.watch[kind] === token;
    const again = () => window.setTimeout(tick, delay);

    async function tick() {
      if (!live()) return;
      let job;
      try {
        job = await api('GET', `/api/jobs/${encodeURIComponent(id)}`);
      } catch (error) {
        if (!live()) return;
        if (error.code === 'network' || error.status >= 500 || error.code === 'bad_answer') {
          // The app may be busy or restarting: wait longer each time instead of hammering it.
          misses += 1;
          delay = Math.min(POLL_MAX_MS, Math.round(delay * 1.7));
          if (misses >= 2) view.step('Lost contact with the app. Trying again.', 'Check that the Clip Studio window is still open.');
          again();
          return;
        }
        const gone = error.status === 404;
        jobOver(kind, token);
        view.fail(gone ? 'This job is no longer in the app, most likely because the app was restarted.' : error.message, gone ? 'Start it again.' : error.hint);
        return;
      }
      if (!live()) return;
      misses = 0;
      delay = POLL_MS;
      const jobState = text(job.state);
      view.progress(jobState === 'done' ? 1 : job.progress);
      if (jobState === 'done') {
        jobOver(kind, token, true);
        view.over();
        if (kind === 'analyze') analysisDone(job.result, { restoring: false });
        else renderDone(job.result);
      } else if (jobState === 'error') {
        const error = job.error && typeof job.error === 'object' ? job.error : {};
        jobOver(kind, token);
        view.fail(text(error.message) || 'Something went wrong. Look at the Clip Studio window for details.', text(error.hint));
      } else if (jobState === 'cancelled') {
        jobOver(kind, token);
        view.step(kind === 'analyze' ? 'Cancelled. Nothing was analysed.' : 'Cancelled. No shorts were made.');
        view.over();
      } else {
        view.step(text(job.step) || (jobState === 'queued' ? 'Waiting for an earlier job to finish.' : 'Working.'));
        again();
      }
    }
    tick();
  }

  /** Common clean-up when a job stops for any reason. Finished jobs are remembered so a reload shows them again. */
  function jobOver(kind, token, done = false) {
    if (state.watch[kind] === token) state.watch[kind] = null;
    if (!done) state.jobs[kind] = null;
    setBusy(null);
    if (kind === 'render') $('#restart-row').hidden = false;
    saveSession();
  }

  async function cancelJob(kind) {
    const id = state.jobs[kind];
    const view = views[kind];
    if (!id || !state.watch[kind] || view.cancel.getAttribute('aria-disabled') === 'true') return;
    view.cancelling(true);
    view.step('Cancelling.');
    try {
      await api('POST', `/api/jobs/${encodeURIComponent(id)}/cancel`);
    } catch (error) {
      if (state.watch[kind]) {
        view.cancelling(false);
        view.step('The job could not be cancelled.', error.message);
      }
    }
    // The poll that is already running picks up the new state and finishes the job on the page.
  }

  // ---------------------------------------------------------------- moments

  function cardTimes(card) {
    return {
      start: card.startEdited ? parseTime(card.startText) : card.start,
      end: card.endEdited ? parseTime(card.endText) : card.end,
    };
  }

  /** Checks one moment against the video length and the clip limits. */
  function checkCard(card, { strict = false } = {}) {
    const { minClipSeconds: min, maxClipSeconds: max } = state.limits;
    const duration = state.analysis ? state.analysis.duration : 0;
    const { start, end } = cardTimes(card);
    const out = { ok: false, field: '', message: '', start, end, length: null };
    const quiet = card.custom && !card.touched && !strict;
    const bad = (field, message) => { out.field = field; out.message = quiet ? '' : message; return out; };
    if (start !== null && end !== null && !Number.isNaN(start) && !Number.isNaN(end) && end > start) out.length = end - start;
    if (start === null) return bad('start', 'Enter a start time, like 1:05.');
    if (Number.isNaN(start)) return bad('start', 'Write the start like 1:05 or 1:05.5.');
    if (duration && start >= duration) return bad('start', `The start is past the end of the video (${formatTime(duration)}).`);
    if (end === null) return bad('end', 'Enter an end time, like 1:35.');
    if (Number.isNaN(end)) return bad('end', 'Write the end like 1:35 or 1:35.5.');
    if (end <= start) return bad('end', 'The end must be after the start.');
    if (duration && end > duration + 0.05) return bad('end', `The end is past the end of the video (${formatTime(duration)}).`);
    if (out.length < min - 0.001) return bad('end', `Too short. A clip must be at least ${min} seconds long.`);
    if (out.length > max + 0.001) return bad('end', `Too long. A clip can be at most ${max} seconds long.`);
    out.ok = true;
    return out;
  }

  function watchUrl(start) {
    const base = state.analysis ? state.analysis.videoUrl : '';
    if (!base || start === null || Number.isNaN(start)) return '';
    const u = new URL(base);
    u.searchParams.set('t', `${Math.max(0, Math.floor(start))}s`);
    return u.href;
  }

  function buildCard(card, index) {
    const n = index + 1;
    const id = (part) => `m${card.key}-${part}`;
    const named = (part) => `${id(part)} ${id('h')}`;

    const include = el('input', { type: 'checkbox', checked: card.on, 'aria-labelledby': named('inc') });
    const head = el('div', { class: 'cs-m-head' },
      el('h3', { id: id('h') }, `Moment ${n}`),
      el('label', { class: 'cs-choice' }, include, el('span', { id: id('inc') }, 'Include')),
    );
    if (card.custom) {
      head.append(el('div', { class: 'cs-score' }, el('span', { class: 'k-meta' }, 'You added this moment, so it has no score.')));
    } else {
      head.append(el('div', { class: 'cs-score' },
        el('label', { id: id('sl'), for: id('score') }, 'Score'),
        el('meter', { id: id('score'), min: 0, max: 100, value: card.score, 'aria-labelledby': named('sl'), 'aria-describedby': id('sw') }, `${card.score} out of 100`),
        el('b', {}, String(card.score)),
        el('span', { class: 'k-meta', id: id('sw') }, 'compared with the rest of this video'),
      ));
    }

    const start = el('input', { class: 'k-input', type: 'text', inputMode: 'decimal', autocomplete: 'off', maxLength: 12, value: card.startText, placeholder: '0:00', 'aria-labelledby': named('s'), 'aria-describedby': id('state') });
    const end = el('input', { class: 'k-input', type: 'text', inputMode: 'decimal', autocomplete: 'off', maxLength: 12, value: card.endText, placeholder: '0:30', 'aria-labelledby': named('e'), 'aria-describedby': id('state') });
    for (const field of [start, end]) field.setAttribute('spellcheck', 'false');
    const lengthLine = el('span', { class: 'cs-len' });
    const problem = el('span', { class: 'k-error', role: 'alert' });
    const title = el('input', { class: 'k-input', type: 'text', autocomplete: 'off', maxLength: TITLE_MAX, value: card.title, 'aria-labelledby': named('t'), 'aria-describedby': id('count') });
    const count = el('p', { class: 'cs-count', id: id('count') });
    const left = el('div', { class: 'cs-m-col' },
      el('div', { class: 'cs-times' },
        el('label', {}, el('span', { id: id('s') }, 'Start'), start),
        el('label', {}, el('span', { id: id('e') }, 'End'), end),
      ),
      el('p', { class: 'cs-m-state', id: id('state') }, lengthLine, problem),
      el('label', { class: 'cs-m-title' }, el('span', { id: id('t') }, 'Title, shown at the top of the short'), title),
      count,
    );

    const right = el('div', { class: 'cs-m-col' });
    if (card.reasons.length) right.append(el('ul', { class: 'cs-reasons', 'aria-label': `Why moment ${n} was picked` }, card.reasons.map((reason) => el('li', {}, reason))));
    let words = null;
    let more = null;
    if (card.text) {
      words = el('p', { class: 'cs-text clamp', id: id('text') }, card.text);
      more = el('button', { class: 'cs-linkbtn', type: 'button', hidden: true, 'aria-expanded': 'false', 'aria-controls': id('text') }, 'Show more');
      more.addEventListener('click', () => {
        const open = words.classList.toggle('clamp') === false;
        more.setAttribute('aria-expanded', String(open));
        more.textContent = open ? 'Show less' : 'Show more';
      });
      right.append(words, more);
    } else if (!card.custom) {
      right.append(el('p', { class: 'cs-text' }, 'No words were found for this part.'));
    }
    const links = el('div', { class: 'cs-m-links' });
    let watch = null;
    if (state.analysis.videoUrl) {
      watch = el('a', { id: id('w'), target: '_blank', rel: 'noopener noreferrer', 'aria-labelledby': named('w') }, `Watch this part on ${state.analysis.siteName}`);
      links.append(watch);
    }
    if (card.custom) {
      links.append(el('button', { class: 'k-btn ghost small', type: 'button', 'aria-label': `Remove moment ${n}`, onclick: () => removeCard(card) }, 'Remove'));
    }
    right.append(links);

    const li = el('li', { class: 'cs-moment' }, head, left, right);
    card.dom = { li, start, end, lengthLine, problem, count, watch, words, more };

    include.addEventListener('change', () => { card.on = include.checked; refreshCard(card); cardsChanged(); });
    // While someone is typing a time, a half-typed value is not called an error. The message waits
    // for a pause, or for the field to be left.
    start.addEventListener('input', () => { card.startText = start.value; card.startEdited = true; card.touched = true; refreshCard(card, { typing: true }); cardsChanged(); });
    end.addEventListener('input', () => { card.endText = end.value; card.endEdited = true; card.touched = true; refreshCard(card, { typing: true }); cardsChanged(); });
    for (const field of [start, end]) field.addEventListener('blur', () => { if (card.dom && card.dom.li.isConnected) refreshCard(card); });
    title.addEventListener('input', () => { card.title = title.value; refreshCard(card, { typing: true }); saveSession(); });
    refreshCard(card);
    return li;
  }

  /** Brings one card's messages in line with its values, touching only what changed. */
  function refreshCard(card, { strict = false, typing = false } = {}) {
    const { li, start, end, lengthLine, problem, count, watch } = card.dom;
    const check = checkCard(card, { strict });
    const set = (node, value) => { if (node.textContent !== value) node.textContent = value; };
    li.classList.toggle('off', !card.on);
    set(lengthLine, check.length === null ? 'Length: not set yet' : `Length: ${check.length.toFixed(1)} seconds`);
    const showProblem = () => {
      set(problem, check.message);
      for (const [name, field] of [['start', start], ['end', end]]) {
        if (check.message && check.field === name) field.setAttribute('aria-invalid', 'true');
        else field.removeAttribute('aria-invalid');
      }
    };
    window.clearTimeout(card.messageTimer);
    if (typing && check.message) card.messageTimer = window.setTimeout(showProblem, MESSAGE_DELAY_MS);
    else showProblem();
    set(count, `${card.title.length} of ${TITLE_MAX} characters`);
    if (watch) {
      const href = watchUrl(check.start);
      if (href) watch.href = href;
      else watch.removeAttribute('href');
    }
    return check;
  }

  function renderCards() {
    $('#moments').replaceChildren(...state.cards.map(buildCard));
    $('#moments-help').hidden = !state.cards.length;
    measureTranscripts();
    cardsChanged();
  }

  /** "Show more" is offered only where the text really is longer than three lines. */
  function measureTranscripts() {
    for (const card of state.cards) {
      const { words, more } = card.dom || {};
      if (words && more && words.classList.contains('clamp')) more.hidden = words.scrollHeight <= words.clientHeight + 1;
    }
  }

  function renderTimeline() {
    const duration = state.analysis ? state.analysis.duration : 0;
    const marks = [];
    state.cards.forEach((card, index) => {
      const { start, end } = cardTimes(card);
      if (!duration || start === null || end === null || Number.isNaN(start) || Number.isNaN(end) || end <= start || start >= duration) return;
      const mark = el('span', { class: card.on ? '' : 'off' }, String(index + 1));
      mark.style.setProperty('left', `min(${clamp((start / duration) * 100, 0, 100).toFixed(3)}%, calc(100% - 1.15rem))`);
      mark.style.setProperty('width', `${clamp(((Math.min(end, duration) - start) / duration) * 100, 0, 100).toFixed(3)}%`);
      marks.push(mark);
    });
    $('#timeline').replaceChildren(...marks);
  }

  function updateMakeButton() {
    const n = state.cards.filter((c) => c.on).length;
    const button = $('#render-go');
    const label = `Make ${plural(n, 'short')}`;
    if (button.textContent !== label) button.textContent = label;
    button.disabled = n === 0 || state.busy === 'render';
  }

  function cardsChanged() {
    renderTimeline();
    updateMakeButton();
    setMessage($('#render-error'), '');
    saveSession();
  }

  function addCard() {
    const card = { key: state.nextKey++, id: `own-${Date.now().toString(36)}-${state.nextKey}`, custom: true, on: true, start: null, end: null, startText: '', endText: '', startEdited: true, endEdited: true, touched: false, title: '', score: 0, reasons: [], text: '' };
    state.cards.push(card);
    renderCards();
    card.dom.start.focus();
  }

  function removeCard(card) {
    state.cards = state.cards.filter((c) => c !== card);
    renderCards();
    $('#add-moment').focus();
  }

  function syncTextOptions() {
    const filterOk = !state.tools || state.tools.captionsFilter;
    const hasCaptions = state.analysis ? state.analysis.hasCaptions : true;
    const captions = $('#opt-captions');
    const title = $('#opt-title');
    const captionsHelp = $('#captions-help');
    const titleHelp = $('#title-help');
    const noText = `This copy of ffmpeg cannot draw text on video. ${INSTALL_HINT.replace('it can', 'the right one can')}`;
    captions.disabled = !filterOk || !hasCaptions;
    captions.checked = !captions.disabled && state.options.captions;
    captionsHelp.textContent = !filterOk ? noText : !hasCaptions ? 'No captions were found for this video, so there is nothing to burn in.' : '';
    captionsHelp.hidden = !captionsHelp.textContent;
    title.disabled = !filterOk;
    title.checked = !title.disabled && state.options.showTitle;
    titleHelp.textContent = filterOk ? '' : noText;
    titleHelp.hidden = filterOk;
  }

  function syncLayoutOptions() {
    const o = state.options;
    for (const radio of $$('input[name="layout"]')) radio.checked = radio.value === o.layout;
    for (const radio of $$('input[name="position"]')) radio.checked = radio.value === o.position;
    $('#opt-zoom').hidden = o.layout !== 'blur';
    $('#opt-position').hidden = o.layout !== 'fill';
    const zoom = $('#zoom');
    zoom.value = String(o.zoom);
    const words = `${o.zoom.toFixed(2)} times`;
    $('#zoom-out').textContent = words;
    zoom.setAttribute('aria-valuetext', words);
  }

  function analysisDone(result, { restoring, saved = null }) {
    const r = result && typeof result === 'object' ? result : {};
    const video = r.video && typeof r.video === 'object' ? r.video : {};
    const signals = r.signals && typeof r.signals === 'object' ? r.signals : {};
    const captionLabel = text(r.captionLabel).trim();
    const videoUrl = safeVideoUrl(video.url);
    const fromLink = typeof video.url === 'string' && video.url !== '';
    state.analysis = {
      id: text(r.analysisId),
      duration: Math.max(0, num(video.duration)),
      videoUrl,
      siteName: /(^|\.)youtube\.com$|^youtu\.be$/.test(videoUrl ? new URL(videoUrl).hostname : '') ? 'YouTube' : 'the video site',
      hasCaptions: Boolean(captionLabel) || signals.speech === true,
    };
    const a = state.analysis;

    // The video
    $('#video-title').textContent = text(video.title) || 'Untitled video';
    const about = [];
    if (text(video.channel)) about.push(`From ${text(video.channel)}.`);
    if (a.duration) about.push(`${formatTime(a.duration, false)} long.`);
    if (video.wasLive === true) about.push('Finished stream.');
    $('#video-meta').textContent = about.join(' ');
    const slot = $('#video-thumb');
    const thumb = safeThumbnail(video.thumbnail);
    slot.replaceChildren();
    slot.hidden = true;
    if (thumb) {
      const img = el('img', { alt: '', referrerPolicy: 'no-referrer', decoding: 'async' });
      img.addEventListener('load', () => { slot.hidden = false; });
      img.addEventListener('error', () => { slot.hidden = true; });
      img.src = thumb;
      slot.append(img);
    }

    // What was measured, in plain words
    const signal = (on, words) => el('li', { class: on ? 'on' : '' }, el('span', {}, words));
    $('#signals').replaceChildren(
      signal(signals.replays === true, `Replay data from YouTube: ${signals.replays === true ? 'found' : fromLink ? 'not available for this video' : 'not available for a file from this computer'}`),
      signal(signals.audio === true, `Audio loudness: ${signals.audio === true ? 'measured' : 'could not be measured'}`),
      signal(a.hasCaptions, `Captions: ${a.hasCaptions ? captionLabel || 'found' : 'none found, so clips will have no captions'}`),
    );

    // Moments, strongest first
    const moments = Array.isArray(r.moments) ? r.moments.filter((m) => m && typeof m === 'object') : [];
    let cards = moments.map((m, i) => {
      const start = Math.max(0, num(m.start));
      const end = Math.max(0, num(m.end));
      return {
        key: state.nextKey++, id: text(m.id) || `moment-${i}`, custom: false, on: true,
        start, end, startText: formatTime(start), endText: formatTime(end), startEdited: false, endEdited: false, touched: true,
        title: text(m.title).slice(0, TITLE_MAX), score: clamp(Math.round(num(m.score, 1)), 1, 100),
        reasons: Array.isArray(m.reasons) ? m.reasons.map(text).filter(Boolean) : [], text: text(m.text),
      };
    }).sort((x, y) => y.score - x.score);
    if (saved && saved.analysisId === a.id && Array.isArray(saved.cards)) cards = restoreCards(cards, saved.cards);
    state.cards = cards;

    const note = text(r.note) || (cards.length ? '' : 'No strong moments were found in this video. You can still add a moment yourself.');
    $('#analysis-note').textContent = note;
    $('#analysis-note').hidden = !note;
    $('#timeline-end').textContent = formatTime(a.duration, false);
    setMessage($('#render-error'), '');

    views.analyze.step(`Finished. ${cards.length ? `Found ${plural(cards.length, 'moment')}.` : 'No strong moments were found.'}`);
    syncTextOptions();
    syncLayoutOptions();
    reveal('moments', { focus: !restoring });
    renderCards();
    setStep('moments');
  }

  /** Puts back the edits made before a reload. Unknown or damaged entries are ignored. */
  function restoreCards(fresh, savedCards) {
    const byId = new Map(fresh.map((c) => [c.id, c]));
    const out = [];
    for (const s of savedCards) {
      if (!s || typeof s !== 'object') continue;
      const base = s.custom === true ? { key: state.nextKey++, id: text(s.id), custom: true, start: null, end: null, startEdited: true, endEdited: true, score: 0, reasons: [], text: '' } : byId.get(s.id);
      if (!base) continue;
      byId.delete(s.id);
      const card = { ...base, on: s.on !== false, touched: true, title: text(s.title).slice(0, TITLE_MAX) };
      if (base.custom || s.startEdited === true) { card.startText = text(s.startText).slice(0, 12); card.startEdited = true; }
      if (base.custom || s.endEdited === true) { card.endText = text(s.endText).slice(0, 12); card.endEdited = true; }
      if (card.custom && !card.startText && !card.endText) card.touched = false;
      out.push(card);
    }
    return out.concat([...byId.values()]);
  }

  // ----------------------------------------------------------------- render

  async function startRender() {
    if (state.busy || !state.analysis) return;
    const errorLine = $('#render-error');
    const chosen = state.cards.filter((c) => c.on);
    const max = state.limits.maxClips;
    if (!chosen.length) return;
    if (chosen.length > max) {
      setMessage(errorLine, `You can make at most ${plural(max, 'short')} at a time. Untick ${chosen.length - max} of the moments.`);
      return;
    }
    const clips = [];
    for (const card of chosen) {
      card.touched = true;
      const check = refreshCard(card, { strict: true });
      if (!check.ok) {
        const n = state.cards.indexOf(card) + 1;
        setMessage(errorLine, `Moment ${n} cannot be made yet. ${check.message}`, 'Fix its times, or untick it.');
        card.dom[check.field === 'start' ? 'start' : 'end'].focus();
        return;
      }
      clips.push({ start: Math.round(check.start * 100) / 100, end: Math.round(check.end * 100) / 100, title: card.title.trim() });
    }
    setMessage(errorLine, '');
    const o = state.options;
    const body = {
      analysisId: state.analysis.id,
      clips,
      options: { layout: o.layout, zoom: o.zoom, position: o.position, captions: $('#opt-captions').checked, showTitle: $('#opt-title').checked },
    };
    setBusy('render');
    let jobId = '';
    try {
      const answer = await api('POST', '/api/render', body);
      jobId = text(answer.jobId);
      if (!jobId) throw new ApiError('The app gave an answer this page could not read. Reload the page and try again.', { code: 'bad_answer' });
    } catch (error) {
      setBusy(null);
      setMessage(errorLine, error.message, error.hint);
      $('#render-go').focus();
      return;
    }
    state.jobs.render = jobId;
    saveSession();
    showRenderProgress();
    reveal('results');
    watchJob('render', jobId);
  }

  function showRenderProgress() {
    for (const video of $$('#clips video')) video.pause();
    $('#clips').replaceChildren();
    $('#results').hidden = true;
    $('#restart-row').hidden = true;
    views.render.reset();
    setStep('results');
  }

  function buildClip(clip, position) {
    const n = position + 1;
    const hid = `clip-${n}-h`;
    const name = text(clip.name);
    const url = safeOutputUrl(clip.url);
    const li = el('li', { class: 'cs-clip' });
    const heading = el('h3', { id: hid }, `Short ${n}`);

    if (clip.ok !== true || !url) {
      const reason = clip.ok === true ? 'The app gave a file address this page will not open. Look for the file in the output folder.' : text(clip.error) || 'The app did not say why. Look at the Clip Studio window for details.';
      li.append(heading, el('div', { class: 'cs-failed' }, el('strong', {}, 'This short could not be made.'), el('span', {}, reason)));
      return li;
    }

    const video = el('video', { controls: true, preload: 'metadata', 'aria-labelledby': hid });
    video.setAttribute('playsinline', '');
    const meta = el('p', { class: 'k-meta' }, `${name ? `${name}. ` : ''}${formatTime(num(clip.seconds), false)} long, ${K.bytes(Math.max(0, Math.round(num(clip.bytes))))}.`);
    video.addEventListener('error', () => { meta.textContent = `${name ? `${name}. ` : ''}The preview could not be loaded here. Open the file from the output folder instead.`; });
    video.src = url;

    const captionId = `clip-${n}-caption`;
    const caption = el('textarea', { class: 'k-textarea', id: captionId, readOnly: true, rows: 3, value: text(clip.caption) });
    li.append(
      heading,
      el('div', { class: 'cs-player' }, video),
      meta,
      el('label', { class: 'k-label', for: captionId }, 'Suggested caption'),
      caption,
      el('div', { class: 'k-row' },
        el('a', { class: 'k-btn', href: url, download: name || `short-${n}.mp4`, 'aria-label': `Download short ${n}` }, 'Download'),
        el('button', { class: 'k-btn ghost', type: 'button', 'aria-label': `Copy caption of short ${n}`, onclick: () => K.copy(caption.value) }, 'Copy caption'),
      ),
    );
    return li;
  }

  function renderDone(result) {
    const r = result && typeof result === 'object' ? result : {};
    const clips = (Array.isArray(r.clips) ? r.clips.filter((c) => c && typeof c === 'object') : []).sort((x, y) => num(x.index) - num(y.index));
    const made = clips.filter((c) => c.ok === true && safeOutputUrl(c.url)).length;
    const failed = clips.length - made;
    $('#clips').replaceChildren(...clips.map(buildClip));
    const folder = text(r.folder);
    $('#folder').textContent = folder;
    $('#folder-row').hidden = !folder || !made;
    $('#results').hidden = false;
    $('#restart-row').hidden = false;
    let summary;
    if (!clips.length) summary = 'Finished, but the app returned no shorts. Look at the Clip Studio window for details.';
    else if (!failed) summary = `Finished. ${plural(made, 'short')} ${made === 1 ? 'is' : 'are'} ready below.`;
    else if (!made) summary = `Finished, but ${failed === 1 ? 'the short' : `none of the ${failed} shorts`} could be made. The reason is on ${failed === 1 ? 'its card' : 'each card'}.`;
    else summary = `Finished. ${made} of ${plural(clips.length, 'short')} ${made === 1 ? 'is' : 'are'} ready. ${failed} could not be made, and the reason is on ${failed === 1 ? 'its card' : 'those cards'}.`;
    views.render.step(summary);
  }

  // ---------------------------------------------------------------- restart

  function startAgain() {
    stopUpload();
    clearAnalysis();
    removeFile();
    views.analyze.reset();
    views.render.reset();
    $('#restart-row').hidden = true;
    $('#results').hidden = true;
    $('#url').value = '';
    $('#permission').checked = false;
    clearSourceError();
    setBusy(null);
    setStep('source');
    window.scrollTo({ top: 0, behavior: reducedMotion.matches ? 'auto' : 'smooth' });
    $('#url').focus({ preventScroll: true });
  }

  // ----------------------------------------------------------------- resume

  /** After a reload, picks up the job that was running, or shows the finished analysis again. */
  async function resume() {
    const saved = loadSession();
    const analyzeJob = text(saved.analyzeJob);
    const renderJob = text(saved.renderJob);
    if (!analyzeJob) { saveSession(); return; }
    state.jobs.analyze = analyzeJob;
    state.jobs.render = renderJob || null;
    state.sourceUrl = text(saved.url).slice(0, 2000);
    if (state.sourceUrl && !$('#url').value) $('#url').value = state.sourceUrl;
    views.analyze.reset();
    views.analyze.step('Picking up where you left off.');
    reveal('progress', { focus: false });
    setStep('progress');

    let job;
    try {
      job = await api('GET', `/api/jobs/${encodeURIComponent(analyzeJob)}`);
    } catch (error) {
      if (error.status === 404) {
        state.jobs.analyze = null;
        state.jobs.render = null;
        saveSession();
        views.analyze.fail('The earlier job is no longer in the app, most likely because the app was restarted.', 'Start again with the video.');
        return;
      }
      setBusy('analyze');
      watchJob('analyze', analyzeJob); // the watcher keeps trying and reports problems
      return;
    }
    if (text(job.state) !== 'done') {
      state.jobs.render = null;
      setBusy('analyze');
      watchJob('analyze', analyzeJob);
      return;
    }
    views.analyze.progress(1);
    views.analyze.over();
    analysisDone(job.result, { restoring: true, saved });
    if (renderJob) {
      showRenderProgress();
      views.render.step('Picking up where you left off.');
      reveal('results', { focus: false });
      setBusy('render');
      watchJob('render', renderJob);
    }
  }

  // ------------------------------------------------------------------ wiring

  function init() {
    const o = state.options;
    $('#clip-length').value = String(o.clipLength);
    $('#clip-count').value = String(o.count);
    syncSourceOutputs();
    syncLayoutOptions();
    syncTextOptions();
    setStep('source');

    $('#source-form').addEventListener('submit', startAnalysis);
    $('#clip-length').addEventListener('input', (e) => { o.clipLength = Number(e.target.value); syncSourceOutputs(); saveOptions(); });
    $('#clip-count').addEventListener('input', (e) => { o.count = Number(e.target.value); syncSourceOutputs(); saveOptions(); });
    $('#url').addEventListener('input', () => { if (sourceError.textContent) clearSourceError(); });
    $('#permission').addEventListener('change', () => { if (sourceError.textContent) clearSourceError(); });
    K.drop($('#file-drop'), chooseFile, { accept: `${VIDEO_EXTENSIONS.join(',')},video/*`, multiple: false });
    $('#file-remove').addEventListener('click', () => {
      if (state.uploadXhr) { stopUpload(); return; }
      if (state.busy) return;
      removeFile();
      $('#file-drop').focus();
    });

    $('#an-cancel').addEventListener('click', () => cancelJob('analyze'));
    $('#rn-cancel').addEventListener('click', () => cancelJob('render'));

    $('#add-moment').addEventListener('click', addCard);
    for (const radio of $$('input[name="layout"]')) radio.addEventListener('change', () => { if (radio.checked) { o.layout = radio.value; syncLayoutOptions(); saveOptions(); } });
    for (const radio of $$('input[name="position"]')) radio.addEventListener('change', () => { if (radio.checked) { o.position = radio.value; saveOptions(); } });
    $('#zoom').addEventListener('input', (e) => { o.zoom = clamp(Math.round(Number(e.target.value) * 100) / 100, 1, 2); syncLayoutOptions(); saveOptions(); });
    $('#opt-captions').addEventListener('change', (e) => { o.captions = e.target.checked; saveOptions(); });
    $('#opt-title').addEventListener('change', (e) => { o.showTitle = e.target.checked; saveOptions(); });
    $('#render-go').addEventListener('click', startRender);

    $('#folder-copy').addEventListener('click', () => K.copy($('#folder').textContent));
    $('#restart').addEventListener('click', startAgain);

    let resizeTimer = 0;
    window.addEventListener('resize', () => { window.clearTimeout(resizeTimer); resizeTimer = window.setTimeout(measureTranscripts, 150); });

    loadStatus().then(resume);
  }

  init();
})();
