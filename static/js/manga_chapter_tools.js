(function () {
  'use strict';
  function key(value) {
    return String(value || '').normalize('NFKC').toLowerCase().replace(/\s/g, '')
      .replace(/[第話话章回集卷節节篇]/g, '');
  }
  function find(rows, query) {
    const needle = key(query);
    return rows.map((row, index) => ({row, index})).filter(entry =>
      !needle || key(entry.row.name + ' ' + (entry.row.group || '')).includes(needle)).slice(0, 100);
  }
  function report(detail, source, comicId, localOnly) {
    const rows = [...(detail.volumes || []), ...(detail.chapters || [])];
    const downloaded = new Set(detail.downloaded || []);
    const partial = new Set(detail.partial_downloaded || []);
    const names = new Map();
    rows.forEach(row => {
      const name = String(row.name || '').trim();
      if (name) names.set(name, [...(names.get(name) || []), String(row.id)]);
    });
    return {schema:1, scope:'visible_catalog_metadata_only', source, comic_id:comicId,
      title:detail.title, local_only:!!localOnly, chapter_count:rows.length,
      omitted_chapters:Math.max(0,rows.length-500),
      chapters:rows.slice(0,500).map(row => ({id:String(row.id).slice(0,256),
        name:String(row.name || '').slice(0,200), group:String(row.group || '').slice(0,128),
        downloaded:downloaded.has(row.id), partial:partial.has(row.id)})),
      same_name_entries:[...names].filter(([,ids])=>ids.length>1).slice(0,40)
        .map(([name,ids])=>({name:name.slice(0,200),ids:ids.slice(0,20).map(id=>id.slice(0,256))}))};
  }
  function mount(host, rows, onSelect) {
    host.replaceChildren();
    if (!rows.length) return;
    const label = document.createElement('label');
    label.textContent = '查找章节';
    const input = document.createElement('input');
    input.type = 'search';
    input.placeholder = '输入话名、话号或分组';
    input.setAttribute('aria-label','查找章节');
    label.append(input);
    const hint = document.createElement('p');
    hint.setAttribute('role','status');
    const results = document.createElement('div');
    results.className = 'finder-results';
    const names = new Map();
    rows.forEach(row=>names.set(row.name,(names.get(row.name)||0)+1));
    input.oninput = () => {
      results.replaceChildren();
      if (!input.value.trim()) { hint.textContent=''; return; }
      const matches = find(rows,input.value);
      hint.textContent = matches.length ? '最多显示 100 项，选择后从该章节第一页阅读。' : '没有匹配的章节';
      matches.forEach(({row,index})=>{
        const button = document.createElement('button');
        button.className='chap-item';
        button.dataset.chapterId=String(row.id);
        button.textContent=(index+1)+'. '+row.name+(row.group?' · '+row.group:'')+
          (names.get(row.name)>1?' · 身份 '+String(row.id).slice(-12):'');
        button.onclick=()=>onSelect(index);
        results.append(button);
      });
    };
    host.append(label,hint,results);
  }
  window.mangaChapterTools={find,report,mount};
}());
