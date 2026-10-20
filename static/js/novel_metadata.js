/* Display metadata repair is previewed and revision-bound; catalogs are untouched. */
(function (root) {
  'use strict';
  root.mountNovelMetadata = function ({button, key, request, reload}) {
    const dialog = document.createElement('dialog');
    dialog.className = 'novel-cleaning';
    dialog.setAttribute('aria-labelledby', 'novel-metadata-title');
    const title = document.createElement('h2');
    title.id = 'novel-metadata-title'; title.textContent = '刷新书名与作者';
    const content = document.createElement('div'); content.className = 'novel-cleaning-content';
    const status = document.createElement('p'); status.setAttribute('role', 'status');
    const actions = document.createElement('div'); actions.className = 'novel-cleaning-actions';
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = '取消';
    const confirm = document.createElement('button'); confirm.type = 'button';
    actions.append(cancel, confirm); dialog.append(title, content, status, actions); document.body.append(dialog);
    const path = '/api/books/' + encodeURIComponent(key) + '/metadata';
    let busy = false, approval = null;
    function reset() { approval = null; confirm.textContent = '读取预览'; }
    function line(value) { const p = document.createElement('p'); p.textContent = value; content.append(p); }
    button.onclick = () => {
      if (busy || dialog.open) return;
      reset(); content.replaceChildren();
      line('从原书源读取书名和作者，确认后才保存。不修改章节目录、正文和阅读进度。');
      status.textContent = '取消不会修改本地文件。'; cancel.textContent = '取消';
      confirm.disabled = false; dialog.showModal(); cancel.focus();
    };
    cancel.onclick = () => { if (!busy) dialog.close(); };
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => { reset(); button.focus(); });
    confirm.onclick = async () => {
      if (busy || confirm.disabled) return;
      const saving = approval;
      let canContinue = true;
      busy = true; button.disabled = true; confirm.disabled = true; cancel.disabled = true;
      status.textContent = saving ? '正在保存…' : '正在从原书源读取预览…';
      try {
        if (saving) {
          const result = await request(path, {method: 'POST', body: {
            state_revision: saving.revision, metadata: saving.fields
          }});
          if (result.ok !== true || result.catalog_preserved !== true) throw new Error(result.error || '未确认保存结果');
          reset(); status.textContent = '书名与作者已保存，章节和进度未修改。';
          cancel.textContent = '关闭';
          try { await reload(); } catch (_) { line('详情刷新失败，请关闭后刷新页面；修改已经保存。'); }
        } else {
          reset(); content.replaceChildren();
          const data = await request(path + '/preview', {timeout: 20000});
          const fields = data.proposed;
          if (data.ok !== true || data.read_only !== true || !/^[a-f0-9]{64}$/.test(data.state_revision || '')
              || !fields || typeof fields !== 'object' || Array.isArray(fields)
              || !Object.keys(fields).length || Object.keys(fields).some(k => !['name', 'author'].includes(k))
              || Object.values(fields).some(v => typeof v !== 'string' || !v.trim() || v.length > 300)
              || !fields.name || !Array.isArray(data.changes)
              || data.changes.some(c => !c || !['name', 'author'].includes(c.field)
                || typeof c.before !== 'string' || c.after !== fields[c.field])) {
            throw new Error(data.error || '预览数据异常，未允许保存');
          }
          for (const change of data.changes) line((change.field === 'name' ? '书名' : '作者') + '：' + change.before + ' → ' + change.after);
          if (data.changes.length) {
            approval = {revision: data.state_revision, fields: {...fields}};
            confirm.textContent = '确认修改'; status.textContent = '请核对上述修改。取消不会保存。';
          } else { status.textContent = '书名与作者已是最新，无需修改。'; canContinue = false; }
        }
      } catch (error) {
        reset(); confirm.disabled = false;
        status.textContent = (saving ? '保存结果未确认，请重新预览：' : '预览失败：') + error.message;
      } finally {
        busy = false; button.disabled = false; cancel.disabled = false;
        confirm.disabled = !canContinue;
        cancel.focus();
      }
    };
  };
})(window);
