'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync('templates/manga_reader.html','utf8');
const code=html.match(/function resolveEntryChapter\([^)]*\) \{[\s\S]*?\n\}/)[0];
const context={};
vm.runInNewContext(code+'; globalThis.resolve=resolveEntryChapter;',context);
const rows=[{id:'new-42'},{id:'old-41'}];
assert.equal(context.resolve(rows,'new-42',1),0,'identity must win after catalog reorder');
assert.equal(context.resolve(rows,'removed',1),-1,'missing identity must not guess another chapter');
assert.equal(context.resolve(rows,'',1),1,'legacy index links remain compatible');
assert.equal(context.resolve(rows,'',NaN),0,'invalid numeric links cannot produce NaN navigation');
assert.equal(context.resolve(rows,'',-4),0);
assert.equal(context.resolve([], '', 0),-1);
assert.match(html,/!params\.has\('pg'\) && _resume\.index === chIdx0/,
  'explicit page choice must not be overwritten by same-chapter resume');
console.log('manga_reader_entry_identity: identity/reorder/missing/legacy/explicit-page contracts passed');
