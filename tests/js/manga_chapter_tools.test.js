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
console.log('manga_chapter_tools: original identity/index, bounded search and metadata-only export passed');
