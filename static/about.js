/* About window: version and credits (Credits.html, also the macOS About panel). */
(() => {
  let started = false;
  let dialog = null;
  function close() { if (dialog) { dialog.close(); dialog.remove(); dialog = null; } }
  async function open() {
    if (dialog) return;
    const response = await fetch('/api/about');
    if (!response.ok) throw new Error(`about ${response.status}`);
    const about = await response.json();
    dialog = document.createElement('dialog'); dialog.className = 'message-dialog about-dialog';
    const title = document.createElement('h2'); title.textContent = 'ZuckerMixer';
    const version = document.createElement('p'); version.className = 'about-version';
    version.textContent = `Version ${about.app_version} · built ${about.build_timestamp} · ${String(about.source_revision).slice(0, 7)}`;
    const credits = document.createElement('div'); credits.className = 'about-credits';
    credits.innerHTML = about.credits_html; // bundled with the app, not user input
    for (const link of credits.querySelectorAll('a')) { link.target = '_blank'; link.rel = 'noopener'; }
    const done = document.createElement('button'); done.type = 'button'; done.textContent = 'Close'; done.onclick = close;
    dialog.append(title, version, credits, done); document.body.append(dialog);
    makeDialogDismissible(dialog, close); dialog.showModal(); done.focus();
  }
  window.zuckerOpenAbout = () => open().catch(() => {});
  function start() {
    const button = document.querySelector('#openAbout');
    if (started || !button) return;
    started = true;
    button.onclick = window.zuckerOpenAbout;
  }
  document.addEventListener('DOMContentLoaded', start);
  start();
})();
