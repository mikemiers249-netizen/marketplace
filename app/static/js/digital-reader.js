(() => {
  const config = JSON.parse(document.getElementById('reader-config').textContent);
  const field = document.getElementById('reading-field');
  const text = document.getElementById('reading-text');
  const blocks = Array.from(text.querySelectorAll('[data-source]'));
  const status = document.getElementById('save-status');
  const percent = document.getElementById('reader-percent');
  if (!blocks.length) return;
  const groups = new Map();
  blocks.forEach(block => {
    const source = Number(block.dataset.source);
    if (!groups.has(source)) groups.set(source, []);
    groups.get(source).push(block);
  });
  let restoring = true, timer, busy = false, queued = null;
  let bookmark = {chapter: config.chapter, position: config.position};
  try {
    const local = JSON.parse(localStorage.getItem(config.storageKey));
    if (local && local.savedAt > Date.parse(config.updatedAt) && groups.has(local.chapter)
        && Number.isFinite(local.position) && local.position >= 0 && local.position <= 1) bookmark = local;
  } catch (_) {}
  function extent(block) {
    const next = block.nextElementSibling;
    return next ? next.offsetTop - block.offsetTop : block.offsetHeight;
  }
  function restore(mark) {
    if (mark.chapter === config.count - 1 && mark.position === 1) { field.scrollTop = field.scrollHeight; return; }
    const group = groups.get(mark.chapter) || Array.from(groups.values()).at(-1);
    const exact = Math.min(.999999, Math.max(0, mark.position)) * group.length;
    const block = group[Math.floor(exact)];
    const y = block.getBoundingClientRect().top - field.getBoundingClientRect().top + field.scrollTop - field.clientTop;
    field.scrollTop = y + (exact % 1) * extent(block);
  }
  function current() {
    const top = field.getBoundingClientRect().top + field.clientTop;
    let low = 0, high = blocks.length - 1;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (blocks[mid].getBoundingClientRect().top <= top + 1) low = mid; else high = mid - 1;
    }
    const block = blocks[low], chapter = Number(block.dataset.source);
    const group = groups.get(chapter);
    const fraction = Math.max(0, Math.min(.999999, (top - block.getBoundingClientRect().top) / Math.max(1, extent(block))));
    const end = field.scrollHeight <= field.clientHeight + 3 || (field.scrollTop > 0 && field.scrollTop + field.clientHeight >= field.scrollHeight - 3);
    return end ? {chapter: config.count - 1, position: 1} : {
      chapter, position: Math.max(.000001, (Number(block.dataset.block) + fraction) / group.length)
    };
  }
  function remember() {
    bookmark = current();
    const progress = bookmark.position === 1 && bookmark.chapter === config.count - 1 ? 100 :
      Math.min(99, Math.floor((bookmark.chapter + bookmark.position) / config.count * 100));
    percent.textContent = progress + '%';
    if (config.storageKey) {
      try { localStorage.setItem(config.storageKey, JSON.stringify({...bookmark, savedAt: Date.now()})); } catch (_) {}
    }
    return {...bookmark};
  }
  async function send() {
    if (busy || !queued || !config.saveUrl) return;
    const payload = queued;
    queued = null;
    busy = true;
    status.textContent = 'Сохраняем место чтения…';
    try {
      const response = await fetch(config.saveUrl, {method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content},
        body: JSON.stringify(payload)});
      if (!response.ok) throw new Error('save');
      status.textContent = 'Место чтения сохранено';
    } catch (_) {
      if (!queued) queued = payload;
      status.textContent = 'Нет связи. Место сохранено на этом устройстве';
    } finally { busy = false; }
  }
  function flush() {
    if (restoring || !config.saveUrl) return;
    clearTimeout(timer);
    queued = remember();
    void send().then(() => { if (queued && navigator.onLine) timer = setTimeout(send, 1500); });
  }
  let autoSpeed = 0, autoFrame = null, autoTime = null, autoSaved = 0, autoRemainder = 0;
  const autoButtons = Array.from(document.querySelectorAll('[data-reading-auto]'));
  const autoOutput = document.querySelector('[data-reading-auto-value]');
  function setAutoSpeed(value) {
    autoSpeed = Math.max(0, value);
    if (autoOutput) autoOutput.textContent = autoSpeed + '%';
    autoButtons.forEach(button => { button.disabled = Number(button.dataset.readingAuto) < 0 && autoSpeed === 0; });
    if (!autoSpeed) {
      cancelAnimationFrame(autoFrame);
      autoFrame = null; autoTime = null; autoRemainder = 0;
      flush();
    } else if (autoFrame === null) autoFrame = requestAnimationFrame(autoScroll);
  }
  function autoScroll(now) {
    autoFrame = null;
    if (!autoSpeed) return;
    if (autoTime !== null && !document.hidden && !restoring) {
      // 100% = 40 px/sec; retain subpixels so low speeds work in every browser.
      autoRemainder += Math.min(100, now - autoTime) * .04 * autoSpeed / 100;
      const pixels = Math.floor(autoRemainder);
      if (pixels) { field.scrollTop += pixels; autoRemainder -= pixels; }
      if (field.scrollTop + field.clientHeight >= field.scrollHeight - 1) { setAutoSpeed(0); return; }
      if (now - autoSaved > 3000) { flush(); autoSaved = now; }
    }
    autoTime = document.hidden ? null : now;
    autoFrame = requestAnimationFrame(autoScroll);
  }
  autoButtons.forEach(button => button.addEventListener('click', () => setAutoSpeed(autoSpeed + Number(button.dataset.readingAuto) * 10)));
  window.addEventListener('pagehide', () => { cancelAnimationFrame(autoFrame); autoFrame = null; autoTime = null; autoSpeed = 0; });
  window.addEventListener('pageshow', event => { if (event.persisted) setAutoSpeed(0); });
  async function initialize() {
    if (document.fonts) await document.fonts.ready;
    restore(bookmark);
    restoring = false;
    remember();
    if (config.saveUrl) { queued = {...bookmark}; void send(); }
  }
  field.addEventListener('scroll', () => {
    if (restoring) return;
    remember();
    clearTimeout(timer);
    timer = setTimeout(flush, 450);
  }, {passive: true});
  window.addEventListener('resize', () => { restoring = true; restore(bookmark); restoring = false; });
  let appearanceBookmark = null;
  document.addEventListener('reader:before-appearance-change', () => {
    if (restoring) return;
    appearanceBookmark = current();
    restoring = true;
  });
  document.addEventListener('reader:after-appearance-change', () => {
    if (!appearanceBookmark) return;
    restore(appearanceBookmark);
    appearanceBookmark = null;
    restoring = false;
    flush();
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) flush(); });
  window.addEventListener('pagehide', flush);
  window.addEventListener('online', flush);
  setInterval(() => { if (queued) void send(); }, 5000);
  text.addEventListener('contextmenu', event => event.preventDefault());
  document.addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && ['s', 'p'].includes(event.key.toLowerCase())) event.preventDefault(); });
  void initialize();
})();
