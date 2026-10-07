/* Listen to completed, immutable exports while the next songs render. */
(() => {
  const player = new Audio(); player.preload = 'none';
  let platform = null;
  let source = null, ready = new Map(), selected = null, pending = false, generation = 0, request = null;
  const icon = playing => playing ? '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="2" width="4" height="12"/><rect x="9" y="2" width="4" height="12"/></svg>' : '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2 L14 8 L4 14 Z"/></svg>';
  const playing = () => Boolean(selected && (pending || (!player.paused && !player.ended)));
  function sync() {
    document.querySelectorAll('#songList .song-card').forEach(card => {
      const song = appState?.songs?.find(item => String(item.id) === card.dataset.songId);
      if (!song || song.index == null) { card.querySelector('.render-listen-button')?.remove(); return; }
      const index = Number(song.index), entry = ready.get(index) || (selected?.index === index && playing() ? selected : null);
      let button = card.querySelector('.render-listen-button');
      if (!entry) { button?.remove(); return; }
      if (!button) {
        button = document.createElement('button'); button.type = 'button'; button.className = 'render-listen-button';
        button.onclick = () => toggle(index).catch(error => { pending = false; selected = null; sync(); showToast(`Could not play render: ${error.message || error}`); });
        card.querySelector('.song-title')?.prepend(button);
      }
      const active = selected?.index === index && playing();
      button.innerHTML = `<span>${String(index).padStart(2,'0')}</span>${icon(active)}`;
      button.setAttribute('aria-label', `${active ? 'Pause' : 'Play'} rendered song ${index}`);
      button.setAttribute('aria-pressed', String(active));
      button.title = `Completed render · v${(active ? selected.version : entry.version) ?? ''}`;
    });
    const folder = document.querySelector('#openRenderFolder');
    if (folder) { folder.disabled = ready.size === 0; folder.textContent = platform === 'darwin' ? 'Open in Finder' : platform === 'win32' ? 'Open in Explorer' : 'Open renders folder'; }
  }
  function pause() { generation++; pending = false; player.pause(); sync(); }
  function reset() { generation++; pause(); selected = null; ready.clear(); source = null; player.removeAttribute('src'); player.load(); sync(); }
  async function toggle(index) {
    if (selected?.index === index && playing()) { pause(); return; }
    const entry = ready.get(index) || (selected?.index === index ? selected : null); if (!entry) return;
    const token = ++generation;
    document.querySelectorAll('audio').forEach(audio => audio.pause());
    if (typeof previewMixes !== 'undefined' && typeof stopPreviewMix === 'function') {
      await Promise.all(Object.keys(previewMixes).map(id => stopPreviewMix(id)));
    }
    if (token !== generation) return;
    if (!selected || selected.index !== index || player.ended || selected.url !== entry.url) {
      player.pause(); selected = entry; player.src = entry.url; player.currentTime = 0;
    }
    pending = true; sync();
    try { await player.play(); }
    catch (error) { if (token === generation) { pending = false; sync(); throw error; } }
  }
  async function poll(folder) {
    if (!folder) return;
    if (source !== folder) { reset(); source = folder; }
    if (request) return request;
    const requestedSource = source;
    request = fetch('/api/render-previews').then(response => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); }).then(data => {
      if (source !== requestedSource || appState?.source_folder !== requestedSource || data.source_folder !== requestedSource) return;
      platform = data.platform;
      const active = (appState?.jobs || []).find(job => ['render','mix'].includes(job.kind) && ['running','stopping'].includes(job.status) && (!job.source_folder || job.source_folder === requestedSource))
        || (appState?.jobs || []).find(job => ['render','mix'].includes(job.kind) && job.status === 'queued' && (!job.source_folder || job.source_folder === requestedSource));
      const requestedSongs = new Set((active?.songs || []).map(Number));
      ready = new Map((data.items || []).filter(entry => {
        const song = appState?.songs?.find(song => Number(song.index) === Number(entry.index) && song.index != null);
        return !active || !requestedSongs.has(Number(song?.id)) || String(entry.job_id) === String(active.id);
      }).map(entry => [Number(entry.index), entry])); sync();
    }).finally(() => { request = null; });
    return request;
  }
  player.addEventListener('playing', () => { pending = false; sync(); });
  player.addEventListener('pause', sync);
  player.addEventListener('ended', () => { pending = false; selected = null; sync(); });
  player.addEventListener('error', () => { pending = false; selected = null; sync(); showToast('Could not read this completed render. Check that its disk is connected.'); });
  document.addEventListener('play', event => { if (event.target.tagName === 'AUDIO') pause(); }, true);
  document.querySelector('#openRenderFolder')?.addEventListener('click', async () => {
    try { const response = await fetch('/api/open-render-folder', {method:'POST'}); if (!response.ok) throw new Error('Could not open renders folder'); }
    catch (error) { showToast(error.message); }
  });
  window.renderListening = { sync, poll, pause, reset, isPlaying: playing };
})();
