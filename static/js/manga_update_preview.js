(function () {
  'use strict';
  window.mountMangaUpdatePreview = function (host, detail, source, comicId) {
    host.replaceChildren();
    const base = '/api/manga/' + encodeURIComponent(source) + '/' + encodeURIComponent(comicId);
    const check = document.createElement('button');
    check.className = 'op-btn';
    check.textContent = '检查更新';
    const status = document.createElement('p');
    status.setAttribute('role', 'status');
    const preview = document.createElement('details');
    preview.hidden = true;
    const summary = document.createElement('summary');
    summary.textContent = '查看待下载清单';
    const list = document.createElement('ol');
    preview.append(summary, list);
    const download = document.createElement('button');
    download.className = 'op-btn';
    download.hidden = true;
    download.disabled = true;
    let ids = [], busy = false;
    check.onclick = async () => {
      if (busy) return;
      busy = true;
      check.disabled = true;
      download.disabled = true;
      download.hidden = true;
      preview.hidden = true;
      list.replaceChildren();
      ids = [];
      status.textContent = '正在核对源站目录…';
      try {
        const result = await requestJSON(base + '/check-update', {method:'POST', timeout:60000});
        if (!result.ok) throw new Error(result.error || '检查未完成');
        const rows = Array.isArray(result.missing) ? result.missing : [];
        ids = [...new Set(rows.map(row => typeof row === 'string' ? row :
          row && (row.id || row.chapter_id)).filter(Boolean).map(String))];
        const count = Math.max(Number(result.missing_count) || 0, ids.length);
        status.textContent = (count ? '有 ' + count + ' 个章节未下载' : '当前目录均已下载') +
          (result.latest ? '（最新：' + result.latest + '）' : '');
        const diff = result.catalog_diff || {};
        if (diff.baseline_available) status.textContent += '。目录新增 ' +
          (Number(diff.added_missing_count) || 0) + ' 项，原目录待补 ' +
          (Number(diff.existing_missing_count) || 0) + ' 项';
        const warnings = [];
        if (result.catalog_warning) warnings.push(String(result.catalog_warning));
        if (diff.renamed_count > 0) warnings.push(diff.renamed_count + ' 项章节名称变更');
        if (diff.same_name_new_id_count > 0) warnings.push(diff.same_name_new_id_count + ' 项同名新身份');
        if (diff.duplicate_name_count > 0) warnings.push(diff.duplicate_name_count + ' 组同名章节');
        if (warnings.length) status.textContent += '。' + warnings.join('；') +
          '，请核对清单；不会自动合并或删除本地内容。';
        if (count && !ids.length) status.textContent += '。未取得章节清单，请重新检查；不会自动下载整本。';
        if (count > ids.length && ids.length) status.textContent +=
          '。本次清单包含 ' + ids.length + ' 项，其余内容需后续检查。';
        rows.forEach(row => {
          const item = document.createElement('li');
          item.textContent = typeof row === 'string' ? row :
            String(row && (row.name || row.id || row.chapter_id) || '未知章节');
          list.append(item);
        });
        preview.hidden = !rows.length;
        download.textContent = '下载缺失章节（' + ids.length + '）';
        download.hidden = !ids.length;
        download.disabled = !ids.length;
      } catch (error) {
        status.textContent = '检查失败：' + error.message + '。可重新检查。';
      } finally { busy = false; check.disabled = false; }
    };
    download.onclick = async () => {
      if (busy || !ids.length || !confirm('下载清单中的 ' + ids.length + ' 个章节？')) return;
      busy = true;
      check.disabled = true;
      download.disabled = true;
      try {
        const result = await requestJSON(base + '/download', {method:'POST',
          body:{title:detail.title, cover:detail.cover || '', chapters:ids.slice()}, timeout:60000});
        if (!result.ok || ['deleting', 'stopping'].includes(result.status))
          throw new Error(result.error || '任务正在变更，请稍后重试');
        status.textContent = '已提交 ' + ids.length + ' 个章节，请到下载页查看进度。';
        ids = [];
      } catch (error) {
        status.textContent = '下载未启动：' + error.message + '。清单已保留，可重试。';
      } finally {
        busy = false;
        check.disabled = false;
        download.disabled = !ids.length;
      }
    };
    host.append(check, status, preview, download);
  };
}());
