/* Category discovery and pagination share the production API with Android. */
(function () {
  'use strict';
  const state = { sources: [], source: '', category: '', page: 0, rows: [], exhausted: false };
  let serial = 0, loading = false, controller;
  const el = id => document.getElementById(id);
  const storageKey = 'manga_browse_v203';
  function save() {
    try { sessionStorage.setItem(storageKey, JSON.stringify(state)); } catch (_) { /* storage may be full */ }
  }
  function status(message) { el('browse-status').textContent = message; }
  function cancel() { serial++; controller?.abort(); loading = false; }
  function button(label, action, className = 'btn') {
    const b = document.createElement('button');
    b.type = 'button'; b.className = className; b.textContent = label;
    b.addEventListener('click', action); return b;
  }
  function render() {
    const source = state.sources.find(s => s.key === state.source);
    const tabs = el('browse-sources'); tabs.replaceChildren();
    for (const s of state.sources) {
      const tab = button(s.name, () => {
        cancel(); state.source = s.key; state.category = ''; state.rows = []; state.page = 0;
        state.exhausted = false; save(); render(); status('');
      }, s.key === state.source ? 'btn primary' : 'btn');
      tab.setAttribute('aria-pressed', String(s.key === state.source)); tabs.append(tab);
    }
    const categories = el('browse-categories'); categories.replaceChildren();
    const results = el('browse-results'); results.replaceChildren();
    if (!source) return;
    const title = document.createElement('h2'); title.textContent = source.name; categories.append(title);
    if (!state.category) {
      const groups = new Map();
      for (const c of source.categories || []) {
        const group = c.group || '分类';
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push(c);
      }
      for (const [group, items] of groups) {
        const section = document.createElement('section');
        const heading = document.createElement('h3'); heading.textContent = group;
        const grid = document.createElement('div'); grid.className = 'browse-category-buttons';
        for (const c of items) grid.append(button(c.name, () => {
          cancel(); state.category = c.key; state.rows = []; state.page = 0; state.exhausted = false;
          save(); render(); loadPage();
        }));
        section.append(heading, grid); categories.append(section);
      }
    } else {
      categories.append(button('返回分类', () => {
        cancel(); state.category = ''; save(); render(); status('');
      }));
      const heading = document.createElement('h3');
      heading.textContent = source.categories.find(c => c.key === state.category)?.name || '分类结果';
      categories.append(heading);
      for (const row of state.rows) results.append(makeMangaCard(row));
    }
    el('browse-more').hidden = !state.category || state.exhausted;
    el('browse-more').disabled = loading;
  }
  async function loadPage() {
    if (loading || !state.category || state.exhausted) return;
    loading = true; const run = ++serial;
    controller = new AbortController(); render(); status('正在加载分类…');
    const next = state.page + 1;
    try {
      const params = new URLSearchParams({ source: state.source, category: state.category, page: next });
      const response = await fetch('/api/manga/browse?' + params, { signal: controller.signal });
      const data = await response.json();
      if (run !== serial) return;
      if (!response.ok || !Array.isArray(data.results)) throw new Error(data.error || '分类加载失败');
      const seen = new Set(state.rows.map(c => c.source + ':' + c.id));
      for (const row of data.results) {
        const key = row.source + ':' + row.id;
        if (!seen.has(key)) { state.rows.push(row); seen.add(key); }
      }
      state.page = next; state.exhausted = data.results.length === 0; save();
      status(state.rows.length ? `${state.rows.length} 部 · 第 ${state.page} 页${state.exhausted ? ' · 已到末页' : ''}` : '该分类暂无漫画');
    } catch (error) {
      if (run !== serial || error.name === 'AbortError') return;
      status('加载失败：' + (error.message || '网络错误') + '，可点击加载更多重试。');
    } finally {
      if (run === serial) { loading = false; render(); }
    }
  }
  let initialized = false;
  async function open() {
    if (initialized) { render(); return; }
    status('正在加载漫画源…');
    try {
      const response = await fetch('/api/explore/sources');
      const data = await response.json();
      if (!response.ok || !Array.isArray(data.manga?.sources)) throw new Error(data.error || '分类源加载失败');
      state.sources = data.manga.sources;
      try {
        const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
        const source = state.sources.find(s => s.key === saved?.source);
        if (source) {
          state.source = source.key;
          if (source.categories.some(c => c.key === saved.category) && Array.isArray(saved.rows)) {
            state.category = saved.category; state.page = Number(saved.page) || 0;
            state.rows = saved.rows; state.exhausted = saved.exhausted === true;
          }
        }
      } catch (_) { /* corrupt storage must not prevent browsing */ }
      state.source ||= state.sources[0]?.key || ''; initialized = true; render();
      status(state.sources.length ? (state.category ? `${state.rows.length} 部 · 第 ${state.page} 页` : '') : '当前没有可浏览的漫画分类');
    } catch (error) {
      status('分类加载失败：' + error.message);
      el('browse-categories').replaceChildren(button('重试', open));
    }
  }
  el('browse-more').addEventListener('click', loadPage);
  window.MangaBrowse = { open };
})();
