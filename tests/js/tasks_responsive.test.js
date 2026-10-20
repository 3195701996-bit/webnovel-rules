'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '../..');
const css = fs.readFileSync(path.join(root, 'static/css/style.css'), 'utf8');
const tasks = fs.readFileSync(path.join(root, 'templates/tasks.html'), 'utf8');
const mangaDownload = fs.readFileSync(path.join(root, 'templates/manga_download.html'), 'utf8');
const primaryPages = ['index', 'library', 'manga', 'sources', 'task', 'tasks', 'reader', 'manga_download'];

assert.match(tasks, /class="task-toolbar"[^>]*>[\s\S]*?id="pause-all-btn"[\s\S]*?id="resume-all-btn"[\s\S]*?id="refresh-btn"/,
  'batch task controls must share a responsive toolbar');
assert.match(css, /@media\s*\(max-width:\s*600px\)[\s\S]*?\.task-toolbar\s*\{[^}]*flex-wrap:\s*wrap/s,
  'task toolbar must wrap on narrow screens');
assert.match(css, /@media\s*\(max-width:\s*600px\)[\s\S]*?\.task-table tbody tr\s*\{[^}]*display:\s*grid/s,
  'narrow-screen task rows must become cards rather than compressed table rows');
assert.match(css, /\.task-table tbody td:nth-child\(1\)\s*\{[^}]*overflow-wrap:\s*anywhere/s,
  'long task titles must be allowed to wrap instead of widening the viewport');
assert.match(css, /\.task-error-detail\s*\{[^}]*color:\s*var\(--red\)[^}]*white-space:\s*normal[^}]*overflow-wrap:\s*anywhere/s,
  'actionable task failure reasons must remain readable without overflowing narrow cards');
assert.match(css, /\.task-table \.ops \.btn\s*\{[^}]*min-height:\s*44px/s,
  'task actions must remain easy to tap on mobile');
assert.match(css, /\.task-table tbody tr\s*\{[^}]*background:\s*var\(--bg-elevated\)/s,
  'desktop task rows should use the app surface palette instead of a bare control-table style');
assert.match(css, /\.task-table tr\[data-status="running"\][\s\S]*?border-left-color:\s*var\(--accent-2\)/,
  'running status must use a semantic theme token');
assert.doesNotMatch(css, /\.task-table[^\n]*#[0-9a-fA-F]{3,8}/,
  'task status styling must not reintroduce hard-coded dashboard colors');
assert.match(tasks, /下载进度/,
  'task page should use reader-facing language rather than crawler-internal terminology');
assert.match(tasks, /暂时没有下载任务[\s\S]*?发现漫画[\s\S]*?搜索小说/,
  'empty task state should provide clear next actions for both content types');
assert.match(tasks, /spd \|\| '速度估算中'/,
  'running manga tasks must explain when the measured download rate is not available yet');
assert.match(mangaDownload, /d\.status === 'running'[\s\S]*?速度估算中/,
  'the manga download page must show estimate-pending state instead of a silent blank rate');
assert.match(mangaDownload, /role="progressbar"[^>]*aria-valuemin="0"[^>]*aria-valuemax="100"/,
  'download progress must expose a semantic progressbar to assistive technology');
assert.match(mangaDownload, /aria-valuenow', String\(pct\)/,
  'progressbar value must follow the same clamped live progress shown to the user');
assert.match(mangaDownload, /\.dl-card\s*\{[^}]*background:var\(--bg-elevated\)/s,
  'download progress surface must use the daily-use paper theme');
assert.match(mangaDownload, /\.dl-actions \.btn\s*\{[^}]*min-height:44px/s,
  'download actions must remain comfortable touch targets on mobile');
const downloadStyles = mangaDownload.match(/<style>([\s\S]*?)<\/style>/)?.[1] || '';
assert.doesNotMatch(downloadStyles, /#(?:1c2230|2a3140|34a853|8b5cf6|8ab4ff)/i,
  'download page must not retain the previous dark/blue/green debug palette');
for (const name of primaryPages) {
  const page = fs.readFileSync(path.join(root, `templates/${name}.html`), 'utf8');
  assert.match(page, /发现 · 收藏 · 阅读/,
    `${name} should use the user-facing product tagline consistently`);
  assert.doesNotMatch(page, /书源驱动 · 小说爬虫|小说爬虫与阅读中心/,
    `${name} should not present internal crawler terminology as the product identity`);
}

console.log('tasks_responsive.test.js: ALL ASSERTIONS PASSED');
