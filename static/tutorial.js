/* Einstein guided tour (from Zucker Editor): read-only, never changes settings or starts work. */
(() => {
  const steps = [
    ['Mixes from your jam sessions', '.brand', 'ZuckerMixer turns the multitrack recording of a whole jam into finished songs. It finds each song, balances every instrument and masters it to MP3, all on your own computer.'],
    ['Your session folder', '#changeSourceFolder', 'Choose the folder with the WAV files exported from your session: one file per instrument, all starting at the same moment. ZuckerMixer remembers the folder and finds the songs in it.'],
    ['Which files to read', '#audioScanMode', 'Auto prefers aligned exports. “Aligned exports only” ignores everything else in the folder; “All files” is for advanced cases. Avoid duplicate stereo or full-session exports.'],
    ['How many songs?', '#knownSongCount', 'If you already know how many songs the session has, type the number and detection uses it as a guide. Leave it empty and ZuckerMixer decides.'],
    ['Sound like a real record', '.reference-control', 'Optionally choose a reference track. Mastering matches its loudness and tonal balance, so pick a professionally mastered song in a similar style, ideally around -10 to -14 LUFS.'],
    ['Your songs', '.song-card', 'Each card is one song found in the session, with its tempo, key and mix status. Tick it to include it in Mix selected. A warning tells you when a boundary needs a look.'],
    ['Name it', '.song-card .title-edit', 'Give the song its real title. The name appears on the card and on the finished mix.'],
    ['One song at a time', '.song-card .row-actions', 'Mix this one renders just this song. Select Cuts opens the editor for its start and end. Skip this one leaves it out of Mix everything. Reset to automatic mix discards your Fine-tune changes.'],
    ['Fine-tune every instrument', '.song-card .fine-tune', 'Open Fine-tune to adjust each instrument: level, pan, EQ, mute and solo, gate and effects. Preview render plays an exact 30-second excerpt; Play Preview gives a quick, approximate listen to the whole song.'],
    ['Where songs start and end', '#editAllCuts', 'Edit All Cuts opens the cut editor for the whole session. Listen, move the edges, add or remove cuts, then save. Saved cuts stay until you change them.'],
    ['Find the songs again', '#redetectSongs', 'If the songs were split badly, Re-detect listens to the recording again. You review the proposal before it replaces anything.'],
    ['Let Whisper listen', '#secondWhisperPass', 'Optional: Whisper listens to what is said between songs, such as announcements, to refine the boundaries. It takes longer than normal detection.'],
    ['Natural or loud', '#batchMaster', 'Choose the master style for the batch. Natural keeps more of the dynamics; Loud pushes the level higher.'],
    ['Mix', '.action-buttons', 'Mix selected renders the songs you ticked; Mix everything renders every song not marked Skip. You choose the output folder, and a song that fails never stops the rest.'],
    ['Follow the work', '.progress-box', 'See what is being processed and how far it has got. Cancel stops the job safely; songs that are already finished are kept.'],
    ['Listen to the results', '.results-head', 'Finished mixes appear here and in your output folder. Open renders folder takes you straight to the files.'],
    ['Everything at hand', '.app-footer', 'The menu bar at the top gathers every action. In Help you can replay this tutorial, check for updates and read About ZuckerMixer.']
  ];
  let dialog, index = -1, previousFocus, pending = false, spotlight = null, previousScroll = 0;
  const el = (tag, cls, text) => { const n = document.createElement(tag); n.className = cls; if (text) n.textContent = text; return n; };
  function build() {
    if (dialog) return;
    dialog = el('dialog', 'einstein-tour');
    dialog.setAttribute('aria-labelledby', 'tourTitle');
    dialog.innerHTML = `<svg class="tour-shade" aria-hidden="true"><defs><mask id="tourMask"><rect width="100%" height="100%" fill="white"/><ellipse id="tourHole" fill="black"/></mask></defs><rect width="100%" height="100%" fill="rgba(0,0,0,.82)" mask="url(#tourMask)"/><ellipse id="tourRing" fill="none" stroke="#f7d776" stroke-width="3"/></svg><div class="tour-demo" aria-hidden="true" inert></div><div class="tour-guide"><img src="/static/tutorial-einstein.png" alt="Einstein guides you through the tutorial"/><section class="tour-bubble"><span class="tour-count"></span><h2 id="tourTitle"></h2><p class="tour-copy"></p><p class="tour-error" role="status"></p><div class="tour-actions"></div></section></div><button class="tour-close" aria-label="Close tutorial">×</button>`;
    document.body.append(dialog);
    dialog.querySelector('.tour-close').onclick = dismiss;
    dialog.addEventListener('cancel', e => { e.preventDefault(); dismiss(); });
    // Keep tutorial controls away from application-wide keyboard/click handlers.
    for (const event of ['click', 'keydown', 'keyup', 'input', 'change']) dialog.addEventListener(event, e => e.stopPropagation());
    window.addEventListener('resize', position);
  }
  function button(label, action, primary = false) {
    const b = el('button', primary ? 'tour-primary' : '', label); b.type = 'button'; b.onclick = action;
    dialog.querySelector('.tour-actions').append(b); return b;
  }
  function open() {
    build(); previousFocus = document.activeElement; previousScroll = window.scrollY || 0;
    if (!dialog.open) dialog.showModal();
  }
  function close() {
    dialog.close(); window.dispatchEvent(new Event('tutorialclosed'));
    dialog.querySelector('.tour-demo').replaceChildren();
    window.scrollTo(0, previousScroll); previousFocus?.focus?.();
  }
  async function answer(value) {
    if (pending) return false;
    pending = true;
    dialog.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      const r = await fetch('/api/tutorial', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({answer: value})});
      if (!r.ok) throw new Error('Your answer could not be saved. Please try again.');
      return true;
    } catch (error) { dialog.querySelector('.tour-error').textContent = error.message; return false; }
    finally { pending = false; dialog.querySelectorAll('button').forEach(b => b.disabled = false); }
  }
  async function dismiss() { if (pending) return; if (index === -1 && !await answer('no')) return; close(); }
  function position() {
    if (!dialog?.open) return;
    const preview = dialog.querySelector('.tour-demo');
    const r = (spotlight || preview).getBoundingClientRect();
    const visible = index >= 0;
    for (const id of ['tourHole', 'tourRing']) {
      const ring = dialog.querySelector('#' + id);
      ring.setAttribute('cx', r.x + r.width / 2); ring.setAttribute('cy', r.y + r.height / 2);
      ring.setAttribute('rx', visible ? r.width / 2 + 16 : 0); ring.setAttribute('ry', visible ? r.height / 2 + 16 : 0);
    }
  }
  // The mixer page is long: bring the control to the top, above Einstein's
  // bubble and below the sticky top bar, which would otherwise cover it.
  function bringIntoView(source) {
    const bar = document.querySelector('.topbar');
    if (bar?.contains(source)) { window.scrollTo(0, 0); return; }
    source.scrollIntoView({block: 'start'});
    const covered = bar && ['sticky', 'fixed'].includes(getComputedStyle(bar).position) ? bar.getBoundingClientRect().bottom : 0;
    window.scrollBy(0, -(covered + 24));
  }
  function show() {
    const [title, selector, copy] = steps[index];
    dialog.classList.remove('tour-welcome');
    dialog.querySelector('#tourTitle').textContent = title;
    dialog.querySelector('.tour-count').textContent = `${index + 1} / ${steps.length} · ZUCKERMIXER`;
    dialog.querySelector('.tour-copy').textContent = copy;
    dialog.querySelector('.tour-error').textContent = '';
    const demo = dialog.querySelector('.tour-demo'); demo.hidden = false; spotlight = null; demo.replaceChildren(el('span', 'tour-preview-label', 'DEMO VIEW · ' + title));
    const source = document.querySelector(selector);
    if (source && !source.closest('dialog') && source.getClientRects().length) bringIntoView(source);
    const bounds = source?.getBoundingClientRect();
    if (bounds?.width && bounds.height && bounds.top >= 0 && bounds.bottom < window.innerHeight * .45 && !source.closest('dialog')) {
      spotlight = source; demo.hidden = true;
    } else if (source) {
      const clone = source.cloneNode(true);
      // Never retain IDs, media sources or actionable controls in the demonstration.
      for (const node of [clone, ...clone.querySelectorAll('*')]) {
        node.removeAttribute('id'); node.removeAttribute('autofocus');
        for (const attr of [...node.attributes]) if (attr.name.startsWith('on')) node.removeAttribute(attr.name);
        if (['VIDEO', 'AUDIO', 'SOURCE', 'IFRAME'].includes(node.tagName)) { node.removeAttribute('src'); node.removeAttribute('autoplay'); node.setAttribute('preload', 'none'); }
      }
      clone.hidden = false;
      if (clone.tagName === 'DETAILS') clone.open = true;
      demo.append(clone);
    }
    const actions = dialog.querySelector('.tour-actions'); actions.replaceChildren();
    const back = button('Back', () => { index--; show(); }); back.disabled = index === 0;
    button(index === steps.length - 1 ? 'Finish' : 'Next →', () => { if (index === steps.length - 1) close(); else { index++; show(); } }, true).focus();
    position();
  }
  function start() { open(); index = 0; show(); }
  async function offer() {
    const response = await fetch('/api/tutorial');
    if (!response.ok) return;
    const state = await response.json();
    if (state.answer) return;
    open(); index = -1; dialog.classList.add('tour-welcome');
    dialog.querySelector('#tourTitle').textContent = 'Want to learn what ZuckerMixer can do?';
    dialog.querySelector('.tour-count').textContent = 'WELCOME · YOUR GUIDE, EINSTEIN';
    dialog.querySelector('.tour-copy').textContent = 'I will walk you through the tools in ZuckerMixer, step by step. You set the pace.';
    dialog.querySelector('.tour-actions').replaceChildren();
    button('No', async () => { if (await answer('no')) close(); });
    button('Yes, show me', async () => { if (await answer('yes')) { index = 0; show(); } }, true).focus();
    position();
  }
  // Ask once the window is free: no other dialog and no loading screen on top.
  function offerWhenFree() {
    const overlay = document.querySelector('#loadingOverlay');
    if (document.querySelector('dialog[open]') || (overlay && !overlay.hidden)) { setTimeout(offerWhenFree, 1000); return; }
    offer().catch(() => {});
  }
  window.MixerTutorial = {start, offer, isOpen: () => Boolean(dialog?.open)};
  let started = false;
  function init() {
    const button = document.querySelector('#openTutorial');
    if (started || !button) return;
    started = true;
    button.onclick = start;
    setTimeout(offerWhenFree, 1500);
  }
  document.addEventListener('DOMContentLoaded', init);
  init();
})();
