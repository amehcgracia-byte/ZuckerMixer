const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
class Audio {
 constructor(){Audio.instance=this;this.paused=true;this.ended=false;this.events={};this.currentTime=0;}
 addEventListener(name,fn){this.events[name]=fn;}
 pause(){this.paused=true;this.events.pause?.();}
 play(){this.paused=false;this.ended=false;this.events.playing?.();return Promise.resolve();}
 removeAttribute(){this.src='';}
 load(){}
}
class Button {setAttribute(k,v){this[k]=v;} remove(){this.parent.button=null;}}
class Card {
 constructor(id){this.dataset={songId:String(id)};this.title={prepend:b=>{this.button=b;b.parent=this;}};}
 querySelector(s){return s==='.song-title'?this.title:this.button;}
}
const cards=[new Card(1),new Card(2)],folder={addEventListener(){}};
let data={source_folder:'/source',items:[]};const messages=[];
const context={Audio,window:{},appState:{source_folder:'/source',songs:[{id:1,index:0},{id:2,index:2}]},
 document:{querySelectorAll:s=>s==='audio'?[]:cards,querySelector:()=>folder,createElement:()=>new Button(),addEventListener(){}},
 fetch:async()=>({ok:true,json:async()=>data}),showToast:message=>messages.push(message)};
vm.runInNewContext(fs.readFileSync('static/render-listening.js','utf8'),context);
(async()=>{
 const listening=context.window.renderListening,player=Audio.instance;
 await listening.poll('/source');assert.equal(cards[0].button,undefined);assert.equal(folder.disabled,true);
 data.items=[{index:0,url:'/audio/rendered/0/first',version:1}];await listening.poll('/source');
 const first=cards[0].button;assert.equal(first['aria-label'],'Play rendered song 0');await first.onclick();
 assert.equal(first['aria-label'],'Pause rendered song 0');player.currentTime=11;
 data.items.push({index:2,url:'/audio/rendered/2/second',version:1});await listening.poll('/source');
 assert.equal(cards[0].button,first);assert.equal(player.currentTime,11);assert.equal(player.paused,false);
 await first.onclick();assert.equal(player.paused,true);await first.onclick();assert.equal(player.currentTime,11);
 data.items[0]={index:0,url:'/audio/rendered/0/new-version',version:2};await listening.poll('/source');assert.equal(player.src,'/audio/rendered/0/first');
 await cards[1].button.onclick();assert.equal(player.src,'/audio/rendered/2/second');assert.equal(player.currentTime,0);assert.equal(first['aria-label'],'Play rendered song 0');
 player.ended=true;player.events.ended();assert.equal(cards[1].button['aria-label'],'Play rendered song 2');
 context.appState.source_folder='/another';data={source_folder:'/another',items:[]};await listening.poll('/another');assert.equal(player.paused,true);assert.equal(folder.disabled,true);
 assert.equal(messages.length,0);
 console.log('Incremental rendered play/pause, resume, immutable source and project reset: OK');
})().catch(error=>{console.error(error);process.exit(1);});
