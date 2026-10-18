(function () {
  'use strict';
  const byId = id => document.getElementById(id);
  let refreshing = false;
  let generating = false;
  let report = '';
  function status(id, text, error) {
    const node = byId(id);
    node.textContent = text;
    node.dataset.error = error ? 'true' : 'false';
  }
  function errorText(error) { return error?.message || '请求失败，请重试'; }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    byId('diagnostics-refresh').disabled = true;
    status('engine-status', '正在读取引擎状态…');
    status('events-status', '正在读取最近记录…');
    byId('engine-summary').textContent = '';
    byId('events-summary').textContent = '';
    try {
      await Promise.all([
        (async () => {
          try {
            const data = await requestJSON('/api/health');
            if (data.ok !== true) throw new Error('引擎状态响应异常');
            const runtime = data.runtime || {};
            const names = {ready:'就绪', new:'未启动', starting:'启动中', stopped:'已停止', stopping:'停止中', failed:'异常'};
            status('engine-status', '接口可访问 · 业务运行时：' + (names[runtime.state] || runtime.state || '未知'));
            byId('engine-summary').textContent = [
              '服务时间：' + (data.server_time || '未知'),
              '注册漫画源：' + (Array.isArray(data.manga_sources) ? data.manga_sources.length : '未知'),
              '注册源数量不代表真实网络验证结果。',
            ].join('\n');
          } catch (error) { status('engine-status', errorText(error), true); }
        })(),
        (async () => {
          try {
            const data = await requestJSON('/api/diagnostics/events');
            if (data.ok !== true || !Array.isArray(data.failed_requests) || !Array.isArray(data.errors)) {
              throw new Error('错误记录响应异常');
            }
            const rows = [...data.failed_requests.slice(-30), ...data.errors.slice(-20)];
            byId('events-summary').textContent = rows.map(row => Array.isArray(row)
              ? row.map(value => String(value ?? '').slice(0, 300)).join(' · ') : '').filter(Boolean).join('\n');
            status('events-status', rows.length ? '最近记录 ' + rows.length + ' 条' : '本次运行暂无失败记录。');
          } catch (error) { status('events-status', errorText(error), true); }
        })(),
      ]);
    } finally {
      refreshing = false;
      byId('diagnostics-refresh').disabled = false;
    }
  }
  async function generate() {
    if (generating) return;
    generating = true;
    report = '';
    byId('report-download').disabled = true;
    byId('report-preview').hidden = true;
    byId('report-text').value = '';
    status('report-status', '正在生成报告，数据较多时可能需要稍等…');
    try {
      const data = await requestJSON('/api/diagnostics/report', {
        timeout:60000, button:byId('report-generate'),
      });
      if (data.ok !== true || typeof data.text !== 'string' || !data.text.trim()) {
        throw new Error('诊断报告为空或格式异常，请重试');
      }
      report = data.text;
      byId('report-text').value = report;
      byId('report-preview').hidden = false;
      byId('report-preview').open = true;
      byId('report-download').disabled = false;
      status('report-status', '报告已生成，请预览确认后下载。');
    } catch (error) { status('report-status', errorText(error), true); }
    finally { generating = false; }
  }
  function download() {
    if (!report || generating) return;
    const url = URL.createObjectURL(new Blob([report], {type:'text/plain;charset=utf-8'}));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'webnovel诊断-Web-' + new Date().toISOString().replace(/[:.]/g, '-') + '.txt';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  byId('diagnostics-refresh').addEventListener('click', refresh);
  byId('report-generate').addEventListener('click', generate);
  byId('report-download').addEventListener('click', download);
  refresh();
})();
