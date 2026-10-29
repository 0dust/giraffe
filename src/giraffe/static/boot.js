// Apply the theme before styles load; only a manual choice is persisted.
(() => {
  const root = document.documentElement;
  const storedTheme = () => {
    try { return localStorage.getItem('giraffe-theme'); } catch { return null; }
  };
  const isTheme = value => value === 'light' || value === 'dark';
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  const choice = storedTheme();
  root.dataset.theme = isTheme(choice) ? choice : media?.matches ? 'dark' : 'light';
  const followSystem = event => {
    if (root.dataset.themePreference === 'manual' || isTheme(storedTheme())) return;
    const theme = event.matches ? 'dark' : 'light';
    root.dataset.theme = theme;
    const toggle = document.querySelector?.('[data-action="theme"]');
    if (toggle) {
      toggle.setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`);
      toggle.innerHTML = theme === 'dark'
        ? '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3.5"/><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4"/></svg>'
        : '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M15.5 12.2A6.5 6.5 0 0 1 7.8 4.5a6.5 6.5 0 1 0 7.7 7.7Z"/></svg>';
    }
  };
  if (media?.addEventListener) media.addEventListener('change', followSystem);
  else media?.addListener?.(followSystem);
  try {
    if (localStorage.getItem('giraffe-rail') === 'collapsed') root.classList.add('rail-collapsed');
  } catch {}
})();
