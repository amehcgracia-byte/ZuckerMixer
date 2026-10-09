const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const controls = source.slice(source.indexOf('function setupDismissControls()'), source.indexOf('let stateFetchPromise'));
const listeners = {};
let paused = 0, rendered = 0, videoPaused = 0;
const overlay = {addEventListener(type, handler) {listeners[type] = handler;}};
const close = {addEventListener(type, handler) {listeners.close = handler;}};
const context = {suppressLoadingOverlay: false,
  window: {renderListening: {pause() {paused++;}}},
  renderLoadingOverlay() {rendered++;},
  document: {addEventListener() {}, querySelector() {return {pause() {videoPaused++;}};}},
  $(selector) {return selector === '#loadingOverlay' ? overlay : close;}
};
vm.runInNewContext(controls, context);
context.setupDismissControls();
// Child clicks never dismiss, even after their button was removed from DOM.
listeners.click({target: {}, currentTarget: overlay});
assert.equal(rendered, 0);
listeners.click({target: overlay, currentTarget: overlay});
assert.equal(rendered, 1);
assert.equal(paused, 1);
assert.equal(videoPaused, 1);
assert.equal(context.suppressLoadingOverlay, true);
console.log('Overlay child clicks preserved; explicit dismissal pauses preview: OK');
