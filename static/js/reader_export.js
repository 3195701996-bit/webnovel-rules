(function (root) {
  'use strict';
  root.mountReaderExport = function ({button, key, request, name}) {
    const dialog = document.createElement('dialog'); dialog.className = 'novel-cleaning';
    const title = document.createElement('h2'); title.id = 'reader-export-title'; title.textContent = '导出阅读版 TXT';
    dialog.setAttribute('aria-labelledby', title.id);
    const note = document.createElement('p'); note.textContent = '连续章节编号、标准标题；只生成导出副本，不修改缓存、原始TXT或阅读记录。';
    const controls = document.createElement('div');
    function option(text, checked) {
      const label = document.createElement('label'); label.className = 'novel-recovery-row';
      const input = document.createElement('input'); input.type = 'checkbox'; input.checked = checked;
      const span = document.createElement('span'); span.textContent = text; label.append(input, span); controls.append(label); return input;
    }
    const clean = option('去除正文中的已知广告', true);
    const omit = option('排除明确推广独立页（仅影响本次导出）', false);
    const content = document.createElement('div'); content.className = 'novel-cleaning-content';
    const status = document.createElement('p'); status.setAttribute('role', 'status');
    const actions = document.createElement('div'); actions.className = 'novel-cleaning-actions';
    function makeButton(text) { const b = document.createElement('button'); b.type = 'button'; b.textContent = text; return b; }
    const close = makeButton('取消'), preview = makeButton('生成预览'), save = makeButton('保存阅读版');
    actions.append(close, preview, save); dialog.append(title, note, controls, content, status, actions); document.body.append(dialog);
    const base = '/api/books/' + encodeURIComponent(key) + '/txt';
    let busy = false, report = null;
    const options = () => `clean=${clean.checked ? 1 : 0}&omit_promotions=${omit.checked ? 1 : 0}`;
    const line = text => { const p = document.createElement('p'); p.textContent = text; content.append(p); };
    function sync() {
      for (const b of [button, close, preview, clean, omit]) b.disabled = busy;
      save.disabled = busy || !report;
    }
    function invalidate() { report = null; content.replaceChildren(); status.textContent = '选项已变化，请重新预览。'; sync(); }
    clean.onchange = omit.onchange = invalidate;
    button.onclick = () => {
      if (busy || dialog.open) return;
      report = null; content.replaceChildren(); status.textContent = '先核对预览，再保存文件。'; sync(); dialog.showModal(); close.focus();
    };
    close.onclick = () => { if (!busy) dialog.close(); };
    dialog.addEventListener('close', () => button.focus());
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    preview.onclick = async () => {
      if (busy) return;
      busy = true; report = null; content.replaceChildren(); status.textContent = '正在生成本地预览…'; sync();
      try {
        const data = await request(base + '/preview?' + options(), {timeout: 120000});
        if (data.ok !== true || data.clean !== clean.checked || data.omit_promotions !== omit.checked ||
            typeof data.revision !== 'string' || !/^[0-9a-f]{64}$/.test(data.revision)) throw new Error('预览版本或选项无效');
        for (const [count, rows] of [['exported_count', 'chapters'], ['omitted_count', 'omitted'], ['missing_count', 'missing']]) {
          if (!Number.isInteger(data[count]) || data[count] < 0 || !Array.isArray(data[rows]) || data[rows].length !== data[count]) throw new Error('预览数量不一致');
        }
        if (!data.exported_count) throw new Error('没有可导出的本地正文');
        report = data;
        line(`导出 ${data.exported_count} 章 · 排除 ${data.omitted_count} 项 · 本地缺失 ${data.missing_count} 章`);
        if (data.missing_count) line('这是部分导出；缺失章节不会补下载。');
        for (const item of data.omitted.slice(0, 100)) line(`排除目录第${item.source_index}项：${item.name}`);
        for (const item of data.chapters.slice(0, 20)) line(`${item.heading}（原目录第${item.source_index}项）`);
        line('最多展示100个排除项及20个标题；上述计数包含全部章节。');
        status.textContent = '预览已就绪；正文或选项变化时需重新预览。';
      } catch (error) { report = null; status.textContent = '预览失败：' + error.message; }
      finally { busy = false; sync(); }
    };
    save.onclick = async () => {
      if (busy || !report) return;
      const path = `${base}?layout=reader&${options()}&revision=${report.revision}`;
      const filename = (String(name() || 'book').replace(/[\\/:*?"<>|\r\n\x00]/g, '_').trim().slice(0, 80) || 'book') + '-阅读版.txt';
      busy = true; sync(); status.textContent = '正在保存阅读版…';
      let timer;
      try {
        // 支持文件系统API时直接流式写入；不支持时使用浏览器Blob下载。
        const handle = typeof root.showSaveFilePicker === 'function' ? await root.showSaveFilePicker({suggestedName: filename,
          types: [{description: 'TXT 文本', accept: {'text/plain': ['.txt']}}]}) : null;
        const controller = new AbortController(); timer = setTimeout(() => controller.abort(), 120000);
        const response = await root.fetch(path, {signal: controller.signal});
        if (!response.ok) {
          if (response.status === 409) { report = null; content.replaceChildren(); }
          let message = `HTTP ${response.status}`;
          try { message += ' · ' + (await response.json()).error; } catch (_) {}
          throw new Error(message);
        }
        if (!(response.headers.get('Content-Type') || '').toLowerCase().startsWith('text/plain')) throw new Error('响应不是TXT，已停止保存');
        if (handle) {
          if (!response.body) throw new Error('没有可读取的正文流');
          await response.body.pipeTo(await handle.createWritable());
          status.textContent = '阅读版已保存。';
        } else {
          const blob = await response.blob(); const url = URL.createObjectURL(blob);
          const link = document.createElement('a'); link.href = url; link.download = filename;
          document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
          status.textContent = '已交给浏览器下载，请在下载列表确认完成。';
        }
      } catch (error) { status.textContent = error.name === 'AbortError' ? '保存已取消或超时，原缓存不受影响。' : '保存失败：' + error.message; }
      finally { if (timer) clearTimeout(timer); busy = false; sync(); }
    };
    sync();
  };
})(window);
