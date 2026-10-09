const assert = require('node:assert/strict');
const { fetchStateSnapshot } = require('./static/app.js');
(async () => {
  let requests = 0, finish;
  global.fetch = () => {
    requests++;
    return new Promise(resolve => { finish = resolve; });
  };
  const first = fetchStateSnapshot();
  const second = fetchStateSnapshot();
  assert.equal(first, second);
  assert.equal(requests, 1);
  finish({ ok: true, json: async () => ({ songs: [{ id: 2 }] }) });
  assert.deepEqual(await first, { songs: [{ id: 2 }] });
  const next = fetchStateSnapshot();
  assert.equal(requests, 2);
  finish({ ok: false, status: 503 });
  await assert.rejects(next, /HTTP 503/);
  const retry = fetchStateSnapshot();
  assert.equal(requests, 3);
  finish({ ok: true, json: async () => ({ songs: [] }) });
  await retry;
  console.log('State requests are shared while pending; subsequent requests and retries work.');
})().catch(error => { console.error(error); process.exitCode = 1; });
