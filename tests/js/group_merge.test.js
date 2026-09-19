/* A03 前端合并纯函数离线单测（node 直接运行，无浏览器/DOM 依赖）
 *
 * 被测对象 = 线上实际运行的同一份代码 static/js/group_merge.js。
 * 覆盖搜索"新增/更新"协议的核心合并逻辑：
 *   1. 稳定分组 key 与内容指纹（指纹不变 → 前端跳过重绘）
 *   2. mergeSources：同书不同来源先后到达 → 并集、按章节数降序、新数据覆盖
 *   3. mergeGroup：晚到来源补齐后单分组包含全部来源（验收场景）；
 *      实时 merge 链结果与"一次性快照"一致（实时收尾 vs 缓存命中一致性）
 *   4. reselectSourceIndex：卡片更新后保留用户已选 source_uid
 *
 * 运行：node tests/js/group_merge.test.js
 */
'use strict';
const assert = require('node:assert');
const path = require('node:path');
const GM = require(path.join(__dirname, '..', '..', 'static', 'js', 'group_merge.js'));

// ── 1. groupKey / groupFingerprint ──
assert.strictEqual(GM.groupKey({name: '协议测试书', author: '作者甲'}), '协议测试书|作者甲');
assert.strictEqual(GM.groupKey({name: '协议测试书'}), '协议测试书|');

const gA = {
  name: '协议测试书', author: '作者甲', intro: '', cover: '',
  sources: [{source_uid: 'src_a', book_url: 'http://a/1', chapter_count: 100,
             source_name: '源A', last_chapter: '第100章', update_time: '', word_count: ''}],
};
// 同内容（哪怕不同对象/数组实例）指纹一致 → 前端据此跳过无变化重绘
assert.strictEqual(
  GM.groupFingerprint(gA),
  GM.groupFingerprint({name: '协议测试书', author: '作者甲', intro: '', cover: '',
    sources: [{source_uid: 'src_a', book_url: 'http://a/1', chapter_count: 100,
               source_name: '源A', last_chapter: '第100章', update_time: '', word_count: ''}]}));

// ── 2. mergeSources：并集 + 章节数降序 + 新数据覆盖 ──
const srcB = {source_uid: 'src_b', book_url: 'http://b/9', chapter_count: 250,
              source_name: '源B'};
const srcA2 = {source_uid: 'src_a', book_url: 'http://a/1', chapter_count: 120,
               source_name: '源A', last_chapter: '第120章'};   // 同源更新元数据
const mergedSrcs = GM.mergeSources(gA.sources, [srcA2, srcB]);
assert.deepStrictEqual(mergedSrcs.map(s => s.source_uid), ['src_b', 'src_a'],
  '来源并集按章节数降序');
assert.strictEqual(mergedSrcs[1].chapter_count, 120, '同 source_uid|book_url 新数据覆盖旧元数据');
// 同 uid 不同 book_url 是两本不同的书，不得合并丢失
const twoUrls = GM.mergeSources(
  [{source_uid: 's', book_url: 'http://s/1', chapter_count: 10}],
  [{source_uid: 's', book_url: 'http://s/2', chapter_count: 20}]);
assert.strictEqual(twoUrls.length, 2, '同 uid 不同 url 不得折叠');

// ── 3. mergeGroup：验收场景——同书来源先后到达，最终一个分组包含全部来源 ──
const gB = {   // 服务端重建的全量组（晚到事件）：含 A+B 两来源
  name: '协议测试书', author: '作者甲', intro: '', cover: '',
  sources: [srcB, gA.sources[0]],
};
const card = GM.mergeGroup(gA, gB);
assert.strictEqual(GM.groupKey(card), '协议测试书|作者甲');
assert.deepStrictEqual(card.sources.map(s => s.source_uid), ['src_b', 'src_a'],
  '同书晚到来源并入同一分组（一个卡片包含全部来源）');

// 实时 merge 链（先 A 后 update B）与一次性快照（直接 B 全量）结果一致
// —— 对应"实时收尾与缓存命中的分组数据一致"
assert.deepStrictEqual(
  GM.mergeGroup(GM.mergeGroup(null, gA), gB).sources,
  GM.mergeGroup(null, gB).sources);

// 静默刷新：实时部分结果不含缓存旧来源时，merge 后旧来源不丢失
const cached = {name: '协议测试书', author: '作者甲', intro: '旧简介', cover: 'http://c/1.jpg',
  sources: [{source_uid: 'src_old', book_url: 'http://old/1', chapter_count: 80}]};
const freshPartial = {name: '协议测试书', author: '作者甲', intro: '', cover: '',
  sources: [{source_uid: 'src_a', book_url: 'http://a/1', chapter_count: 100}]};
const silentMerged = GM.mergeGroup(cached, freshPartial);
assert.deepStrictEqual(silentMerged.sources.map(s => s.source_uid).sort(),
  ['src_a', 'src_old'], '缓存旧来源不被实时部分结果冲掉');
assert.strictEqual(silentMerged.intro, '旧简介', '新数据缺简介时保留旧简介');
assert.strictEqual(silentMerged.cover, 'http://c/1.jpg', '新数据缺封面时保留旧封面');

// 指纹随来源补齐变化（服务端判"变化需重发"、前端判"变化需重绘"的共同依据）
assert.notStrictEqual(GM.groupFingerprint(gA), GM.groupFingerprint(gB),
  '来源补齐后指纹必须变化');

// ── 4. reselectSourceIndex：卡片更新保留用户已选 source_uid ──
const selIdx = GM.reselectSourceIndex(card.sources, 'src_a', 0);
assert.strictEqual(selIdx, 1, 'src_a 排序后在 index 1，用户选择被保留');
assert.strictEqual(GM.reselectSourceIndex(card.sources, 'gone_uid', 0), 0,
  '已选来源消失时回退主源');
assert.strictEqual(GM.reselectSourceIndex([], 'x', -1), 0, '空来源列表安全回退');

console.log('group_merge.test.js: 全部断言通过');
