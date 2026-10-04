const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const source=fs.readFileSync('static/app.js','utf8');
const events=[];let enabled=true;
const context={previewStemParams:()=>({gate_points:[[0,1],[4,1],[6,0],[10,0],[12,1]]}),
 previewFxEnabled:()=>true,previewEffectEnabled:()=>enabled};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function schedulePreviewSectionGate('),source.indexOf('function reconnectPreviewStemFx(')),context);
const mix={instrumentGates:{kit:{gain:{cancelScheduledValues:t=>events.push(['cancel',t]),setValueAtTime:(v,t)=>events.push(['set',v,t]),linearRampToValueAtTime:(v,t)=>events.push(['ramp',v,t])}}}};
context.schedulePreviewSectionGate(mix,2,{file:'kit'},5,100);
assert.deepEqual(events,[['cancel',100],['set',.5,100],['ramp',0,101],['ramp',0,105],['ramp',1,107]],'seek halfway through closing fade must start at half gain');
events.length=0;enabled=false;
context.schedulePreviewSectionGate(mix,2,{file:'kit'},7,200);
assert.deepEqual(events,[['cancel',200],['set',1,200]],'turning Gate off must cancel closing ramps and restore unity');
console.log('Instrument Gate preserves seek timing and cancels automation when disabled.');
