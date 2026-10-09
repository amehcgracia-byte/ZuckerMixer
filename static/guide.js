/* "Read me first" guide: shown once per installed version, reopened from the footer. */
(() => {
  let started = false;
  let dialog = null;
  const markSeen = () => fetch('/api/guide/seen', { method: 'POST' }).catch(() => {});
  function close() { if (dialog) { dialog.close(); dialog.remove(); dialog = null; markSeen(); } }
  function show(text) {
    if (dialog || !text) return;
    dialog = document.createElement('dialog'); dialog.className = 'message-dialog guide-dialog';
    const title = document.createElement('h2'); title.textContent = 'Read me first';
    const body = document.createElement('pre'); body.textContent = text;
    const done = document.createElement('button'); done.type = 'button'; done.textContent = 'Got it'; done.onclick = close;
    dialog.append(title, body, done); document.body.append(dialog);
    makeDialogDismissible(dialog, close); dialog.showModal(); done.focus();
  }
  // Wait for any open dialog (an update offer, a confirmation) before opening.
  function showWhenFree(text) {
    if (document.querySelector('dialog[open]')) { setTimeout(() => showWhenFree(text), 1000); return; }
    show(text);
  }
  async function load() {
    const response = await fetch('/api/guide');
    if (!response.ok) throw new Error(`guide ${response.status}`);
    return response.json();
  }
  window.zuckerOpenGuide = () => load().then((guide) => show(guide.text)).catch(() => {});
  function start() {
    const button = document.querySelector('#openGuide');
    if (started || !button) return;
    started = true;
    button.onclick = window.zuckerOpenGuide;
    load().then((guide) => { if (guide.unseen) showWhenFree(guide.text); }).catch(() => {});
  }
  document.addEventListener('DOMContentLoaded', start);
  start();
})();
