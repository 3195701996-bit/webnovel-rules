(function () {
  'use strict';
  window.mountMangaLocalManager = function (host, detail, source, comicId, reload) {
    host.replaceChildren();
    let ids = [];
    let names = new Map();
    let searchable = new Map();
    let titles = new Map();
    let visibleIds = [];
    const base = '/api/manga/' + encodeURIComponent(source) + '/' + encodeURIComponent(comicId);
    const selected = new Set();
    const toggle = document.createElement('button');
    toggle.className = 'op-btn';
    toggle.textContent = '管理本地内容';
    const panel = document.createElement('div');
    panel.hidden = true;
    const controls = document.createElement('div');
    controls.className = 'ops-row';
    const all = document.createElement('button');
    all.className = 'op-btn';
    const remove = document.createElement('button');
    remove.className = 'op-btn';
    const status = document.createElement('p');
    status.setAttribute('role', 'status');
    const list = document.createElement('div');
    list.className = 'chap-list';
    const checks = [];
    const search = document.createElement('input');
    search.type = 'search';
    search.placeholder = '筛选标题；查 ID 用 id:编号';
    search.setAttribute('aria-label', '筛选本地缓存标题或 ID');
    search.className = 'local-media-search';
    search.value = '';
    const summary = document.createElement('p');
    summary.setAttribute('aria-live', 'polite');
    let busy = false;
    let loading = false;
    let failed = false;
    const retry = document.createElement('button');
    retry.className = 'op-btn';
    retry.textContent = '重新读取本地缓存';
    async function refresh() {
      if (loading) return;
      loading = true;
      failed = false;
      status.textContent = '正在读取本地缓存…';
      render();
      try {
        const data = await requestJSON(base + '/local-media');
        const seen = new Set();
        if (data.ok !== true || data.local_only !== true || data.source !== source ||
            data.comic_id !== comicId || !Array.isArray(data.media) || data.count !== data.media.length ||
            !data.media.every(row => row && typeof row.id === 'string' && row.id.trim() &&
              typeof row.name === 'string' && typeof row.downloaded === 'boolean' &&
              typeof row.partial === 'boolean' && row.downloaded !== row.partial &&
              !seen.has(row.id) && seen.add(row.id))) throw new Error('本地缓存清单格式不正确');
        ids = data.media.map(row => row.id);
        titles = new Map(data.media.map(row => [row.id, row.name.normalize('NFKC').toLowerCase()]));
        searchable = new Map(data.media.map(row => [row.id, (row.name + ' ' + row.id).normalize('NFKC').toLowerCase()]));
        const counts = new Map();
        data.media.forEach(row => counts.set(row.name, (counts.get(row.name) || 0) + 1));
        names = new Map(data.media.map(row => [row.id, (row.name || row.id) +
          (row.partial ? ' · 部分缓存' : ' · 完整缓存') +
          (counts.get(row.name) > 1 ? ' · ID: ' + row.id : '')]));
        for (const id of selected) if (!seen.has(id)) selected.delete(id);
        buildList();
        status.textContent = ids.length ? '共 ' + ids.length + ' 项本地缓存' : '没有可清理的本地缓存';
      } catch (error) {
        failed = true;
        status.textContent = '读取失败：' + error.message + '。未使用在线目录代替。';
      } finally { loading = false; render(); }
    }
    function render() {
      const filtering = search.value.trim().length > 0;
      all.textContent = visibleIds.length && visibleIds.every(id => selected.has(id)) ? '取消全选' :
        filtering ? '全选筛选结果' : '全选本地内容';
      remove.textContent = '删除选中（' + selected.size + '）';
      remove.disabled = busy || loading || failed || !selected.size;
      all.disabled = busy || loading || failed || !visibleIds.length;
      search.hidden = ids.length <= 10 && !search.value;
      search.disabled = busy || loading || failed;
      summary.hidden = !filtering;
      const visibleSet = new Set(visibleIds);
      summary.textContent = '匹配 ' + visibleIds.length + ' 项 · 筛选外已选 ' +
        [...selected].filter(id => !visibleSet.has(id)).length + ' 项';
      toggle.disabled = busy || loading;
      retry.hidden = !failed;
      retry.disabled = busy || loading;
      checks.forEach(([input, id]) => { input.checked = selected.has(id); input.disabled = busy || loading || failed; });
    }
    function buildList() {
      checks.length = 0;
      list.replaceChildren();
      const terms = search.value.normalize('NFKC').trim().toLowerCase().split(/\s+/).filter(Boolean);
      visibleIds = ids.filter(id => terms.every(term => {
        if (term.startsWith('id:')) return term.length > 3 && id.normalize('NFKC').toLowerCase().includes(term.slice(3));
        if (/^[0-9]+(?:\.[0-9]+)?$/.test(term)) return titles.get(id).includes(term);
        return searchable.get(id).includes(term);
      }));
      visibleIds.forEach(rawId => {
      const id = String(rawId);
      const label = document.createElement('label');
      label.className = 'chap-item';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.addEventListener('change', () => {
        if (input.checked) selected.add(id); else selected.delete(id);
        render();
      });
      label.append(input, document.createTextNode(' ' + (names.get(id) || id)));
      list.append(label);
      checks.push([input, id]);
      });
      if (ids.length && !visibleIds.length) {
        const empty = document.createElement('p');
        empty.textContent = '没有匹配的本地内容，可修改筛选词';
        list.append(empty);
      }
    }
    search.addEventListener('input', () => {
      if (search.disabled) return;
      buildList();
      render();
    });
    toggle.onclick = () => {
      if (busy || loading) return;
      panel.hidden = !panel.hidden;
      toggle.textContent = panel.hidden ? '管理本地内容' : '完成管理';
      if (!panel.hidden) return refresh();
    };
    retry.onclick = refresh;
    all.onclick = () => {
      if (all.disabled) return;
      const wasAll = visibleIds.every(id => selected.has(id));
      visibleIds.forEach(id => { if (wasAll) selected.delete(id); else selected.add(id); });
      render();
    };
    remove.onclick = async () => {
      const chapterIds = [...selected];
      const visibleSet = new Set(visibleIds);
      const hiddenCount = chapterIds.filter(id => !visibleSet.has(id)).length;
      if (busy || loading || failed || !chapterIds.length || !confirm('删除选中的 ' + chapterIds.length +
          ' 个章节的本地图片？' + (hiddenCount ? '其中 ' + hiddenCount + ' 项位于当前筛选之外。' : '') +
          '收藏和阅读历史保留。此操作不可撤销。')) return;
      busy = true;
      render();
      try {
        const response = await requestJSON(base + '/chapters/delete', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({chapter_ids: chapterIds, identity_mode: 'disk'}), timeout: 60000,
        });
        if (response.ok !== true) throw new Error(response.error || '删除未完成');
        if (response.missing > 0) {
          await refresh();
          status.textContent = '部分内容未确认删除（' + response.missing +
            ' 项），已重新核对本地缓存；未自动重试。' +
            (failed ? '缓存清单读取失败，请重新读取后操作。' : '');
          return;
        }
        selected.clear();
        await refresh();
        status.textContent = '删除完成。' + (failed ? '缓存清单读取失败，请重新读取后操作。' : '');
        try { await reload(); }
        catch (error) { status.textContent += '详情刷新失败：' + error.message + '。请重新打开详情，不需要重复删除。'; }
      } catch (error) {
        await refresh();
        status.textContent = '删除未完成：' + error.message + '。已保留仍存在的选择，可重试。' +
          (failed ? '缓存清单读取失败，请重新读取后操作。' : '');
      } finally { busy = false; render(); }
    };
    controls.append(all, remove);
    panel.append(search, summary, controls, status, retry, list);
    host.append(toggle, panel);
    render();
  };
}());
