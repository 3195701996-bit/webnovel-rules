/* Source-backed novel rankings. No source/category/cover data is rendered as HTML. */
(function () {
  'use strict';
  function categoriesInOrder(categories) {
    const groups = [];
    for (const category of categories) {
      const group = typeof category.group === 'string' ? category.group : '';
      if (!groups.length || groups.at(-1).name !== group) groups.push({name: group, entries: []});
      groups.at(-1).entries.push(category);
    }
    return groups;
  }
  function checkedBooks(data, source, page) {
    if (!data || data.ok === false || !Array.isArray(data.books) || data.page !== page) throw new Error('榜单响应格式异常');
    const seen = new Set();
    return data.books.filter(book => {
      if (!book || typeof book.book_url !== 'string' || !book.book_url.trim() ||
          (book.source_uid && book.source_uid !== source)) throw new Error('榜单作品身份异常');
      if (seen.has(book.book_url)) return false;
      seen.add(book.book_url); return true;
    });
  }
  window.NovelExploreData = {categoriesInOrder, checkedBooks};
  if (!document.getElementById('explore-menu')) return;
  const $ = id => document.getElementById(id);
  let sources = [], selected = null, category = null, books = [], page = 1;
  let generation = 0, controller, busy = false, ended = false, retryAction;
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  function button(text, action) {
    const item = node('button', text, 'btn'); item.type = 'button';
    item.addEventListener('click', action); return item;
  }
  function invalidate() { generation++; controller?.abort(); busy = false; }
  function status(message, retry) {
    $('explore-status').textContent = message;
    $('explore-status').dataset.kind = retry ? 'error' : 'info';
    retryAction = retry; $('explore-retry').hidden = !retry;
  }
  function remember(push) {
    const params = new URLSearchParams();
    if (selected) params.set('source', selected.uid);
    if (category) { params.set('category', category.url); params.set('page', page); }
    const url = '/explore' + (params.size ? '?' + params.toString() : '');
    if (push && location.pathname + location.search !== url) history.pushState({}, '', url);
  }
  function focusHeading() { $('explore-heading').focus({preventScroll: true}); window.scrollTo(0, 0); }
  function draw() {
    $('explore-menu').replaceChildren(); $('explore-books').replaceChildren();
    $('explore-back').hidden = !selected;
    $('explore-back').textContent = category ? '返回分类' : '返回来源';
    $('explore-heading').textContent = category ? selected.name + ' · ' + category.title : selected?.name || '小说榜单';
    $('explore-pagination').hidden = !category;
    $('explore-prev').disabled = busy || page <= 1;
    $('explore-next').disabled = busy || ended;
    $('explore-page').textContent = '第 ' + page + ' 页';
    if (!selected) {
      sources.forEach(source => $('explore-menu').append(button(source.name + ' · ' + source.categories.length + ' 个入口', () => {
        invalidate(); selected = source; category = null; books = []; page = 1;
        remember(true); status('选择一个分类或榜单。'); draw(); focusHeading();
      })));
    } else if (!category) {
      categoriesInOrder(selected.categories).forEach(group => {
        const section = node('section', undefined, 'explore-category-group');
        if (group.name) section.append(node('h2', group.name));
        const wrap = node('div', undefined, 'explore-category-buttons');
        group.entries.forEach(entry => wrap.append(button(entry.title, () => {
          invalidate(); category = entry; page = 1; books = []; ended = false;
          remember(true); draw(); focusHeading(); loadPage(1);
        })));
        section.append(wrap); $('explore-menu').append(section);
      });
    } else {
      books.forEach(book => {
        const card = node('article', undefined, 'card explore-book');
        const heading = node('h2', typeof book.name === 'string' && book.name ? book.name : '未命名作品');
        card.append(heading, node('p', typeof book.author === 'string' ? book.author : '', 'explore-note'));
        if (typeof book.last_chapter === 'string') card.append(node('p', book.last_chapter));
        const actions = node('div', undefined, 'actions');
        const detail = node('a', '查看详情', 'btn');
        detail.href = '/novel_detail?' + new URLSearchParams({source_uid: selected.uid,
          book_url: book.book_url, name: typeof book.name === 'string' ? book.name : ''});
        const sourceUid = selected.uid;
        const download = button('加入书架并下载', async () => {
          if (download.disabled || !window.confirm('加入书架并开始下载《' + heading.textContent + '》？')) return;
          download.disabled = true;
          try {
            const result = await requestJSON('/api/tasks', {method: 'POST',
              body: {source_uid: sourceUid, book_url: book.book_url}});
            if (!result || typeof result.id !== 'string' || !result.id) throw new Error('未确认任务创建，请到下载页核对后重试');
            download.textContent = '已加入下载';
            toast('已加入下载，可在下载页查看进度', 'ok');
          } catch (error) { download.disabled = false; toast(error.message || '创建下载失败，可重试', 'error'); }
        });
        actions.append(detail, download); card.append(actions); $('explore-books').append(card);
      });
    }
  }
  async function loadPage(target, pushHistory = true) {
    invalidate(); const mine = generation; controller = new AbortController();
    const source = selected, entry = category; busy = true; draw(); status('正在读取榜单…');
    try {
      const result = await requestJSON('/api/explore?' + new URLSearchParams({source: source.uid, url: entry.url, page: target}),
        {signal: controller.signal, timeout: 30000});
      if (mine !== generation) return;
      const rows = checkedBooks(result, source.uid, target);
      if (!rows.length && target > page && books.length) {
        ended = true; status('已经没有更多结果。');
      } else {
        books = rows; page = target; ended = !rows.length; remember(pushHistory);
        status(rows.length ? '本页 ' + rows.length + ' 部作品，按来源榜单顺序展示。' : '该分类本次没有返回作品。');
      }
    } catch (error) {
      if (mine === generation) status('读取榜单失败：' + error.message, () => loadPage(target, pushHistory));
    } finally { if (mine === generation) { busy = false; draw(); } }
  }
  function restore() {
    invalidate(); const params = new URLSearchParams(location.search);
    selected = sources.find(source => source.uid === params.get('source')) || null;
    category = selected?.categories.find(entry => entry.url === params.get('category')) || null;
    books = []; page = 1; ended = false;
    status(selected ? '选择一个分类或榜单。' : sources.length ? '先选择来源。' : '当前没有声明榜单规则的小说来源。');
    draw();
    if (category) {
      const target = Number(params.get('page') || 1);
      loadPage(Number.isSafeInteger(target) && target > 0 && target <= 10000 ? target : 1, false);
    }
  }
  async function loadSources() {
    invalidate(); const mine = generation; controller = new AbortController(); status('正在读取来源…');
    try {
      const data = await requestJSON('/api/explore/sources', {signal: controller.signal});
      if (mine !== generation) return;
      if (!data || data.ok === false || !Array.isArray(data.sources)) throw new Error('来源响应格式异常');
      sources = data.sources.filter(source => source && typeof source.uid === 'string' && source.uid &&
        typeof source.name === 'string' && Array.isArray(source.categories)).map(source => ({...source,
        categories: source.categories.filter(entry => entry && typeof entry.title === 'string' &&
          typeof entry.url === 'string' && entry.url.trim())})).filter(source => source.categories.length);
      restore();
    } catch (error) { if (mine === generation) status('读取来源失败：' + error.message, loadSources); }
  }
  $('explore-back').addEventListener('click', () => {
    invalidate(); if (category) category = null; else selected = null;
    books = []; page = 1; ended = false; remember(true); status('请选择榜单入口。'); draw(); focusHeading();
  });
  $('explore-retry').addEventListener('click', () => retryAction?.());
  $('explore-prev').addEventListener('click', () => { if (!busy && page > 1) loadPage(page - 1); });
  $('explore-next').addEventListener('click', () => { if (!busy && !ended) loadPage(page + 1); });
  window.addEventListener('popstate', restore);
  window.addEventListener('pagehide', invalidate);
  window.addEventListener('pageshow', event => {
    if (event.persisted) { if (sources.length) restore(); else loadSources(); }
  });
  loadSources();
})();
