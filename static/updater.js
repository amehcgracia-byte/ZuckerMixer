/* Native update operations are exposed only by the desktop bridge. */
(() => {
  let started = false;
  let dialog = null;
  let promptedVersion = null;
  let checking = false;
  let installing = false;
  window.canInstallUpdate = () => !renderInProgress && !cutIsDirty() && overrideWritesInFlight === 0 && !overrideSaveTimer;
  const api = () => window.pywebview?.api;
  const notice = (text) => { document.querySelector('#updateNotice').textContent = text; };
  function closeOffer() { if (dialog) { dialog.close(); dialog.remove(); dialog = null; } }
  async function offer(status) {
    if (promptedVersion === status.version) return;
    promptedVersion = status.version;
    closeOffer();
    dialog = document.createElement('dialog'); dialog.className = 'message-dialog update-dialog';
    const title = document.createElement('h2'); title.textContent = `ZuckerMixer ${status.version} is available`;
    const text = document.createElement('p'); text.textContent = 'Install the update? ZuckerMixer will close and restart. Your projects and cuts will be preserved.';
    const update = document.createElement('button'); update.textContent = 'Update and restart';
    const later = document.createElement('button'); later.textContent = 'Later'; later.onclick = closeOffer;
    const progress = document.createElement('p'); progress.setAttribute('role', 'status');
    update.onclick = async () => {
      if (!window.canInstallUpdate()) { progress.textContent = 'Save your cuts and wait for current work to finish before updating.'; return; }
      installing = true; update.disabled = true; later.disabled = true;
      dialog.addEventListener('cancel', (event) => event.preventDefault());
      dialog.querySelector('[data-dialog-close]')?.remove();
      try {
        const result = await api().install_update();
        if (result.status === 'error') throw new Error(result.error);
        const poll = async () => {
          const state = await api().update_status();
          if (state.status === 'error') { installing = false; progress.textContent = `Update failed: ${state.error}`; later.disabled = false; later.textContent = 'Close'; return; }
          progress.textContent = state.status === 'downloading' ? `Downloading update: ${state.progress}%` : state.status === 'verifying' ? 'Verifying update…' : 'Restarting ZuckerMixer…';
          setTimeout(() => poll().catch(() => {}), 750);
        };
        poll();
      } catch (error) { installing = false; progress.textContent = error.message; later.disabled = false; }
    };
    dialog.append(title, text, update, later, progress); document.body.append(dialog);
    makeDialogDismissible(dialog, () => { if (!installing) closeOffer(); }); dialog.showModal(); later.focus();
  }
  async function check(manual = false) {
    if (checking || !api()) return;
    checking = true;
    if (manual) promptedVersion = null;
    notice('Checking for updates…');
    try {
      await api().check_update();
      const poll = async () => {
        const state = await api().update_status();
        if (state.status === 'checking') { setTimeout(() => poll().catch(failed), 500); return; }
        checking = false;
        if (state.status === 'available') { notice(`Update ${state.version} available`); offer(state); }
        else if (state.status === 'current') notice(manual ? 'You have the latest version.' : '');
        else notice(manual ? `Could not check for updates${state.error ? `: ${state.error}` : '.'} Please try again later.` : '');
      };
      poll().catch(failed);
    } catch (error) { failed(error); }
  }
  function failed() { checking = false; notice(''); }
  function start() {
    if (started || !api() || !document.querySelector('#checkUpdate')) return;
    started = true;
    const button = document.querySelector('#checkUpdate'); button.hidden = false; button.onclick = () => check(true);
    check();
  }
  window.addEventListener('pywebviewready', start);
  document.addEventListener('DOMContentLoaded', start);
  start();
})();
