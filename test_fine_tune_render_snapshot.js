const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('static/app.js','utf8');
const stems=[{file:'Synth.wav',role:'synth'},{file:'Guit.wav',role:'guitar'}];
const params={'Synth.wav':{makeup_gain_db:-7.5,user_fader_db:0},'Guit.wav':{makeup_gain_db:4,user_fader_db:0}};
const overrides={},live={},strips=[];let applied;
function element(){return {listeners:{},dataset:{},addEventListener(k,f){this.listeners[k]=f;}};}
const faders={dataset:{},set innerHTML(x){strips.length=0;},appendChild(s){strips.push(s);}};
const context={Number,Math,JSON,console:{log(){},error(){}},livePreviewOverrides:{},appState:{overrides:{songs:{'12':{stems:overrides}}},songs:[{id:12,active_stems:stems.map(s=>s.file)}],stems},document:{createElement(){const nodes={};return {dataset:{},set innerHTML(html){this.html=html;nodes['[data-fader]']=element();nodes['[data-fader]'].value=html.match(/data-fader[^>]*value="([^"]+)/)[1];},querySelector(q){return nodes[q]??=element();},querySelectorAll(){return [];}};}},orderedFaderGroups:(_,ss)=>ss.map(stem=>({stem,linked:[stem]})),stemOverrides:(_,f)=>overrides[f]??={},livePreviewStemOverrides:(_,f)=>live[f]??={},previewStemParams:(_,s)=>params[s.file],setLinkedOverride:(_,linked,k,v)=>linked.forEach(s=>{(overrides[s.file]??={})[k]=v;}),stemGainDb:()=>0,defaultPan:()=>0,stemEq:()=>({eq_low_cut_hz:30,eq_mid_gain_db:0,eq_air_gain_db:0}),plainName:s=>s.file,stemFamily:()=>'',esc:x=>x,signedDb:x=>`${x} dB`,panText:()=>'',previewSendGain:()=>0,previewMixFor:()=>null,faderGainNode:()=>null,previewMixKey:String,previewNodeId:()=>null,persistPreviewChange(){},applyLiveFaderGain:(_,s,value)=>{applied={file:s.file,value};},updatePreviewGains(){}};
vm.createContext(context);vm.runInContext(source.slice(source.indexOf('function automaticFaderLevel('),source.indexOf('function wireFineTuneControls(')),context);
context.songOverrides=()=>context.appState.overrides.songs['12'];
vm.runInContext(source.slice(source.indexOf('function setLinkedOverride('), source.indexOf('function syncLivePreviewSongOverride(')),context);
vm.runInContext(source.slice(source.indexOf('function cloneOverridesPayload('),source.indexOf('function syncOverrideSequenceFromState(')),context);
const root={querySelector:()=>faders};context.renderFaders(root,12);
assert.equal(Number(strips[0].querySelector('[data-fader]').value),-7.5);
assert.equal(Number(strips[1].querySelector('[data-fader]').value),4);
const slider=strips[0].querySelector('[data-fader]');slider.value='-4.5';slider.listeners.input();
assert.equal(overrides['Synth.wav'].fader_db,3);assert.equal(overrides['Guit.wav'].fader_db,0);assert.equal(overrides['Guit.wav'].user_confirmed,undefined);
assert.equal(applied.file,'Synth.wav');assert.equal(applied.value,3);
context.renderFaders(root,12);assert.equal(Number(strips[0].querySelector('[data-fader]').value),-4.5);assert.equal(Number(strips[1].querySelector('[data-fader]').value),4);
console.log('Actual renderFaders/input/reopen: saved synth +3dB, guitar unchanged, no duplicate automatic gain.');

const payload=context.cloneOverridesPayload();
assert.equal(payload.songs['12'].stems['Synth.wav'].user_confirmed,true);
assert.equal(payload.songs['12'].stems['Synth.wav'].fader_db,3);
assert.notEqual(payload.songs['12'].stems['Guit.wav'].user_confirmed,true);
const gain=strips[0].querySelector('[data-gain]');gain.value='2';context.applyLivePreGain=()=>{};gain.listeners.input();
const gainPayload=context.cloneOverridesPayload();assert.equal(gainPayload.songs['12'].stems['Synth.wav'].user_confirmed,true);assert.equal(gainPayload.songs['12'].stems['Synth.wav'].gain_db,2);
console.log('Actual Fine Tune handlers preserve manual confirmation in live preview and render snapshot.');
