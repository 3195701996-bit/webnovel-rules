(function (root) {
  'use strict';
  root.filterDownloadTasks = function (tasks, query, mode) {
    const normalize = value => String(value || '').normalize('NFKC').toLowerCase();
    const words = normalize(query).trim().split(/\s+/).filter(Boolean);
    return tasks.filter(task => {
      const active = task.running === true || ['running', 'queued', 'cancel'].includes(task.status);
      const recoverable = !active && ['paused', 'stopped', 'error'].includes(task.status);
      const match = mode === 'active' ? active : mode === 'recoverable' ? recoverable :
        mode === 'failed' ? !active && task.status === 'error' : mode === 'finished' ? !active && !recoverable : true;
      const title = task.type === 'manga' ? task.title :
        (task.progress && task.progress.book_name) || task.book_key || task.title;
      const text = normalize(String(title || '') + ' ' + String(task.manga_key || task.source_uid || '').split(':')[0]);
      return match && words.every(word => text.includes(word));
    });
  };
})(window);
