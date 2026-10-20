/* Local novel cleaning: inspect first; cancellation never submits a write. */
(function (root) {
  'use strict';
  root.mountNovelCleaning = function ({button, key, request, reload}) {
    const dialog = document.createElement('dialog');
    dialog.className = 'novel-cleaning';
    dialog.setAttribute('aria-labelledby', 'novel-cleaning-title');
    const heading = document.createElement('h2');
    heading.id = 'novel-cleaning-title'; heading.textContent = '清洗已下载正文';
    const content = document.createElement('div'); content.className = 'novel-cleaning-content';
    const status = document.createElement('p'); status.setAttribute('role', 'status');
    const actions = document.createElement('div'); actions.className = 'novel-cleaning-actions';
    const cancel = document.createElement('button'); cancel.textContent = '取消'; cancel.type = 'button';
    const confirm = document.createElement('button'); confirm.textContent = '确认清洗'; confirm.type = 'button';
    actions.append(cancel, confirm); dialog.append(heading, content, status, actions); document.body.append(dialog);
    let busy = false;
    const path = '/api/books/' + encodeURIComponent(key) + '/reclean';
    const line = text => { const p = document.createElement('p'); p.textContent = text; content.append(p); };
    function close() { if (!busy) dialog.close(); }
    cancel.onclick = close;
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => button.focus());
    button.onclick = async () => {
      if (busy || dialog.open) return;
      busy = true; button.disabled = true; confirm.disabled = true; cancel.disabled = true;
      content.replaceChildren(); cancel.textContent = '取消';
      status.textContent = '正在扫描本地缓存…'; dialog.showModal();
      try {
        const data = await request(path + '?dry_run=1', {method: 'POST', timeout: 60000});
        if (data.ok !== true) throw new Error(data.error || '预览失败');
        line(`已扫描 ${data.scanned ?? data.chapters} 章，${data.changed} 章需要清洗；预计移除 ${data.removed_lines} 行、约 ${data.removed_chars} 字。`);
        for (const sample of data.samples || []) line('样例：' + sample);
        if (data.review_count) {
          line(`发现 ${data.review_count} 个疑似推广章节，仅供审查，不会自动删除：`);
          for (const item of data.review_items || []) line(`目录第${item.index}项：${item.name}`);
          if (data.review_truncated) line('最多列出100项，统计包含其余建议。');
        }
        line('确认后将改写本地正文缓存并重建TXT；章节目录和阅读进度不变。请核对样例，清洗可能存在误判。');
        if ((data.failed || []).length) line(`扫描有 ${data.failed.length} 项失败，请先核对后重试。`);
        confirm.disabled = !(data.changed > 0) || (data.failed || []).length > 0;
        status.textContent = data.changed > 0 ? '请核对后确认，取消不会修改文件。' : '未发现需要清洗的正文。';
      } catch (error) { status.textContent = '预览失败：' + error.message; }
      finally { busy = false; button.disabled = false; cancel.disabled = false; cancel.focus(); }
    };
    confirm.onclick = async () => {
      if (busy || confirm.disabled) return;
      busy = true; confirm.disabled = true; cancel.disabled = true; button.disabled = true;
      status.textContent = '正在清洗，请勿重复操作…';
      try {
        const data = await request(path, {method: 'POST', body: {dry_run: false}, timeout: 120000});
        if (data.ok !== true) throw new Error(data.error || '清洗失败');
        status.textContent = `已清洗 ${data.changed} 章；` + (data.txt_rebuilt ? 'TXT已重建。' : 'TXT未重建。');
        for (const failure of (data.failed || []).slice(0, 3)) line('失败：' + failure.reason);
        cancel.textContent = '关闭';
        try { await reload(); } catch (_) { line('详情刷新失败，请关闭后刷新页面；清洗结果以上方为准。'); }
      } catch (error) { status.textContent = '清洗请求失败，结果可能未确认，请关闭后重新预览：' + error.message; }
      finally { busy = false; cancel.disabled = false; button.disabled = false; cancel.focus(); }
    };
  };
})(window);
