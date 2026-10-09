const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Menu dispatch: menu items press the window's buttons, special items open dialogs.
{
  const clicks = []; const toasts = []; const opened = [];
  const buttons = {
    mixAll: { disabled: false, hidden: false, textContent: 'Mix everything', click: () => clicks.push('mixAll') },
    openRenderFolder: { disabled: true, hidden: false, textContent: ' Open renders folder ', click: () => clicks.push('openRenderFolder') },
  };
  const context = {
    window: { MixerTutorial: { start: () => opened.push('tutorial') }, zuckerOpenAbout: () => opened.push('about') },
    document: { getElementById: (id) => buttons[id] || null },
    showToast: (text) => toasts.push(text),
  };
  vm.runInNewContext(fs.readFileSync('static/menu.js', 'utf8'), context);
  const menu = context.window.zuckerMenu;
  assert.equal(menu('mixAll'), true);
  assert.deepEqual(clicks, ['mixAll']);
  assert.equal(menu('openRenderFolder'), false);
  assert.deepEqual(clicks, ['mixAll'], 'disabled buttons are not pressed');
  assert.deepEqual(toasts, ['Open renders folder is not available right now.']);
  assert.equal(menu('tutorial'), true); assert.equal(menu('about'), true);
  assert.deepEqual(opened, ['tutorial', 'about']);
  assert.equal(menu('missing'), false);
}

// About window: version line, bundled credits, external links open outside the app.
(async () => {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.links = []; }
    append(...items) { this.children.push(...items); }
    set innerHTML(html) { this.html = html; this.links = (html.match(/<a /g) || []).map(() => ({})); }
    get innerHTML() { return this.html; }
    querySelectorAll() { return this.links; }
    showModal() { this.open = true; } close() { this.open = false; } remove() {} focus() {}
  }
  const body = new Element('body'); const footer = new Element('button');
  const context = {
    window: {},
    fetch: async () => ({ ok: true, json: async () => ({ app_version: '2.8.11', build_timestamp: '2026-10-09', source_revision: 'abcdef123456', credits_html: '<p><b>Created by José Manuel García (JM.G)</b> <a href="https://github.com/x">GitHub</a></p>' }) }),
    document: { body, createElement: (tag) => new Element(tag), querySelector: (sel) => (sel === '#openAbout' ? footer : null), addEventListener() {} },
    makeDialogDismissible() {},
  };
  vm.runInNewContext(fs.readFileSync('static/about.js', 'utf8'), context);
  assert.equal(footer.onclick, context.window.zuckerOpenAbout);
  await context.window.zuckerOpenAbout();
  const dialog = body.children[0];
  assert.equal(dialog.open, true);
  assert.match(dialog.children[1].textContent, /Version 2\.8\.11 .* abcdef1$/);
  assert.match(dialog.children[2].innerHTML, /José Manuel García/);
  assert.deepEqual(dialog.children[2].links, [{ target: '_blank', rel: 'noopener' }]);
  console.log('Menu dispatch, disabled-button guard and About window: OK');
})().catch((error) => { console.error(error); process.exit(1); });
