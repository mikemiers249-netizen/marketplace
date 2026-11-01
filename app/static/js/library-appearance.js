(() => {
  const key = 'wimli-library-appearance';
  const root = document.documentElement;
  const minimum = 15, maximum = 35;
  function load() {
    try {
      const value = JSON.parse(localStorage.getItem(key)) || {};
      const font = Number(value.font);
      return {theme: value.theme === 'night' ? 'night' : 'day',
        font: Number.isFinite(font) && font >= minimum && font <= maximum ? Math.round(font) : 21};
    } catch (_) { return {theme: 'day', font: 21}; }
  }
  let settings = load();
  function apply(persist = false) {
    document.dispatchEvent(new Event('reader:before-appearance-change'));
    root.dataset.libraryTheme = settings.theme;
    root.style.setProperty('--reader-font-size', settings.font + 'px');
    document.querySelectorAll('[data-reading-theme]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.readingTheme === settings.theme));
    });
    document.querySelectorAll('[data-reading-font-value]').forEach(output => { output.textContent = settings.font + ' пкс'; });
    document.querySelectorAll('[data-reading-font]').forEach(button => {
      button.disabled = Number(button.dataset.readingFont) < 0 ? settings.font <= minimum : settings.font >= maximum;
    });
    if (persist) { try { localStorage.setItem(key, JSON.stringify(settings)); } catch (_) {} }
    document.dispatchEvent(new Event('reader:after-appearance-change'));
  }
  apply();
  function bind() {
    apply();
    document.querySelectorAll('[data-reading-theme]').forEach(button => {
      button.addEventListener('click', () => { settings.theme = button.dataset.readingTheme; apply(true); });
    });
    document.querySelectorAll('[data-reading-font]').forEach(button => {
      button.addEventListener('click', () => {
        settings.font = Math.max(minimum, Math.min(maximum, settings.font + Number(button.dataset.readingFont) * 2));
        apply(true);
      });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind, {once: true}); else bind();
  window.addEventListener('storage', event => { if (event.key === key || event.key === null) { settings = load(); apply(); } });
})();
