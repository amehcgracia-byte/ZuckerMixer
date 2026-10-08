const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/app.js', 'utf8');
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function automaticFaderLevel('), source.indexOf('function renderFaders(')), context);
const synth = {makeup_gain_db: -7.5, computed_gain_db: -8};
const guitar = {makeup_gain_db: 4};
assert.equal(context.displayedFaderLevel(synth, 0), -7.5);
assert.equal(context.displayedFaderLevel(guitar, 0), 4);
// Raising only the synth by 3 dB saves a relative adjustment. The automatic
// gain remains in the DSP path once; reopening restores the chosen level.
const trim = context.userTrimFromFader(synth, -4.5);
assert.equal(trim, 3);
assert.equal(context.displayedFaderLevel(synth, trim), -4.5);
assert.equal(context.displayedFaderLevel(guitar, 0), 4);
assert.equal(context.displayedFaderLevel(null, 0), 0);
assert.equal(context.displayedFaderLevel({computed_gain_db: -12}, 2), -10);
console.log('Automatic levels, one-stem edits and saved relative adjustments agree.');
