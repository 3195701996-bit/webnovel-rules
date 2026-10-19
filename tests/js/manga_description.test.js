const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let resize, frame, disconnected=0, removed=0;
const context={window:{addEventListener:(name,fn)=>{resize=fn;},removeEventListener:()=>{removed++;},
  requestAnimationFrame:fn=>{frame=fn;return 1;},cancelAnimationFrame(){}},
  ResizeObserver:class {constructor(fn){this.fn=fn;} observe(){} disconnect(){disconnected++;}}};
vm.runInNewContext(fs.readFileSync('static/js/manga_description.js','utf8'),context);
function nodes(height=200){
  const classes=new Set();
  return [{scrollHeight:height,clientHeight:80,classList:{remove:key=>classes.delete(key),
    toggle:(key,on)=>on?classes.add(key):classes.delete(key)}},
  {attributes:{},setAttribute(key,value){this.attributes[key]=value;}}];
}
const [text,button]=nodes();
const dispose=context.window.mountMangaDescription(text,button,'<img src=x>');
frame();
assert.equal(text.textContent,'<img src=x>');
assert.equal(button.hidden,false);
button.onclick();
assert.equal(button.attributes['aria-expanded'],'true');
assert.equal(button.textContent,'收起简介');
button.onclick();
assert.equal(button.attributes['aria-expanded'],'false');
text.clientHeight=200;resize();
assert.equal(button.hidden,true,'untruncated text needs no disclosure');
dispose();assert.equal(disconnected,1);assert.equal(removed,1);
const [empty,emptyButton]=nodes();
context.window.mountMangaDescription(empty,emptyButton,'   ');frame();
assert.equal(empty.hidden,true);assert.equal(emptyButton.hidden,true);
console.log('manga description: measured disclosure, resize, text safety, empty state and cleanup passed');
