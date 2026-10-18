'use strict';
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const context={window:{}};
vm.runInNewContext(fs.readFileSync('static/js/manga_chapter_tools.js','utf8'),context);
const tools=context.window.mangaChapterTools;
const rows=[{id:'vol',name:'第01卷',group:'单行本'},
  {id:'old',name:'第41話',group:'默认'},{id:'new',name:'第42話',group:'默认'}];
assert.equal(tools.find(rows,'42话')[0].index,2);
assert.equal(tools.find(rows,'42话')[0].row.id,'new');
assert.equal(tools.find(rows,'单行本')[0].index,0);
const many=Array.from({length:600},(_,i)=>({id:'id-'+i,name:'同名',group:'默认'}));
assert.equal(tools.find(many,'同名').length,100);
const report=tools.report({chapters:many,cover:'SECRET_URL',intro:'PRIVATE_TEXT',
  downloaded:['id-0'],partial_downloaded:['id-1']},'demo','comic',true);
assert.equal(report.chapters.length,500);
assert.equal(report.omitted_chapters,100);
assert.equal(report.same_name_entries.length,1);
assert.equal(report.chapters[0].downloaded,true);
assert.equal(report.chapters[1].partial,true);
assert.equal(JSON.stringify(report).includes('SECRET_URL'),false);
assert.equal(JSON.stringify(report).includes('PRIVATE_TEXT'),false);
const twins=[
  {id:'3ed54ac2-c519-11f1-a73b-fa163e02432f',name:'第41話',group:'默认'},
  {id:'3b77390f-c4d7-11f1-8ff4-fa163e02432f',name:' 第41話 ',group:'默认'}];
assert.equal(twins[0].id.slice(-12),twins[1].id.slice(-12));
const hints=tools.identityHints(twins);
assert.equal(hints.size,2);
assert.notEqual(hints.get(twins[0].id),hints.get(twins[1].id));
assert.equal(hints.get(twins[0].id),'3ed54ac2…');
const sharedPrefix='same-prefix-'.repeat(5);
const longRows=['a','b'].map(suffix=>({id:sharedPrefix+suffix,name:'同名'}));
const longHints=tools.identityHints(longRows);
assert.notEqual(longHints.get(longRows[0].id),longHints.get(longRows[1].id));
assert.equal(tools.identityHints([{id:'same',name:'one'},{id:'same',name:'one'}]).size,0);
assert.equal(tools.identityHints(rows).size,0);
console.log('manga_chapter_tools: original identity/index, bounded search and metadata-only export passed');
