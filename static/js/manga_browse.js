/* Category discovery and pagination share the production API with Android. */
(function () {
  'use strict';
  const state = { sources: [], source: '', category: '', page: 0, rows: [], exhausted: false, filters: {} };
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
          state.filters = { ordering: new URLSearchParams(c.key).get('ordering') || '-datetime_updated', region: '', status: '' };
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
      if (state.source === 'copymanga') {
        const groups = [
          ['region', [['', '全部地区'], ['0', '日漫'], ['1', '韩漫'], ['2', '美漫']]],
          ['status', [['', '全部状态'], ['0', '连载中'], ['1', '已完结'], ['2', '短篇']]],
          ['ordering', [['-datetime_updated', '时间倒序'], ['datetime_updated', '时间正序'], ['-popular', '热度倒序'], ['popular', '热度正序']]],
        ];
        for (const [key, options] of groups) {
          const grid = document.createElement('div'); grid.className = 'browse-category-buttons';
          for (const [value, label] of options) {
            const active = (state.filters[key] || '') === value;
            const control = button(label, () => {
              cancel(); state.filters[key] = value; state.rows = []; state.page = 0; state.exhausted = false;
              save(); render(); loadPage();
            }, active ? 'btn primary' : 'btn');
            control.setAttribute('aria-pressed', String(active)); grid.append(control);
          }
          categories.append(grid);
        }
      }
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
      if (state.source === 'copymanga') for (const [key, value] of Object.entries(state.filters)) params.set(key, value);
      const response = await fetch('/api/manga/browse?' + params, { signal: controller.signal });
      const data = await response.json();
      if (run !== serial) return;
      if (!response.ok || !Array.isArray(data.results)) throw new Error(data.error || '分类加载失败');
      const seen = new Set(state.rows.map(c => c.source + ':' + c.id));
      for (const row of data.results) {
        const key = row.source + ':' + row.id;
        if (!seen.has(key)) { state.rows.push(row); seen.add(key); }
      }
      state.page = next; state.exhausted = data.has_more === false || data.results.length === 0; save();
      status(state.rows.length ? `${state.rows.length} 部 · 第 ${state.page}${data.total_pages ? ' / ' + data.total_pages : ''} 页${state.exhausted ? ' · 已到末页' : ''}` : '该分类暂无漫画');
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
            state.filters = saved.filters || { ordering: new URLSearchParams(saved.category).get('ordering') || '-datetime_updated', region: '', status: '' };
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
