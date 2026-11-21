'use strict';
/* Small helpers shared by the tools. No dependencies. */
const K = {
  $: (sel, root = document) => root.querySelector(sel),
  $$: (sel, root = document) => [...root.querySelectorAll(sel)],

  /** Builds an element: K.el('button', { class: 'k-btn', onclick: fn }, 'Save') */
  el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === false || value == null) continue;
      if (key === 'class') node.className = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else if (key in node && key !== 'list') node[key] = value;
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) if (child != null && child !== false) node.append(child);
    return node;
  },

  bytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
    return `${(n / 1048576).toFixed(n < 10485760 ? 2 : 1)} MB`;
  },

  download(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = K.el('a', { href: url, download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  },

  toast(message, bad = false) {
    K.$$('.k-toast').forEach((t) => t.remove());
    const t = K.el('div', { class: `k-toast${bad ? ' bad' : ''}`, role: bad ? 'alert' : 'status' }, message);
    document.body.append(t);
    setTimeout(() => t.remove(), bad ? 6000 : 2800);
  },

  async copy(text) {
    try { await navigator.clipboard.writeText(text); K.toast('Copied.'); }
    catch { K.toast('Copying is blocked here. Select the text and copy it yourself.', true); }
  },

  /** Makes a drop area that also opens a file picker. onFiles gets an array of File. */
  drop(zone, onFiles, { accept = '', multiple = true } = {}) {
    const input = K.el('input', { type: 'file', accept, multiple, hidden: true });
    zone.append(input);
    zone.tabIndex = 0;
    zone.setAttribute('role', 'button');
    const open = () => input.click();
    zone.addEventListener('click', (e) => { if (e.target !== input) open(); });
    zone.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    input.addEventListener('change', () => { if (input.files.length) onFiles([...input.files]); input.value = ''; });
    for (const type of ['dragenter', 'dragover']) zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add('over'); });
    for (const type of ['dragleave', 'drop']) zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.remove('over'); });
    zone.addEventListener('drop', (e) => { const files = [...e.dataTransfer.files]; if (files.length) onFiles(multiple ? files : files.slice(0, 1)); });
    return input;
  },

  /** Wires .k-tab buttons to panels: <button class="k-tab" data-tab="merge"> and <section data-panel="merge"> */
  tabs(root = document, onChange) {
    const tabs = K.$$('.k-tab', root);
    const show = (name) => {
      tabs.forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
      K.$$('[data-panel]', root).forEach((p) => { p.hidden = p.dataset.panel !== name; });
      if (onChange) onChange(name);
    };
    tabs.forEach((t) => { if (!t.hasAttribute('role')) t.setAttribute('role', 'tab'); });
    K.$$('[data-panel]', root).forEach((p) => { if (!p.hasAttribute('role')) p.setAttribute('role', 'tabpanel'); });
    tabs.forEach((t) => t.addEventListener('click', () => show(t.dataset.tab)));
    show((tabs.find((t) => t.getAttribute('aria-selected') === 'true') || tabs[0]).dataset.tab);
    return show;
  },

  loaded: new Map(),
  /** Loads an external script once. Rejects with a readable message. */
  script(url) {
    if (!K.loaded.has(url)) {
      K.loaded.set(url, new Promise((resolve, reject) => {
        const s = K.el('script', { src: url, async: true });
        s.onload = resolve;
        s.onerror = () => { K.loaded.delete(url); reject(new Error('A needed library could not be downloaded. Check your internet connection and try again.')); };
        document.head.append(s);
      }));
    }
    return K.loaded.get(url);
  },

  save(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
  load(key, fallback) { try { const raw = localStorage.getItem(key); return raw === null ? fallback : JSON.parse(raw); } catch { return fallback; } },

  /** Indian number format: 1234567.5 -> 12,34,567.50 */
  inr(n, decimals = 2) { return Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals }); },
};

// A file dropped beside a drop area would otherwise make the browser open it and leave the page.
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());

const CDN = {
  pdfLib: 'https://cdn.jsdelivr.net/npm/pdf-lib@1.17.1/dist/pdf-lib.min.js',
  sqlJs: 'https://cdn.jsdelivr.net/npm/sql.js@1.17.0/dist/',
  qr: 'https://cdn.jsdelivr.net/npm/qrcode-generator@2.0.4/dist/qrcode.js',
  jsQR: 'https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js',
};
