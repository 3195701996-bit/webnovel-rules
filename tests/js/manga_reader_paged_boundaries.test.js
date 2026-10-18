'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('templates/manga_reader.html', 'utf8');
const extract = name => html.match(new RegExp('function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n\\}'))?.[0];
class Element {
  constructor() { this.children=[]; this.style={}; this.className=''; this.isConnected=true; this.attrs={}; }
  set innerHTML(value) { this.children.forEach(c=>c.isConnected=false); this.children=[]; }
  get classList() { return {contains:n=>this.className.split(' ').includes(n), add:n=>this.className+=' '+n}; }
  appendChild(child) { child.isConnected=true; this.children.push(child); return child; }
  setAttribute(name,value) { this.attrs[name]=value; }
  querySelector(selector) { return this.children.find(c=>c.classList.contains(selector.slice(1))) || null; }
}
const stage=new Element(), timers=[], scrolls=[], listeners={};
const window={innerWidth:1280, innerHeight:844, _curImgs:Array.from({length:21},()=>({url:'/local',local:true})),
  scrollTo:(...args)=>scrolls.push(args), addEventListener:(name,fn)=>listeners[name]=fn};
const document={createElement:()=>new Element(), body:{scrollHeight:1500},
  querySelector:selector=>stage.querySelector(selector),
  getElementById:()=>({value:'11'})};
const ctx=vm.createContext({window,document,Math,settings:{mode:'double',flip:'zone'},
  _curGen:1,_curChId:'chapter',_pageIdx:0,_seekGeneration:0,loading:false,pg0:21,chIdx:0,chapters:[{id:'chapter',name:'测试'}],
  $:selector=>stage, esc:x=>x, preloadPaged:()=>{},syncReaderProgress:()=>{},scheduleProgressSave:()=>{},
  currentPage:()=>1, setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout:()=>{},
});
for(const name of ['readerLastPageStart','readerProgressState','mountReaderPageZones','showPage','renderPaged','flipPage','seekReaderProgress']) {
  assert.ok(extract(name),name);
  vm.runInContext(extract(name),ctx);
}
for(const count of [0,1,2,3,20,21,22,31]) {
  for(const per of [1,2]) {
    const last=ctx.readerLastPageStart(count,per);
    assert.equal(last%per,0,'last spread remains aligned');
    assert.ok(last>=0 && (!count || last<count));
    if(count) assert.equal(Math.floor(last/per)+1,Math.ceil(count/per));
  }
}
ctx.renderPaged(window._curImgs,'测试',1,'chapter');
assert.equal(ctx._pageIdx,20,'21-page double mode must reach a single-page final spread');
assert.equal(ctx.readerProgressState().current,11);
let view=stage.querySelector('.page-view');
assert.equal(view.children.filter(c=>c.classList.contains('page-img')).length,1);
let zones=view.children.filter(c=>c.classList.contains('page-zone'));
assert.equal(zones.length,2,'tap zones survive showPage clearing its DOM');
assert.equal(zones[1].disabled,true);
assert.equal(zones[0].attrs['aria-label'],'上一页');
zones[0].onclick();
assert.equal(ctx._pageIdx,18);
assert.equal(ctx.readerProgressState().current,10);
zones=view.children.filter(c=>c.classList.contains('page-zone'));
assert.equal(zones.length,2,'tap zones are rebuilt after every flip');
const beforeOldTimer=scrolls.length;
timers.filter(t=>t.ms===120).forEach(t=>t.fn());
assert.equal(scrolls.length,beforeOldTimer,'obsolete final-spread timer cannot move a newer screen');
ctx.seekReaderProgress(11);
assert.equal(ctx._pageIdx,20);
assert.equal(ctx.readerProgressState().current,11);
ctx.settings.flip='swipe';
ctx.renderPaged(window._curImgs,'测试',1,'chapter');
assert.equal(stage.querySelector('.page-view').children.filter(c=>c.classList.contains('page-zone')).length,0);
ctx.settings.flip='zone';

// Exercise the actual resize handler: preserve image 21, not spread number 11.
const resize=html.match(/let _readerResizeTimer = null;[\s\S]*?\n\}, \{passive: true\}\);/)?.[0];
assert.ok(resize);
ctx.renderChapter=(imgs,name)=>{ctx._curGen++;ctx.renderPaged(imgs,name,ctx._curGen,'chapter');};
ctx.seekReaderProgress=value=>{throw Error('paged resize must not interpret spread as image index');};
vm.runInContext(resize,ctx);
for(const width of [390,1280]) {
  window.innerWidth=width;
  listeners.resize();
  timers.filter(t=>t.ms===80).at(-1).fn();
  assert.equal(ctx._pageIdx,20);
  assert.equal(stage.querySelector('.page-view').classList.contains('double'),width>=760);
  assert.equal(ctx.readerProgressState().total,width>=760?11:21);
}
ctx.settings.mode='scroll';
listeners.resize();
ctx._seekGeneration++;
timers.filter(t=>t.ms===80).at(-1).fn(); // Must not invoke the stale scroll seek spy above.
ctx.settings.mode='double'; ctx.loading=true;
ctx.renderChapter=()=>{throw Error('pending chapter must not render previous images during resize');};
listeners.resize();
timers.filter(t=>t.ms===80).at(-1).fn();
console.log('reader paged boundaries: odd final spread, persistent tap zones, late timer and responsive regrouping passed');
