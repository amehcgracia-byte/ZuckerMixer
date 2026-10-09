const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.open = false; }
  append(...items) { this.children.push(...items); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() { this.removed = true; }
  focus() {}
}
const tick = () => new Promise((r) => setImmediate(r));
async function setup(guide, { otherDialogOpen = false } = {}) {
  const button = new Element('button'), body = new Element('body');
  const posts = []; const timers = []; let busy = otherDialogOpen;
  const context = {
    fetch: async (url, options = {}) => {
      if (options.method === 'POST') { posts.push(url); return { ok: true, json: async () => ({}) }; }
      return { ok: true, json: async () => guide };
    },
    document: {
      body, createElement: (tag) => new Element(tag), addEventListener() {},
      querySelector: (sel) => (sel === '#openGuide' ? button : sel === 'dialog[open]' && busy ? {} : null),
    },
    makeDialogDismissible() {},
    setTimeout: (fn) => timers.push(fn),
  };
  vm.runInNewContext(fs.readFileSync('static/guide.js', 'utf8'), context);
  await tick(); await tick();
  return { button, body, posts, timers, free: () => { busy = false; } };
}
(async () => {
  const text = 'ZUCKER MIXER — READ ME FIRST';
  const fresh = await setup({ text, unseen: true });
  assert.equal(fresh.body.children.length, 1);
  const dialog = fresh.body.children[0];
  assert.equal(dialog.open, true);
  assert.equal(dialog.children[1].textContent, text);
  dialog.children[2].onclick(); await tick();
  assert.equal(dialog.removed, true);
  assert.deepEqual(fresh.posts, ['/api/guide/seen']);

  const seen = await setup({ text, unseen: false });
  assert.equal(seen.body.children.length, 0);
  seen.button.onclick(); await tick(); await tick();
  assert.equal(seen.body.children.length, 1, 'footer button reopens the guide');

  const waiting = await setup({ text, unseen: true }, { otherDialogOpen: true });
  assert.equal(waiting.body.children.length, 0, 'waits for the update offer to close');
  waiting.free(); waiting.timers.shift()();
  assert.equal(waiting.body.children.length, 1);
  console.log('Guide shows once per version, waits for other dialogs and reopens from footer: OK');
})().catch((error) => { console.error(error); process.exit(1); });
