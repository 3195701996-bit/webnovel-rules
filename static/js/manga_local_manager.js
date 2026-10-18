(function () {
  'use strict';
  window.mountMangaLocalManager = function (host, detail, source, comicId, reload) {
    host.replaceChildren();
    const ids = [...new Set([...(detail.downloaded || []), ...(detail.partial_downloaded || [])]
      .map(String).filter(Boolean))];
    if (!ids.length) return;
    const names = new Map([...(detail.volumes || []), ...(detail.chapters || [])]
      .map(row => [String(row.id), row.name || row.label || row.id]));
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
    let busy = false;
    function render() {
      all.textContent = selected.size === ids.length ? '取消全选' : '全选本地内容';
      remove.textContent = '删除选中（' + selected.size + '）';
      remove.disabled = busy || !selected.size;
      all.disabled = busy;
      toggle.disabled = busy;
      checks.forEach(([input, id]) => { input.checked = selected.has(id); input.disabled = busy; });
    }
    ids.forEach(rawId => {
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
    toggle.onclick = () => {
      panel.hidden = !panel.hidden;
      toggle.textContent = panel.hidden ? '管理本地内容' : '完成管理';
    };
    all.onclick = () => {
      const wasAll = selected.size === ids.length;
      selected.clear();
      if (!wasAll) ids.forEach(id => selected.add(String(id)));
      render();
    };
    remove.onclick = async () => {
      const chapterIds = [...selected];
      if (busy || !chapterIds.length || !confirm('删除选中的 ' + chapterIds.length +
          ' 个章节的本地图片？收藏和阅读历史保留。此操作不可撤销。')) return;
      busy = true;
      render();
      try {
        const response = await requestJSON('/api/manga/' + encodeURIComponent(source) + '/' +
          encodeURIComponent(comicId) + '/chapters/delete', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({chapter_ids: chapterIds}), timeout: 60000,
        });
        if (!response.ok) throw new Error(response.error || '删除未完成');
        if (response.missing > 0) {
          status.textContent = '部分内容未确认删除（' + response.missing +
            ' 项），请刷新目录核对；未自动重试。';
          return;
        }
        status.textContent = '删除完成，正在刷新本地目录';
        await reload();
      } catch (error) {
        status.textContent = '删除失败：' + error.message + '。已保留选择，可重试。';
      } finally { busy = false; render(); }
    };
    controls.append(all, remove);
    panel.append(controls, status, list);
    host.append(toggle, panel);
    render();
  };
}());
