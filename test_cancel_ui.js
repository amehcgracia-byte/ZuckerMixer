const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const src=fs.readFileSync('static/app.js','utf8');
const code=src.slice(src.indexOf('let cancellationUiPending = false;'),src.indexOf('let jobsPollPromise = null;'));
const buttons={ '#cancelLoadingWork':{id:'cancelLoadingWork'}, '#cancelJob':{id:'cancelJob'} };
let polls=0,hidden=0,stoppingSeen=false;
const context={loadingOverlayJob:{status:'running'},suppressLoadingOverlay:false,cutSelector:null,
 $:s=>buttons[s]||null,showConfirm:async()=>true,showToast(){},window:{renderListening:{pause(){}}},
 renderLoadingOverlay(){stoppingSeen=context.loadingOverlayJob.status==='stopping';},setRenderControlsBusy(){},
 hideLoadingOverlayImmediately(){assert.ok(polls>=2,'keep loading screen until cancellation is confirmed');hidden++;},
 fetch:async()=>({ok:true,json:async()=>({ok:true})}),AbortController,Date,setTimeout,clearTimeout,
 pollJobs:async()=>++polls===1?[{status:'stopping'}]:[{status:'cancelled'}],refreshState:async()=>{}};
vm.createContext(context);vm.runInContext(code,context);
(async()=>{
 await vm.runInContext('cancelActiveWork()',context);
 assert.ok(stoppingSeen);assert.equal(hidden,0);assert.equal(buttons['#cancelLoadingWork'].textContent,'Canceling…');
 await new Promise(r=>setTimeout(r,500));assert.equal(hidden,1);assert.equal(buttons['#cancelLoadingWork'].disabled,false);
 console.log('Cancellation keeps loading screen and Canceling… until worker termination: OK');
})().catch(e=>{console.error(e);process.exit(1)});
