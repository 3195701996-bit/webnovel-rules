'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'templates/task.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static/css/style.css'), 'utf8');

assert.match(html, /role="status" aria-live="polite" hidden/,
  'task action and load errors must be announced accessibly');
assert.match(html, /role="progressbar" aria-label="任务进度"[\s\S]*aria-valuenow="0"/,
  'task progress must expose an accessible initial value');
assert.match(html, /任务状态暂时加载失败，页面会自动重试/,
  'polling failures must explain automatic recovery instead of replacing task title');
assert.match(html, /id="task-failure"[^>]*class="task-error-detail"[^>]*role="alert"/,
  'task detail must expose a screen-reader announcement for actionable terminal errors');
assert.match(html, /st === 'error' \? String\(t\.stop_reason \|\| t\.error \|\| ''\)\.trim\(\)\.slice\(0, 240\)/,
  'task detail must display the persisted service error reason, bounded and safely assigned as text');
assert.match(html, /r\.status === 404[\s\S]*taskPoller\.stop\(\)[\s\S]*return 'missing'/,
  'deleted tasks must show a permanent-not-found message and stop polling');
assert.match(html, /if \(!response\.ok \|\| data\.ok === false\) throw/,
  'task actions must validate both transport and business success');
assert.match(html, /taskAction\(\$\('#btn-stop'\), 'stop'/,
  'stop must use the shared checked action path');
assert.match(css, /@media \(max-width: 600px\)[\s\S]*?\.task-action-row > \.btn \{[^}]*min-height: 44px/s,
  'task controls must remain tappable on narrow screens');

const action = html.match(/async function taskAction\(button, action, pendingText, successText\) \{[\s\S]*?\n\}/);
assert.ok(action, 'task actions must share one state/feedback implementation');

async function exerciseAction(response, expected) {
  const button = {disabled: false, textContent: '⏸ 暂停'};
  const classes = new Set();
  const feedback = {
    textContent: '', hidden: true,
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
    },
  };
  const messages = [];
  let kicks = 0;
  const sandbox = {
    TASK_ID: 't1',
    fetch: async () => response,
    $: selector => selector === '#task-feedback' ? feedback : button,
    toast: message => messages.push(message),
    taskPoller: {kick: () => kicks++},
    String,
  };
  vm.createContext(sandbox);
  vm.runInContext(action[0] + '\nglobalThis.run = taskAction;', sandbox);
  await sandbox.run(button, 'pause', '暂停中…', '暂停请求已接受');
  assert.equal(button.disabled, expected.success);
  assert.equal(feedback.hidden, false);
  assert.equal(classes.has('task-feedback-error'), !expected.success);
  assert.match(feedback.textContent, expected.pattern);
  assert.equal(kicks, expected.success ? 1 : 0);
  assert.equal(messages.length, 1);
}

Promise.all([
  exerciseAction({ok: true, json: async () => ({ok: true})}, {
    success: true, pattern: /暂停请求已接受/,
  }),
  exerciseAction({ok: false, status: 503, json: async () => ({error: '服务暂不可用'})}, {
    success: false, pattern: /失败：服务暂不可用/,
  }),
  exerciseAction({ok: true, json: async () => ({ok: false, error: '任务已结束'})}, {
    success: false, pattern: /失败：任务已结束/,
  }),
]).then(() => console.log('task_detail.test.js: ALL ASSERTIONS PASSED'))
  .catch(error => { console.error(error); process.exitCode = 1; });
