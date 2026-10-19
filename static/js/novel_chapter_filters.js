/* Same display-only rules as Android NovelChapterFilters: never renumber chapters. */
(function () {
  'use strict';
  window.filterNovelChapters = function (chapters, query, mode) {
    const normalized = String(query || '').normalize('NFKC').trim().toLowerCase();
    const numeric = /^[0-9]+$/.test(normalized) ? normalized.replace(/^0+/, '') || '0' : null;
    const terms = normalized.split(/\s+/).filter(Boolean);
    if (!terms.length && mode !== 'downloaded' && mode !== 'missing') return chapters;
    return chapters.filter(chapter => {
      if (mode === 'downloaded' && chapter.downloaded !== true) return false;
      if (mode === 'missing' && chapter.downloaded === true) return false;
      if (numeric !== null) return String(chapter.index) === numeric;
      if (!terms.length) return true;
      const label = String(chapter.name || '第 ' + chapter.index + ' 章').normalize('NFKC').toLowerCase();
      return terms.every(term => label.includes(term));
    });
  };
})();
