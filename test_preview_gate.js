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

vm.runInContext(source.slice(source.indexOf('function preservePreviewMixParams('),source.indexOf('async function refreshState(')),context);
const previous={source_folder:'source',songs:[{id:2,start:10,end:50,revision:3,mix_params:{stems:{bass:{gate_enabled:true,gate_points:[[0,0],[4,1]]}}}}]};
const next={source_folder:'source',songs:[{id:2,start:10,end:50,revision:3}]};
context.preservePreviewMixParams(next,previous);
assert.equal(next.songs[0].mix_params,previous.songs[0].mix_params,'job polling must retain gate automation');
const changed={source_folder:'source',songs:[{id:2,start:11,end:50,revision:3}]};
context.preservePreviewMixParams(changed,previous);assert.equal(changed.songs[0].mix_params,undefined,'changing a cut invalidates gate timing');
const other={source_folder:'other',songs:[{id:2,start:10,end:50,revision:3}]};
context.preservePreviewMixParams(other,previous);assert.equal(other.songs[0].mix_params,undefined,'changing source invalidates the plan');
