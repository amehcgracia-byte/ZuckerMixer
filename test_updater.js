const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.hidden=false; this.disabled=false; this.dataset={}; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  setAttribute() {}
  addEventListener() {}
  querySelector() { return null; }
  showModal() { this.open=true; }
  close() { this.open=false; }
  remove() {}
  focus() {}
}
const tick = () => new Promise(r=>setImmediate(r));
async function setup(status) {
  const button=new Element('button'), notice=new Element('span'), body=new Element('body');
  let checks=0, installs=0;const timers=[];
  const context={
    window:{pywebview:{api:{check_update:async()=>{ checks++; },update_status:async()=>status,install_update:async()=>{ installs++; return {status:'error',error:'active job'}; }}},addEventListener(){}},
    document:{body,querySelector:sel=>sel==='#checkUpdate'?button:notice,createElement:tag=>new Element(tag),addEventListener(){}},
    makeDialogDismissible(){},renderInProgress:false,cutIsDirty:()=>false,overrideWritesInFlight:0,overrideSaveTimer:null,
    setTimeout:fn=>timers.push(fn),
  };
  vm.runInNewContext(fs.readFileSync('static/updater.js','utf8'),context);
  await tick();
  return {context,button,notice,body,checks:()=>checks,installs:()=>installs};
}
(async()=>{
  const current=await setup({status:'current',version:'2.8.2'});
  assert.equal(current.checks(),1);assert.equal(current.button.hidden,false);assert.equal(current.body.children.length,0);
  await current.button.onclick();await tick();assert.equal(current.notice.textContent,'You have the latest version.');
  const offered=await setup({status:'available',version:'2.8.3'});
  assert.equal(offered.body.children.length,1);const dialog=offered.body.children[0];
  assert.match(dialog.children[0].textContent,/2.8.3/);
  offered.context.renderInProgress=true;await dialog.children[2].onclick();assert.equal(offered.installs(),0);
  offered.context.renderInProgress=false;offered.context.cutIsDirty=()=>true;await dialog.children[2].onclick();assert.equal(offered.installs(),0);
  offered.context.cutIsDirty=()=>false;await dialog.children[2].onclick();assert.equal(offered.installs(),1);assert.match(dialog.children[4].textContent,/active job/);
  console.log('Updater startup, offer and unsaved/active-work guards: OK');
})().catch(error=>{console.error(error);process.exit(1);});
