'use strict';
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const code = fs.readFileSync('static/js/reader_screen_awake.js', 'utf8');
const flush = async () => {for(let i=0;i<8;i++) await Promise.resolve();};
function environment({secure=true, supported=true}={}) {
  const dl = {}, wl = {}, requests = [];
  const document = {visibilityState:'visible', addEventListener:(name, fn) => dl[name]=fn};
  const window = {isSecureContext:secure, addEventListener:(name, fn) => wl[name]=fn};
  const navigator = supported ? {wakeLock:{request(type) {
    assert.equal(type, 'screen');
    return new Promise((resolve,reject)=>requests.push({resolve,reject}));
  }}} : {};
  vm.runInNewContext(code,{window,document,navigator,Promise});
  return {requests, hide(){document.visibilityState='hidden'; dl.visibilitychange?.();},
    show(){document.visibilityState='visible'; dl.visibilitychange?.();},
    event(name){wl[name]?.();}};
}
function lock() {
  let onRelease;
  return {released:0, addEventListener(name,fn){assert.equal(name,'release');onRelease=fn;},
    release(){this.released++; onRelease?.(); return Promise.resolve();},
    revoke(){onRelease?.();}};
}
(async () => {
  assert.equal(environment({secure:false}).requests.length,0);
  assert.equal(environment({supported:false}).requests.length,0);
  const e=environment(), first=lock();
  assert.equal(e.requests.length,1);
  e.event('pageshow'); e.show();
  assert.equal(e.requests.length,1,'pending request must not duplicate');
  e.requests[0].resolve(first); await flush();
  e.hide(); assert.equal(first.released,1);
  e.show(); assert.equal(e.requests.length,2);
  const second=lock(); e.requests[1].resolve(second); await flush();
  second.revoke(); await flush(); assert.equal(e.requests.length,2,'no revocation spin');
  e.hide(); e.show(); assert.equal(e.requests.length,3);
  e.requests[2].reject(new Error('permission denied')); await flush();
  assert.equal(e.requests.length,3,'no denial retry spin');
  e.hide(); e.show(); const late=lock();
  e.hide(); e.show();
  e.requests[3].resolve(late); await flush();
  assert.equal(late.released,1,'stale hidden request must release');
  assert.equal(e.requests.length,5,'visible new generation gets its own lease');
  const current=lock(); e.requests[4].resolve(current); await flush();
  e.event('pagehide'); assert.equal(current.released,1);
  e.show(); assert.equal(e.requests.length,5,'hidden page stays suspended');
  e.event('pageshow'); assert.equal(e.requests.length,6,'bfcache return reacquires');
  e.event('pagehide'); const afterExit=lock(); e.requests[5].resolve(afterExit); await flush();
  assert.equal(afterExit.released,1);
  assert.equal(e.requests.length,6);
  for(const template of ['reader','manga_reader'])
    assert(fs.readFileSync(`templates/${template}.html`,'utf8').includes('/static/js/reader_screen_awake.js'));
  console.log('reader screen awake: optional support, pending/stale leases, visibility, revocation, denial and bfcache passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
