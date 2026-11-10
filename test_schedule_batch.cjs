const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture(selectedPath = '/two') {
  const f = { calls: [], dialogOpen: false, status: null, hook: null };
  const branches = ['one', 'two', 'pre', 'master'].map(branch => ({
    branch, name: branch, ref: 'refs/heads/' + branch, path: '/' + branch, missing: false
  }));
  f.overview = { root: '/fixture', refs: branches.map(({ ref, name }) => ({ ref, name })), branches, worktrees: [] };
  f.links = { items: { 1: { branch_ref: 'refs/heads/one' }, 2: { branch_ref: 'refs/heads/two' }, 3: { branch_ref: 'refs/heads/one' } }, warning: '' };
  const storage = { getItem() { return null; }, setItem() {} };
  const context = vm.createContext({ console, setTimeout: () => 0, clearTimeout() {},
    location: { hash: '', pathname: '/' }, localStorage: storage, sessionStorage: storage,
    async fetch(url, options) {
      const route = url.replace('/api/', '');
      f.calls.push({ route, data: JSON.parse(options.body) });
      await f.hook?.(route);
      const responses = { overview: f.overview, 'task-links': f.links, 'batch/status': f.status };
      assert(Object.hasOwn(responses, route), 'Unexpected API request: ' + route);
      return { ok: true, json: async () => JSON.parse(JSON.stringify(responses[route])) };
    }, SchedulePanel: {} });
  context.window = context;
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'web/vendor/vue.global.prod.js'), 'utf8'), context);
  context.document = { querySelector: () => f.dialogOpen ? {} : null, getElementById() { return null; } };
  const vue = context.Vue;
  context.Vue = { ...vue, onMounted() {}, onUnmounted() {}, createApp(options) {
    f.app = options.setup(); return { mount() {} };
  } };
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'web/batch.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'web/app.js'), 'utf8'), context);
  f.app.overview.value = JSON.parse(JSON.stringify(f.overview));
  f.app.view.value = 'schedule';
  f.app.scheduleLocked.value = true;
  f.app.schedulePanel.value = { busy: true, editor: null };
  f.batch = context.BatchDialog.setup({ api: f.app.api, selectedPath, showMessage() {},
    get overview() { return f.app.overview.value; },
    get disabled() { return f.app.busy.value || f.app.resolving.value; },
  }, { emit(event, value) { if (event === 'locked') f.app.batchRunning.value = value; } });
  f.batch.modal.value = { open: false, showModal() { this.open = true; f.dialogOpen = true; } };
  f.app.batchDialog.value = f.batch;
  f.tasks = [{ work_item_id: '2', ref: 'refs/heads/two' }, { work_item_id: '1', ref: 'refs/heads/one' }, { work_item_id: '3', ref: 'refs/heads/one' }];
  return f;
}

(async () => {
  const local = fixture();
  await local.app.mergeRelease(local.tasks);
  assert.equal(local.app.view.value, 'merge');
  assert.deepEqual(Array.from(local.batch.sources.value), ['refs/heads/two', 'refs/heads/one']);
  assert.deepEqual(Array.from(local.batch.targets.value), ['refs/heads/pre']);
  assert.equal(local.batch.screen.value, 'edit');
  assert.equal(local.batch.preview.value, null);
  assert.equal(local.batch.modal.value.open, true);
  assert.deepEqual(local.calls.map(call => call.route).sort(), ['batch/status', 'overview', 'task-links']);

  const remote = fixture();
  remote.overview.branches.find(branch => branch.branch === 'pre').ref = 'refs/remotes/origin/pre';
  remote.overview.branches.find(branch => branch.branch === 'pre').path = '';
  await remote.app.mergeRelease(remote.tasks);
  assert.deepEqual(Array.from(remote.batch.targets.value), ['refs/remotes/origin/pre']);

  const manual = fixture();
  await manual.batch.open('all-in');
  assert.deepEqual(Array.from(manual.batch.targets.value), ['refs/heads/two']);
  assert.equal(manual.batch.sources.value.length, 0);
  const master = fixture('/master');
  await master.batch.open('all-in');
  assert.deepEqual(Array.from(master.batch.targets.value), ['refs/heads/master']);
  master.batch.targets.value = [];
  master.batch.applyCombination({ mode: 'all-in', sources: ['refs/heads/one'], targets: ['refs/heads/master'] });
  assert.deepEqual(Array.from(master.batch.targets.value), ['refs/heads/master']);

  const sync = fixture();
  sync.app.overview.value.refs.push({ ref: 'refs/remotes/origin/master', name: 'origin/master' });
  await sync.batch.open('sync');
  assert.deepEqual(Array.from(sync.batch.sources.value), ['refs/remotes/origin/master']);
  assert.equal(sync.batch.targets.value.length, 0);

  for (const preview of ['plan', 'commitPreview', 'pushPreview', 'resolutionPreview']) {
    const f = fixture(); f.app[preview].value = {};
    await assert.rejects(f.app.mergeRelease(f.tasks), /完成或取消/);
    assert.equal(f.calls.length, 0);
  }
  const editor = fixture(); editor.app.schedulePanel.value.editor = {};
  await assert.rejects(editor.app.mergeRelease(editor.tasks), /完成或取消/);
  const developing = fixture(); developing.app.schedulePanel.value.developing = true;
  await assert.rejects(developing.app.mergeRelease(developing.tasks), /完成或取消/);
  const modal = fixture(); modal.dialogOpen = true;
  await assert.rejects(modal.app.mergeRelease(modal.tasks), /完成或取消/);
  const changed = fixture(); changed.links.items[1].branch_ref = 'refs/heads/two';
  await assert.rejects(changed.app.mergeRelease(changed.tasks), /关联已变化/);
  assert.equal(changed.app.view.value, 'schedule');
  const missing = fixture(); missing.overview.branches = missing.overview.branches.filter(branch => branch.branch !== 'one');
  await assert.rejects(missing.app.mergeRelease(missing.tasks), /来源分支不可用/);
  const noPre = fixture(); noPre.overview.branches = noPre.overview.branches.filter(branch => branch.branch !== 'pre');
  await assert.rejects(noPre.app.mergeRelease(noPre.tasks), /没有可用的 pre/);
  const targetSource = fixture(); targetSource.links.items[1].branch_ref = 'refs/heads/pre';
  await assert.rejects(targetSource.app.mergeRelease([{ work_item_id: '1', ref: 'refs/heads/pre' }]), /来源分支不可用/);
  const limit = fixture();
  const many = Array.from({ length: 31 }, (_, index) => {
    const work_item_id = String(index + 10), ref = 'refs/heads/task-' + index;
    limit.links.items[work_item_id] = { branch_ref: ref };
    limit.overview.branches.push({ branch: 'task-' + index, ref });
    return { work_item_id, ref };
  });
  await assert.rejects(limit.app.mergeRelease(many), /最多合并 30/);

  const race = fixture(); race.hook = async route => { if (route === 'batch/status') race.app.busy.value = true; };
  await assert.rejects(race.app.mergeRelease(race.tasks), /状态已变化/);
  assert.equal(race.app.view.value, 'schedule');
  assert.equal(race.batch.modal.value.open, false);
  assert.equal(race.batch.loading.value, false);
  const unavailable = fixture(); unavailable.hook = async route => { if (route === 'batch/status') throw new Error('fixture failure'); };
  await assert.rejects(unavailable.app.mergeRelease(unavailable.tasks), /读取批量状态失败/);
  assert.equal(unavailable.app.view.value, 'schedule');
  assert.equal(unavailable.batch.modal.value.open, false);

  const running = fixture(); running.status = { id: 'existing', mode: 'sync', status: 'running', rows: [] };
  running.batch.sources.value = ['refs/heads/old'];
  await running.app.mergeRelease(running.tasks);
  assert.equal(running.batch.screen.value, 'job');
  assert.equal(running.batch.job.value.id, 'existing');
  assert.deepEqual(Array.from(running.batch.sources.value), ['refs/heads/old']);
  assert.match(running.batch.error.value, /尚未填入/);
  console.log('Schedule batch checks passed: ordered deduplication, local/remote pre, read-only entry, preview/editor/modal guards, stale links, unavailable sources/target, async locks and existing queue preservation.');
})().catch(error => { console.error(error); process.exitCode = 1; });
