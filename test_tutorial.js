const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Node {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.parts = {}; this.attributes = []; this.classList = { add() {}, remove() {} }; this.textContent = ''; this.disabled = false; }
  set innerHTML(_) {} // the tour skeleton; parts are created on demand
  querySelector(sel) { return (this.parts[sel] ||= new Node()); }
  querySelectorAll(sel) { return sel === 'button' ? this.querySelector('.tour-actions').children : []; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {} removeAttribute() {} addEventListener() {} focus() {}
  showModal() { this.open = true; } close() { this.open = false; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 0, height: 0, top: 0, bottom: 0 }; }
}

async function setup(saved, { busy = false } = {}) {
  const posts = []; const timers = []; const events = [];
  const footer = new Node('button'); let opened = null; let blocked = busy;
  const context = {
    window: { addEventListener() {}, dispatchEvent: (e) => events.push(e.type), scrollY: 0, scrollTo() {}, scrollBy() {}, innerHeight: 800 },
    Event: class { constructor(type) { this.type = type; } },
    document: {
      body: { append: (d) => { opened = d; } }, activeElement: null, addEventListener() {},
      createElement: (tag) => new Node(tag),
      querySelector: (sel) => sel === '#openTutorial' ? footer : sel === 'dialog[open]' && blocked ? {} : null,
    },
    fetch: async (url, options = {}) => {
      if (options.method === 'POST') { posts.push(JSON.parse(options.body).answer); return { ok: true, json: async () => ({}) }; }
      return { ok: true, json: async () => ({ answer: saved }) };
    },
    setTimeout: (fn) => timers.push(fn),
  };
  vm.runInNewContext(fs.readFileSync('static/tutorial.js', 'utf8'), context);
  const flush = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
  return { context, footer, posts, timers, events, flush, dialog: () => opened, free: () => { blocked = false; } };
}

(async () => {
  // New profile: Einstein asks, and "No" is remembered.
  const fresh = await setup(null);
  assert.equal(fresh.timers.length, 1, 'offer is scheduled after startup');
  fresh.timers.shift()(); await fresh.flush();
  const dialog = fresh.dialog();
  assert.equal(dialog.open, true);
  assert.equal(dialog.querySelector('#tourTitle').textContent, 'Want to learn what ZuckerMixer can do?');
  const [no, yes] = dialog.querySelector('.tour-actions').children;
  assert.equal(yes.textContent, 'Yes, show me');
  await no.onclick(); await fresh.flush();
  assert.deepEqual(fresh.posts, ['no']);
  assert.equal(dialog.open, false);
  assert.deepEqual(fresh.events, ['tutorialclosed'], 'a waiting update offer is released');

  // "Yes" starts the tour at step 1; Next walks on.
  const yesRun = await setup(null);
  yesRun.timers.shift()(); await yesRun.flush();
  await yesRun.dialog().querySelector('.tour-actions').children[1].onclick(); await yesRun.flush();
  assert.deepEqual(yesRun.posts, ['yes']);
  assert.match(yesRun.dialog().querySelector('.tour-count').textContent, /^1 \/ \d+ · ZUCKERMIXER$/);
  yesRun.dialog().querySelector('.tour-actions').children[1].onclick();
  assert.match(yesRun.dialog().querySelector('.tour-count').textContent, /^2 \//);

  // Already answered: no question, but the footer button replays the tour.
  const answered = await setup('no');
  answered.timers.shift()(); await answered.flush();
  assert.equal(answered.dialog(), null);
  answered.footer.onclick();
  assert.equal(answered.dialog().open, true);
  assert.match(answered.dialog().querySelector('.tour-count').textContent, /^1 \//);

  // Waits while another dialog (an update offer) is open.
  const waiting = await setup(null, { busy: true });
  waiting.timers.shift()(); await waiting.flush();
  assert.equal(waiting.dialog(), null);
  waiting.free(); waiting.timers.shift()(); await waiting.flush();
  assert.equal(waiting.dialog().open, true);
  assert.equal(waiting.context.window.MixerTutorial.isOpen(), true);
  console.log('Einstein tutorial: asks once, remembers the answer, replays from the footer, waits for other dialogs: OK');
})().catch((error) => { console.error(error); process.exit(1); });
