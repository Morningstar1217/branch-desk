// Pure-memory recovery checks; no browser, service, Codex task or Git operation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, 'web', name), 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const text = node => typeof node === 'string' ? node : Array.isArray(node)
  ? node.map(text).join('') : node && typeof node === 'object' ? text(node.children) : '';
function walk(node, parents = [], result = []) {
  if (Array.isArray(node)) node.forEach(child => walk(child, parents, result));
  else if (node && typeof node === 'object') {
    result.push({ node, parents });
    walk(node.children, [...parents, node], result);
  }
  return result;
}

async function fixture() {
  const f = { calls: [], confirmations: [], handlers: {}, focused: 0, confirm: true };
  f.serverJob = { id: 'batch-one', mode: 'sync', status: 'failed', summary: '第 10 项失败',
    rows: Array.from({ length: 11 }, (_, index) => ({ source: 'refs/remotes/origin/master',
      source_sha: 'a'.repeat(40), target: 'task-' + (index + 1), path: '/fixture/task-' + (index + 1),
      status: index < 9 ? 'succeeded' : index === 9 ? 'failed' : 'pending',
      recoverable: index === 9, summary: index === 9 ? '等待检查修复' : '其他任务', commit: '' })) };
  const unmounted = [], timers = new Map();
  let timerId = 0;
  const context = vm.createContext({ console, localStorage: { getItem() { return null; } },
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id) });
  context.window = context;
  vm.runInContext(read('vendor/vue.global.prod.js'), context);
  const vue = context.Vue, scope = vue.effectScope();
  context.Vue = { ...vue, onMounted() {}, onUnmounted: fn => unmounted.push(fn) };
  vm.runInContext(read('batch.js'), context);
  const props = vue.reactive({ disabled: false, selectedPath: '', overview: { root: '/fixture', refs: [], branches: [] },
    showMessage() {}, async confirmAction(options) { f.confirmations.push(options); return f.confirm; },
    async api(route, data) {
      f.calls.push({ route, data });
      if (route === 'batch/status') return clone(f.serverJob);
      assert(f.handlers[route], 'Unexpected request: ' + route);
      return f.handlers[route](data);
    } });
  f.app = scope.run(() => context.BatchDialog.setup(props, { emit() {} }));
  f.app.modal.value = { open: true };
  f.app.body.value = { scrollTop: 0, focus() { f.focused++; } };
  const compileErrors = [];
  const render = vue.compile(context.BatchDialog.template,
    { decodeEntities: value => value, onError: error => compileErrors.push(error.message) });
  assert.deepEqual(compileErrors, []);
  f.render = () => walk(render(vue.proxyRefs({ ...props, ...f.app }), []));
  f.tick = async () => { await vue.nextTick(); await vue.nextTick(); };
  f.close = () => { unmounted.forEach(fn => fn()); scope.stop(); };
  f.props = props;
  await f.app.poll(); f.app.showJob(); await f.tick();
  f.calls = []; f.focused = 0; f.app.body.value.scrollTop = 900;
  return f;
}

function assertRowFeedback(f, role, expected) {
  const rendered = f.render();
  const matches = rendered.filter(({ node }) => node.props?.role === role && text(node).includes(expected));
  assert.equal(matches.length, 1, 'Feedback appears once, not also at the scrolled-away top');
  const row = matches[0].parents.find(node => node.type === 'li' && node.props?.['data-status']);
  assert(row, 'Feedback belongs to a result row');
  const rows = rendered.filter(({ node }) => node.type === 'li' && node.props?.['data-status']);
  assert.equal(rows.findIndex(entry => entry.node === row), 9, 'Feedback belongs to the tenth result');
}

(async () => {
  for (const [method, route, progress] of [
    ['previewResume', 'batch/resume/preview', '正在检查修复结果'],
    ['repair', 'batch/repair', '正在启动 Codex 修复'],
  ]) {
    const f = await fixture();
    try {
      const request = deferred(); f.handlers[route] = () => request.promise;
      const pending = f.app[method](9); await f.tick();
      assert.equal(f.app.loading.value, true);
      assertRowFeedback(f, 'status', progress);
      assert.equal(f.app.body.value.scrollTop, 900, 'A pending row action does not jump away');
      request.reject(Error('现场已变化，请重新检查'));
      await pending; await f.tick();
      assert.equal(f.app.loading.value, false);
      assertRowFeedback(f, 'alert', '现场已变化，请重新检查');
      assert.equal(f.app.body.value.scrollTop, 900);
      f.handlers[route] = async () => { throw Error('再次检查失败'); };
      await f.app[method](9); await f.tick();
      assert.equal(f.calls.filter(call => call.route === route).length, 2, 'Failure permits a fresh retry');
      assert.equal(f.app.loading.value, false);
      assertRowFeedback(f, 'alert', '再次检查失败');
      f.serverJob.id = 'batch-two';
      await f.app.poll(); await f.tick();
      assert.equal(f.app.rowAction.value, null, 'A different batch clears the old row action');
    } finally { f.close(); }
  }

  const cancelled = await fixture();
  try {
    const confirmation = deferred(); cancelled.confirm = confirmation.promise;
    const pending = cancelled.app.repair(9); await cancelled.tick();
    assertRowFeedback(cancelled, 'status', '等待确认修复');
    confirmation.resolve(false); await pending; await cancelled.tick();
    assert.equal(cancelled.calls.length, 0, 'Cancelling confirmation never starts a repair');
    assert.equal(cancelled.app.loading.value, false);
    assert.equal(cancelled.app.rowAction.value, null);
    assert.equal(cancelled.app.error.value, '');
  } finally { cancelled.close(); }

  const success = await fixture();
  try {
    success.handlers['batch/resume/preview'] = async () => ({ plan: 'reviewed', branch: 'task-10',
      path: '/fixture/task-10', head: 'b'.repeat(40), remaining: 2, message: 'merge: reviewed repair',
      files: 'M\tfile.vue', stat: '1 file changed', patch: 'fixture diff' });
    await success.app.previewResume(9); await success.tick();
    assert.equal(success.app.loading.value, false);
    assert.equal(success.app.resumePreview.value.plan, 'reviewed');
    assert.equal(success.app.rowAction.value, null);
    assert.equal(success.app.body.value.scrollTop, 0);
    assert.equal(success.focused, 1);
    assert(success.render().some(({ node }) => node.props?.id === 'batch-resume-heading'));
    assert.deepEqual(success.calls.map(call => call.route), ['batch/resume/preview'], 'A preview never resumes or commits');
  } finally { success.close(); }

  for (const guard of ['disabled', 'running', 'unrecoverable', 'loading']) {
    const f = await fixture();
    try {
      if (guard === 'disabled') f.props.disabled = true;
      if (guard === 'running') f.app.job.value.status = 'running';
      if (guard === 'unrecoverable') f.app.job.value.rows[9].recoverable = false;
      if (guard === 'loading') f.app.loading.value = true;
      await f.app.repair(9); await f.app.previewResume(9);
      assert.equal(f.calls.length, 0, guard + ' prevents both requests');
      assert.equal(f.confirmations.length, 0);
      assert.equal(f.app.rowAction.value, null);
    } finally { f.close(); }
  }
  console.log('Batch UI checks passed: tenth-row progress/errors, retry, cancellation, preview focus, guards and batch replacement.');
})().catch(error => { console.error(error); process.exitCode = 1; });
