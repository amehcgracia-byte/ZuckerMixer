const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let Processor;
vm.runInNewContext(fs.readFileSync('static/vocal-expander.js', 'utf8'), {
  AudioWorkletProcessor: class {}, sampleRate: 48000,
  registerProcessor: (_name, implementation) => { Processor = implementation; },
});
function run(level, blocks = 800) {
  const processor = new Processor({processorOptions: {}});
  let input = new Float32Array(128).fill(level);
  let output = new Float32Array(128);
  for (let n = 0; n < blocks; n++) processor.process([[input]], [[output]]);
  return output;
}
assert.ok(run(0).every(x => x === 0), 'silence must stay exactly silent');
const quiet = run(1e-5);
assert.ok(Math.abs(20 * Math.log10(quiet[127] / 1e-5) + 12) < 0.01,
  'quiet microphone floor receives downward attenuation');
const voice = run(0.1);
assert.ok(Math.abs(voice[127] - 0.1) < 1e-6,
  'loud voice must retain amplitude, rather than being compressed');
const processor = new Processor({processorOptions: {}});
const input = [new Float32Array(128).fill(0.1), new Float32Array(128).fill(1e-5)];
const output = [new Float32Array(128), new Float32Array(128)];
for (let n = 0; n < 800; n++) processor.process([input], [output]);
assert.ok(Math.abs(output[0][127] - 0.1) < 1e-6);
assert.ok(output[1][127] < 3e-6, 'channels keep independent envelope history');
const source = fs.readFileSync('static/app.js', 'utf8');
assert.ok(source.includes('stem.role === "vocal" || previewEffectEnabled'));
assert.ok(source.includes('audioWorklet.addModule("/static/vocal-expander.js")'));
console.log('Preview microphone expander preserves voice and attenuates quiet noise.');
