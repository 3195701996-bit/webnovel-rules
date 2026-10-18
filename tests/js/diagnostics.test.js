'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {disabled:false, dataset:{}, listeners:{},
      addEventListener(type, fn) { this.listeners[type] = fn; }});
    return nodes.get(id);
  };
  const requests = [];
  let report = {ok:true, text:'诊断内容 <script>不会执行</script>'};
  let healthError = false;
  const blobs = [];
  const downloads = [];
  const context = vm.createContext({
    document: {getElementById:node, body:{appendChild() {}},
      createElement() { return {click() { downloads.push(this.download); }, remove() {}}; }},
    URL: {createObjectURL(blob) { blobs.push(blob); return 'blob:selftest'; }, revokeObjectURL() {}},
    Blob, Date, setTimeout(fn) { fn(); },
    async requestJSON(url, options = {}) {
      requests.push(url);
      if (options.button) options.button.disabled = true;
      try {
        if (url === '/api/health') {
          if (healthError) throw new Error('连接失败，请刷新');
          return {ok:true, runtime:{state:'ready'}, manga_sources:[]};
        }
        if (url === '/api/diagnostics/events') return {ok:true, failed_requests:[], errors:[]};
        if (url === '/api/diagnostics/report') return report;
        throw new Error('Unexpected request: ' + url);
      } finally { if (options.button) options.button.disabled = false; }
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../../static/js/diagnostics.js'), 'utf8');
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests, ['/api/health', '/api/diagnostics/events']);
  await node('report-generate').listeners.click();
  assert.equal(node('report-text').value, report.text);
  assert.equal(node('report-download').disabled, false);
  node('report-download').listeners.click();
  assert.equal(await blobs[0].text(), report.text);
  assert.equal(downloads.length, 1);
  for (const invalid of [{ok:false, text:'old'}, {ok:true, text:''}, {ok:true, text:42}]) {
    report = invalid;
    await node('report-generate').listeners.click();
    assert.equal(node('report-download').disabled, true);
    assert.equal(node('report-text').value, '');
    assert.equal(node('report-preview').hidden, true);
    assert.equal(node('report-generate').disabled, false);
    node('report-download').listeners.click();
    assert.equal(downloads.length, 1, 'failed generation must not export stale content');
  }
  healthError = true;
  await node('diagnostics-refresh').listeners.click();
  assert.equal(node('engine-status').dataset.error, 'true');
  assert.equal(node('events-status').dataset.error, 'false');
  assert.equal(node('diagnostics-refresh').disabled, false);
  healthError = false;
  await node('diagnostics-refresh').listeners.click();
  assert.equal(node('engine-status').dataset.error, 'false');
  console.log('diagnostics: manual preview/download, stale protection, malformed responses and independent retry passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
