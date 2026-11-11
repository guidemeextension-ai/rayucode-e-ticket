(() => {
  const root = document.documentElement;
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let override = null;
  try { override = localStorage.getItem('eticket-theme'); } catch (_) {}
  if (!['light', 'dark'].includes(override)) override = null;
  function apply(theme) {
    root.dataset.theme = theme;
    document.querySelectorAll('.theme-toggle').forEach(button => {
      const dark = theme === 'dark';
      button.textContent = dark ? 'Light theme' : 'Dark theme';
      button.setAttribute('aria-label', `Switch to ${dark ? 'light' : 'dark'} theme`);
      button.setAttribute('aria-pressed', String(dark));
    });
  }
  apply(override || (system.matches ? 'dark' : 'light'));
  system.addEventListener('change', () => {
    if (!override) apply(system.matches ? 'dark' : 'light');
  });
  document.addEventListener('DOMContentLoaded', () => {
    apply(root.dataset.theme);
    document.querySelectorAll('.theme-toggle').forEach(button => {
      button.addEventListener('click', () => {
        override = root.dataset.theme === 'dark' ? 'light' : 'dark';
        try { localStorage.setItem('eticket-theme', override); } catch (_) {}
        apply(override);
      });
    });
  });
})();
