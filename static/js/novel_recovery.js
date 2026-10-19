(function (root) {
  'use strict';
  root.mountNovelRecovery = function ({button, key, request, reload}) {
    const dialog = document.createElement('dialog'); dialog.className = 'novel-cleaning';
    const title = document.createElement('h2'); title.id = 'novel-recovery-title'; title.textContent = '恢复清洗前原文';
    dialog.setAttribute('aria-labelledby', title.id);
    const note = document.createElement('p'); note.textContent = '恢复会带回原有广告；后续下载或修改过的内容不会被覆盖。旧版本未存档的原文无法追溯。';
    const status = document.createElement('p'); status.setAttribute('role', 'status');
    const list = document.createElement('div'); list.className = 'novel-cleaning-content novel-recovery-list';
    const actions = document.createElement('div'); actions.className = 'novel-cleaning-actions';
    function makeButton(text) { const b = document.createElement('button'); b.type = 'button'; b.textContent = text; return b; }
    const refresh = makeButton('刷新存档'); const all = makeButton('全选 / 取消全选（最多500项）');
    const close = makeButton('关闭'); const confirm = makeButton('预览恢复');
    const tools = document.createElement('div'); tools.className = 'novel-cleaning-actions'; tools.append(refresh, all);
    actions.append(close, confirm); dialog.append(title, note, tools, list, status, actions); document.body.append(dialog);
    const base = '/api/books/' + encodeURIComponent(key) + '/reclean';
    let busy = false, reviewed = false, rows = []; const selected = new Set();
    const checks = [];
    function sync() {
      button.disabled = busy; refresh.disabled = busy; close.disabled = busy;
      all.disabled = busy || !rows.some(r => r.restorable === true);
      confirm.disabled = busy || selected.size === 0;
      confirm.textContent = busy ? '处理中…' : reviewed ? '确认恢复' : `预览恢复（${selected.size}）`;
      for (const {input, row} of checks) { input.disabled = busy || row.restorable !== true; input.checked = selected.has(row.id); }
    }
    function render() {
      list.replaceChildren(); checks.length = 0;
      for (const row of rows) {
        const label = document.createElement('label'); label.className = 'novel-recovery-row';
        const input = document.createElement('input'); input.type = 'checkbox';
        const text = document.createElement('span');
        const state = row.restorable === true ? '可恢复' : row.already_original === true ? '已经是原文' : '内容变化或存档异常';
        text.textContent = `${row.label || '无法关联目录的存档'} · ${state} · ${row.id.slice(0, 8)}`;
        input.onchange = () => {
          if (busy || row.restorable !== true) return;
          if (!input.checked) selected.delete(row.id);
          else if (selected.size < 500) selected.add(row.id);
          reviewed = false; sync();
        };
        checks.push({input, row}); label.append(input, text); list.append(label);
      }
      sync();
    }
    async function load() {
      const data = await request(base + '/recovery', {timeout: 60000});
      if (data.ok !== true || !Array.isArray(data.items) || data.count !== data.items.length ||
          data.items.some(r => !r || typeof r.id !== 'string' || !/^[0-9a-f]{64}$/.test(r.id) || typeof r.restorable !== 'boolean') ||
          new Set(data.items.map(r => r.id)).size !== data.items.length) {
        throw new Error('存档列表或身份无效');
      }
      rows = data.items; selected.clear(); reviewed = false; render();
    }
    async function refreshList() {
      busy = true; sync(); status.textContent = '正在读取存档…';
      try { await load(); status.textContent = rows.length ? '请选择存档后预览。' : '没有原文存档。'; }
      catch (error) { rows = []; selected.clear(); reviewed = false; render(); status.textContent = '读取失败：' + error.message; }
      finally { busy = false; sync(); }
    }
    button.onclick = async () => {
      if (busy || dialog.open) return;
      dialog.showModal(); await refreshList(); close.focus();
    };
    refresh.onclick = refreshList;
    close.onclick = () => { if (!busy) dialog.close(); };
    dialog.addEventListener('close', () => button.focus());
    dialog.addEventListener('cancel', e => { if (busy) e.preventDefault(); });
    all.onclick = () => {
      if (busy) return;
      const eligible = rows.filter(r => r.restorable === true).slice(0, 500).map(r => r.id);
      if (eligible.every(id => selected.has(id))) selected.clear();
      else { selected.clear(); eligible.forEach(id => selected.add(id)); }
      reviewed = false; sync();
    };
    confirm.onclick = async () => {
      if (busy || !selected.size) return;
      const ids = [...selected]; const dry = !reviewed;
      busy = true; sync(); status.textContent = dry ? '正在预览恢复…' : '正在恢复原文…';
      try {
        const data = await request(base + '/restore', {method: 'POST', body: {ids, dry_run: dry}, timeout: 120000});
        if (data.ok !== true || data.dry_run !== dry || !Array.isArray(data.items) || data.items.length !== ids.length ||
            new Set(data.items.map(r => r.id)).size !== ids.length || data.items.some(r => !ids.includes(r.id))) {
          throw new Error('恢复响应身份不一致');
        }
        const failed = data.items.filter(r => r.ok !== true);
        reviewed = dry && failed.length === 0;
        const failures = failed.length ? ' 部分存档无法恢复：' + failed.slice(0, 3).map(r => r.error || '未知错误').join('；') : '';
        status.textContent = dry ? (failed.length ? failures.trim() : `预览通过，将恢复 ${ids.length} 项；请再次确认。`) :
          `已恢复 ${data.restored} 项；` + (data.txt_rebuilt ? 'TXT已重建。' : 'TXT未重建：' + (data.export_error || '没有新增恢复项')) + failures;
        if (!dry) {
          selected.clear(); reviewed = false;
          try { await load(); await reload(); } catch (_) { status.textContent += ' 存档或详情刷新失败，请重新打开核对。'; }
        }
      } catch (error) {
        reviewed = false; status.textContent = '结果未确认，请刷新存档后重试：' + error.message;
      } finally { busy = false; sync(); }
    };
    sync();
  };
})(window);
