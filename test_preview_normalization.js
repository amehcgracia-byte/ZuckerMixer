const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/app.js', 'utf8');
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function estimatePreviewRmsDb('),
  source.indexOf('function previewTimeText(')), context);
function entry(name, level, channels = 1) {
  const data = new Float32Array(256).fill(level);
  return {stem:{file:name,role:'keys'},buffer:{length:data.length,
    numberOfChannels:channels,getChannelData:()=>data}};
}
const first = entry('first', 0.1);
const mix = {loaded:true,buffers:[first]};
const one = context.estimatePreviewRmsDb(mix);
assert.ok(Math.abs(one - (-23.0103)) < 0.001, 'equal-power mono pan');
mix.buffers.push(entry('silent', 0));
assert.equal(context.estimatePreviewRmsDb(mix), one, 'silent track must not change normalization');
mix.buffers.push(entry('second', 0.1));
assert.ok(Math.abs(context.estimatePreviewRmsDb(mix) - one - 6.0206) < 0.001,
  'coherent tracks must sum before measuring');
assert.ok(Number.isNaN(context.estimatePreviewRmsDb({loaded:true,buffers:[entry('zero',0)]})),
  'silence must not request huge makeup gain');
assert.ok(Math.abs(context.estimatePreviewRmsDb({loaded:true,buffers:[entry('stereo',0.1,2)]}) + 20) < 0.001);
console.log('Preview normalization measures the stereo sum and ignores silent-track count.');
