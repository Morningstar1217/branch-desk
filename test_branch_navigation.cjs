// Navigation uses the bundled Vue in memory: no browser, Git writes, or service processes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, 'web', name), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
function fixture(pinned = null) {
  const f = { calls: [], handlers: {}, storage: new Map(), pinned, failPreferences: false, failTarget: false, projectStatus: 'running', focused: '', services: {}, moduleScroll: { scrollTop: 0 }, events: new Map() };
  const trees = ['master', 'M-2-需求乙', 'M-1-需求甲'].map(branch => ({ branch, name: branch, path: '/fixture/' + branch, ref: 'refs/heads/' + branch }));
  const uncreated = { branch: 'new', name: 'new', ref: 'refs/heads/new', path: '' };
  f.overview = { root: '/fixture', branches: [...trees, uncreated], worktrees: trees, refs: trees.map(({ ref, name }) => ({ ref, name })) };
  f.trees = trees; f.uncreated = uncreated;
  const snapshot = tree => ({ ...plain(tree), head: 'abc', status: '', conflicts: [], operations: [], upstream_ok: true,
    blocked: '', update_blocked: '', ...f.snapshotOverrides });
  const storage = { getItem: key => f.storage.get(key) || null, setItem: (key, value) => f.storage.set(key, value) };
  const context = vm.createContext({ console, AbortController, URL, URLSearchParams, location: { hash: '', pathname: '/', assign(url) { f.assigned = url; } },
    scrollY: 0, scrollTo(x, y) { context.scrollY = y; },
    innerWidth: 800, innerHeight: 500,
    addEventListener: (name, fn) => f.events.set(name, fn), removeEventListener: name => f.events.delete(name),
    localStorage: storage, sessionStorage: storage, setTimeout: () => 0, clearTimeout() {}, SchedulePanel: {},
    async fetch(url, options) {
      const route = url.slice(5), data = options.body ? JSON.parse(options.body) : {}; f.calls.push({ route, data });
      let result;
      if (f.handlers[route]) result = await f.handlers[route](data);
      else if (route === 'overview') result = f.overview;
      else if (route === 'preferences') {
        if (f.failPreferences) throw Error('preferences unavailable');
        if (options.method === 'POST') f.pinned = data.pinned;
        result = { pinned: f.pinned };
      }
      else if (route === 'services/status') result = { items: f.services, error: '' };
      else if (route === 'status') result = snapshot(f.overview.worktrees.find(tree => tree.path === data.path));
      else if (route === 'changes') {
        const tree = f.overview.worktrees.find(tree => tree.path === data.path);
        result = { ...snapshot(tree), state: snapshot(tree), plan: 'changes-plan', files: f.changeFiles || [{ path: 'src/changed.js', staged: false, unstaged: true }] };
      }
      else if (route === 'stage') {
        const tree = f.overview.worktrees.find(tree => tree.path === f.ui.path.value);
        result = { plan: 'commit-plan', mode: 'worktree', path: tree.path, branch: tree.branch, head: 'abc',
          state: snapshot(tree), message: data.message, files: 'A  src/already-staged.js', stat: ' 1 file changed', patch: '', warning: '' };
      }
      else if (route === 'branch/update') {
        if (f.updateHttpCode === 404) return { ok: false, status: 404, json: async () => ({ error: '接口不存在' }) };
        if (f.failUpdate) throw Error('更新受阻：本地与远端历史已分叉');
        result = { status: f.updateKind || 'updated', state: snapshot(f.overview.worktrees.find(tree => tree.path === data.path)) };
      }
      else if (route === 'codex/new') result = { branch: data.branch };
      else if (route === 'target') {
        if (f.failTarget) throw Error('target unavailable');
        const tree = f.overview.branches.find(tree => tree.ref === data.ref);
        if (!tree.path) { tree.path = '/fixture/' + tree.branch; f.overview.worktrees.push(tree); }
        result = snapshot(tree);
      } else if (route === 'task-links') result = { items: { 1: { branch_ref: trees[1].ref, status: 'succeeded', thread_url: '' } } };
      else if (route === 'projects/connect') result = { status: data.start ? 'started' : f.projectStatus, url: data.start || f.projectStatus === 'running' ? 'http://127.0.0.1:8765/#project-token' : '' };
      else if (route === 'projects/update') result = { current_id: 'a'.repeat(64), default_id: 'a'.repeat(64), projects: [{ id: 'a'.repeat(64), path: '/fixture', name: data.name || '项目' }] };
      else if (route === 'projects') result = { current_id: 'a'.repeat(64), default_id: 'a'.repeat(64), projects: [{ id: 'a'.repeat(64), path: '/fixture', name: '项目' }], settings: { service: {}, feishu: { enabled: false } }, service_options: { supported: false } };
      else throw Error('Unexpected request: ' + route);
      return { ok: true, json: async () => plain(result) };
    }
  });
  context.window = context;
  vm.runInContext(read('vendor/vue.global.prod.js'), context);
  const vue = context.Vue, scope = vue.effectScope(), unmount = [];
  f.sourceRect = { left: 10, top: 420, bottom: 452, width: 600 };
  context.document = { querySelector: () => null, removeEventListener() {},
    getElementById: id => ({ focus() { f.focused = id; }, scrollIntoView() {}, getBoundingClientRect: () => f.sourceRect, querySelector: () => ({ focus() { f.focused = id + '-first'; } }) }) };
  context.Vue = { ...vue, onMounted() {}, onUnmounted: fn => unmount.push(fn), createApp(options) {
    f.ui = scope.run(options.setup); return { mount() {} };
  } };
  vm.runInContext(read('app.js'), context);
  f.ui.confirmationDialog.value = { showModal() {}, close() {} };
  f.ui.overviewPane.value = { scrollTop: 0, closest() { return f.moduleScroll; }, focus() { f.focused = 'overview'; } };
  return Object.assign(f, { vue, snapshot, context, async ready() { await f.ui.refresh(); await vue.nextTick(); f.calls.length = 0; },
    async settle() { for (let i = 0; i < 6; i++) await vue.nextTick(); },
    close() { unmount.forEach(fn => fn()); scope.stop(); } });
}
let checks = 0;
async function check(name, fn) { const f = fixture(); try { await f.ready(); await fn(f); checks++; console.log('ok - ' + name); } finally { f.close(); } }
function mergedCleanupFixture(f) {
  const ui = f.ui, base = 'a'.repeat(40);
  ui.bulkCleanupDialog.value = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  f.handlers['branch/merged/list'] = async () => ({ root: '/fixture', base_ref: 'refs/remotes/origin/master', base,
    rows: f.trees.map((tree, index) => ({ ...tree, reason: index === 0 ? '环境分支保留' : '' })) });
  f.handlers['branch/merged/preview'] = async data => {
    const tree = f.trees.find(tree => tree.ref === data.ref);
    return { plan: tree.ref + '-plan', ...tree, head: 'b'.repeat(40), root_path: '/fixture', base_ref: 'refs/remotes/origin/master', base,
      force: data.discard_ignored, ignored: data.discard_ignored ? [{ path: 'node_modules/', kind: '目录', count: 3 }] : [], generated: [] };
  };
  f.handlers['branch/merged/delete'] = async data => {
    const tree = f.trees.find(tree => tree.ref + '-plan' === data.plan);
    return { branch: tree.branch, path: tree.path, head: 'b'.repeat(40), status: 'succeeded', released: true, deleted: true };
  };
  return ui;
}
async function unlinkFixture(f, count = 2) {
  const ui = f.ui;
  ui.projectSettings.value.feishu = { enabled: true, space_id: 'fixture-space' };
  await f.settle();
  const dialog = () => ({ open: false, showModal() { this.open = true; }, close() { this.open = false; } });
  ui.quickDialog.value = dialog(); ui.unlinkDialog.value = dialog();
  f.links = Object.fromEntries(Array.from({ length: count }, (_, index) => {
    const id = String(1000 + index);
    return [id, { branch_ref: 'refs/heads/deleted-' + id, thread_url: 'codex://threads/fixture-' + id }];
  }));
  f.handlers['task-links'] = async () => ({ items: plain(f.links), warning: '' });
  f.handlers['task-links/unbind'] = async data => {
    const results = data.items.map(item => {
      const link = f.links[item.work_item_id];
      if (link.branch_ref !== item.expected_ref || data.missing_only && f.overview.branches.some(row => row.ref === item.expected_ref)) {
        return { work_item_id: item.work_item_id, status: 'skipped', error: '关联或分支已变化' };
      }
      link.unlinked_ref = link.branch_ref; link.branch_ref = ''; link.unlinked_at = 1234567890;
      return { work_item_id: item.work_item_id, status: 'succeeded', error: '' };
    });
    return { results, items: plain(f.links), warning: '' };
  };
  return ui;
}
(async () => {
  await check('unified refresh fetches before reading the list and internal refresh remains local', async f => {
    const ui = f.ui;
    await ui.refresh(); assert(!f.calls.some(call => call.route === 'fetch'));
    f.calls.length = 0; f.handlers.fetch = async () => ({});
    ui.plan.value = { plan: 'old-preview' };
    await ui.fetchRemote();
    assert.equal(f.calls[0].route, 'fetch'); assert.equal(f.calls[1].route, 'overview');
    assert.equal(ui.plan.value, null); assert(ui.fetchedAt.value); assert.match(ui.message.value, /远端引用和列表已刷新/);
    const html = read('index.html');
    assert.equal((html.match(/@click="fetchRemote"/g) || []).length, 1);
    assert.match(html, /@click="fetchRemote"[^>]*>刷新<\/button>/);
    assert.match(html, /@click="restartWorkbench"[^>]*>重启当前工作台<\/button>/);
    assert.doesNotMatch(html, />刷新远端引用<\/button>|>刷新列表<\/button>/);
  });
  await check('refresh always attempts the list read, retains both errors and never retries or stamps a failed fetch', async f => {
    const ui = f.ui; ui.fetchedAt.value = 'previous';
    f.handlers.fetch = async () => { throw Error('origin unavailable'); };
    f.handlers.overview = async () => { throw Error('overview unavailable'); };
    await ui.fetchRemote();
    assert.deepEqual(f.calls.map(call => call.route), ['fetch', 'overview']);
    assert.match(ui.error.value, /origin unavailable/); assert.match(ui.error.value, /overview unavailable/);
    assert.equal(ui.fetchedAt.value, 'previous'); assert.equal(ui.overview.value.root, '/fixture');
    f.calls.length = 0; ui.busy.value = true; await ui.fetchRemote(); assert.equal(f.calls.length, 0); ui.busy.value = false;
    f.handlers.fetch = async () => ({}); await ui.fetchRemote();
    assert.notEqual(ui.fetchedAt.value, 'previous'); assert.match(ui.error.value, /overview unavailable/);
  });
  await check('restart requires confirmation and reloads only after the same project has a new runtime identity', async f => {
    const ui = f.ui, original = { identity: 'a'.repeat(32), common: '/fixture/.git', app: '/workbench', pid: 123 };
    let restarted = false, reloads = 0;
    f.context.location.reload = () => { reloads++; };
    f.handlers.runtime = async () => ({ ...original, identity: (restarted ? 'b' : 'a').repeat(32) });
    f.handlers['runtime/restart'] = async data => {
      assert.deepEqual(data, { confirmed: true, expected_identity: original.identity });
      restarted = true; return { status: 'restarting' };
    };
    const cancelled = ui.restartWorkbench(); await f.settle();
    assert.equal(ui.confirmation.value.title, '重启当前工作台？');
    assert(!f.calls.some(call => call.route === 'runtime/restart'));
    ui.finishConfirmation(false); await cancelled; assert.equal(reloads, 0);
    const accepted = ui.restartWorkbench(); await f.settle(); ui.finishConfirmation(true); await accepted;
    assert.equal(f.calls.filter(call => call.route === 'runtime/restart').length, 1);
    assert.equal(reloads, 1); assert.equal(ui.busy.value, false);
  });
  await check('restart waits through old and unavailable runtime probes, then stops on timeout without replaying the request', async f => {
    const ui = f.ui, original = { identity: 'a'.repeat(32), common: '/fixture/.git', app: '/workbench' };
    let clock = 0, probes = 0, reloads = 0, accepted = false;
    f.context.Date = class extends Date { static now() { return clock; } };
    f.context.location.reload = () => { reloads++; };
    f.context.setTimeout = (callback, ms) => {
      if (ms === 400) { clock += ms; Promise.resolve().then(callback); }
      return 0;
    };
    f.handlers.runtime = async () => {
      if (accepted) { probes++; if (probes % 2 === 0) throw Error('connecting'); }
      return original;
    };
    f.handlers['runtime/restart'] = async () => { accepted = true; return { status: 'restarting' }; };
    const result = ui.restartWorkbench(); await f.settle(); ui.finishConfirmation(true); await result;
    assert.match(ui.error.value, /重启超时/); assert.equal(reloads, 0); assert(probes > 1);
    assert.equal(f.calls.filter(call => call.route === 'runtime/restart').length, 1);
    assert.equal(ui.busy.value, false);
  });
  await check('pending edits and previews block restart, and mismatched projects or old backends never reload', async f => {
    const ui = f.ui, original = { identity: 'a'.repeat(32), common: '/fixture/.git', app: '/workbench' };
    let reloads = 0, current = original;
    f.context.location.reload = () => { reloads++; };
    f.handlers.runtime = async () => current;
    ui.plan.value = { plan: 'pending' }; await ui.restartWorkbench(); assert.equal(f.calls.length, 0); ui.plan.value = null;
    ui.scheduleDirty.value = true; await ui.restartWorkbench(); assert.equal(f.calls.length, 0); ui.scheduleDirty.value = false;
    f.handlers['runtime/restart'] = async () => {
      current = { ...original, identity: 'b'.repeat(32), common: '/other/.git' }; return { status: 'restarting' };
    };
    const mismatch = ui.restartWorkbench(); await f.settle(); ui.finishConfirmation(true); await mismatch;
    assert.match(ui.error.value, /身份不匹配/); assert.equal(reloads, 0);
    current = original; f.handlers['runtime/restart'] = async () => { throw Error('接口不存在'); };
    const oldBackend = ui.restartWorkbench(); await f.settle(); ui.finishConfirmation(true); await oldBackend;
    assert.match(ui.error.value, /通过启动器重启一次/); assert.equal(reloads, 0);
    assert.equal(f.calls.filter(call => call.route === 'runtime/restart').length, 2);
  });
  await check('delivered-link selection covers more than sixty rows, previews without writes, then refreshes remaining attention', async f => {
    const ui = await unlinkFixture(f, 72);
    f.links['999'] = { branch_ref: f.trees[1].ref };
    f.snapshotOverrides = { conflicts: ['src/conflict.js'] };
    await ui.openUnlink();
    assert.equal(ui.unlinkRows.value.length, 72);
    ui.selectVisibleUnlinks(); assert.equal(ui.unlinkSelected.value.length, 72);
    ui.unlinkSearch.value = 'deleted-1000'; assert.equal(ui.unlinkVisible.value.length, 1);
    await ui.previewUnlink(); assert.equal(ui.unlinkPreview.value.rows.length, 72);
    assert.equal(f.calls.filter(call => call.route === 'task-links/unbind').length, 0);
    ui.resetUnlinkPreview(); assert.equal(ui.unlinkSelected.value.length, 72);
    await ui.previewUnlink(); await ui.executeUnlink();
    assert.equal(ui.unlinkResults.value.filter(row => row.status === 'succeeded').length, 72);
    assert.equal(f.links['1000'].thread_url, 'codex://threads/fixture-1000');
    assert.equal(f.links['999'].branch_ref, f.trees[1].ref);
    assert.equal(ui.attentionItems.value.filter(row => row.key.startsWith('association:')).length, 0);
    assert.equal(ui.attentionItems.value.filter(row => row.key.startsWith('conflict:')).length, 3);
    const writes = f.calls.filter(call => call.route === 'task-links/unbind');
    assert.equal(writes.length, 1); assert.equal(writes[0].data.space_id, 'fixture-space');
    assert.equal(writes[0].data.missing_only, true); assert.equal(writes[0].data.confirmed, true);
    await ui.executeUnlink(); assert.equal(f.calls.filter(call => call.route === 'task-links/unbind').length, 1);
  });
  await check('unlink preview detects changed links and execution skips changes after preview', async f => {
    const ui = await unlinkFixture(f);
    await ui.openUnlink(); ui.selectVisibleUnlinks();
    f.links['1000'].branch_ref = 'refs/heads/rebound';
    await ui.previewUnlink(); assert.equal(ui.unlinkPreview.value, null);
    assert.match(ui.unlinkIssue.value, /已变化/);
    await ui.openUnlink(); ui.selectVisibleUnlinks(); await ui.previewUnlink();
    f.overview.branches.push({ ref: 'refs/heads/rebound', branch: 'rebound', name: 'rebound', path: '' });
    f.links['1000'].unlinked_at = 1234;
    await ui.executeUnlink();
    assert.deepEqual(plain(ui.unlinkResults.value.map(row => row.status)), ['skipped', 'succeeded']);
    assert.equal(ui.unlinkResults.value[0].unlinked_at, null);
    assert.equal(f.links['1000'].branch_ref, 'refs/heads/rebound');
  });
  await check('unlink timeout consumes its confirmation and rereads actual remaining links without automatic replay', async f => {
    const ui = await unlinkFixture(f);
    await ui.openUnlink(); ui.selectVisibleUnlinks(); await ui.previewUnlink();
    f.handlers['task-links/unbind'] = async () => { f.links['1000'].branch_ref = ''; throw Error('timeout'); };
    await ui.executeUnlink();
    assert.equal(ui.unlinkPreview.value, null); assert.match(ui.unlinkIssue.value, /timeout/);
    assert(ui.unlinkResults.value.every(row => row.status === 'unconfirmed'));
    await ui.executeUnlink(); assert.equal(f.calls.filter(call => call.route === 'task-links/unbind').length, 1);
    await ui.openUnlink(); assert.deepEqual(plain(ui.unlinkRows.value.map(row => row.work_item_id)), ['1001']);
    ui.selectVisibleUnlinks(); await ui.previewUnlink();
    ui.projectSettings.value.feishu = { enabled: true, space_id: 'changed-space' }; await f.settle();
    assert.equal(ui.unlinkDialog.value.open, false);
    await ui.executeUnlink(); assert.equal(f.calls.filter(call => call.route === 'task-links/unbind').length, 1);
  });
  await check('cleanup synchronizes only explicitly selected associations and keeps Git success on metadata failure', async f => {
    const ui = mergedCleanupFixture(f); await unlinkFixture(f);
    f.links['1000'].branch_ref = f.trees[1].ref; f.links['1001'].branch_ref = f.trees[2].ref;
    await ui.openBulkCleanup('merged'); ui.selectAllBulkCleanup(); await ui.previewBulkCleanup();
    assert.equal(ui.bulkCleanupLinks.value.length, 2); assert.equal(ui.bulkCleanupUnlink.value, false);
    ui.bulkCleanupUnlink.value = true;
    const unbind = f.handlers['task-links/unbind'];
    f.handlers['task-links/unbind'] = async data => {
      if (data.items[0].work_item_id === '1001') throw Error('storage failure');
      return unbind(data);
    };
    await ui.executeBulkCleanup();
    assert.deepEqual(plain(ui.bulkCleanupResults.value.map(row => row.status)), ['succeeded', 'succeeded']);
    assert.equal(ui.bulkCleanupResults.value[0].unlinked, 1);
    assert.match(ui.bulkCleanupResults.value[1].linkError, /storage failure/);
    assert.equal(f.links['1001'].branch_ref, f.trees[2].ref);
    assert(f.calls.filter(call => call.route === 'task-links/unbind').every(call => call.data.missing_only === false));
  });
  await check('cleanup leaves associations alone when unchecked and stops before Git writes after a selected link changes', async f => {
    const ui = mergedCleanupFixture(f); await unlinkFixture(f, 1);
    f.links['1000'].branch_ref = f.trees[1].ref;
    await ui.openBulkCleanup('merged'); ui.bulkCleanupSelected.value = [f.trees[1].ref]; await ui.previewBulkCleanup();
    await ui.executeBulkCleanup(); assert.equal(f.calls.filter(call => call.route === 'task-links/unbind').length, 0);
    await ui.openBulkCleanup('merged'); ui.bulkCleanupSelected.value = [f.trees[1].ref]; await ui.previewBulkCleanup();
    ui.bulkCleanupUnlink.value = true; f.links['1000'].branch_ref = f.trees[2].ref;
    const writes = f.calls.filter(call => call.route === 'branch/merged/delete').length;
    await ui.executeBulkCleanup(); assert.equal(f.calls.filter(call => call.route === 'branch/merged/delete').length, writes);
    assert.equal(ui.bulkCleanupResults.value[0].status, 'failed');
    f.handlers['task-links'] = async () => { throw Error('metadata unavailable'); };
    await ui.openBulkCleanup('merged'); ui.bulkCleanupSelected.value = [f.trees[1].ref]; await ui.previewBulkCleanup();
    assert.equal(ui.bulkCleanupReady.value, true); assert.match(ui.bulkCleanupLinksIssue.value, /metadata unavailable/);
    assert.equal(ui.bulkCleanupUnlink.value, false);
    await ui.executeBulkCleanup(); assert.equal(ui.bulkCleanupResults.value[0].status, 'succeeded');
  });
  await check('delivered-link dialog template compiles selection, preview, result and empty states', async f => {
    const ui = await unlinkFixture(f, 1), errors = [];
    const template = read('index.html').match(/<dialog ref="unlinkDialog"[\s\S]*?<\/dialog>/)[0];
    const render = f.vue.compile(template, { decodeEntities: value => value, onError: error => errors.push(error.message) });
    await ui.openUnlink(); render(f.vue.proxyRefs(ui), []);
    ui.selectVisibleUnlinks(); await ui.previewUnlink(); render(f.vue.proxyRefs(ui), []);
    await ui.executeUnlink(); render(f.vue.proxyRefs(ui), []);
    await ui.openUnlink(); render(f.vue.proxyRefs(ui), []);
    assert.deepEqual(errors, []); assert.equal(ui.unlinkRows.value.length, 0);
  });
  await check('merged cleanup lists protected branches, previews without writes, then releases and deletes only confirmed selections', async f => {
    const ui = mergedCleanupFixture(f);
    await ui.openBulkCleanup('merged');
    assert.equal(ui.bulkCleanupTitle.value, '删除已合并的分支');
    assert.deepEqual(f.calls.map(call => call.route), ['branch/merged/list']);
    ui.selectAllBulkCleanup();
    assert.deepEqual(plain(ui.bulkCleanupSelected.value), f.trees.slice(1).map(tree => tree.ref));
    await ui.previewBulkCleanup();
    assert.equal(ui.bulkCleanupReady.value, true);
    assert.equal(f.calls.filter(call => call.route.endsWith('/delete')).length, 0);
    await ui.executeBulkCleanup();
    assert.deepEqual(plain(ui.bulkCleanupResults.value.map(row => row.status)), ['succeeded', 'succeeded']);
    const writes = f.calls.filter(call => call.route === 'branch/merged/delete');
    assert.equal(writes.length, 2);
    assert(writes.every(call => call.data.confirmation === true && call.data.discard_confirmation === false));
    assert(!f.calls.some(call => ['worktree/release', 'branch/delete'].includes(call.route)));
    const count = f.calls.length;
    await ui.executeBulkCleanup(); assert.equal(f.calls.length, count, 'Completed plans cannot be replayed');
  });
  await check('ignored files are listed in a cancellable second confirmation and never deleted after cancellation', async f => {
    const ui = mergedCleanupFixture(f);
    await ui.openBulkCleanup('merged', [], true);
    ui.bulkCleanupSelected.value = [f.trees[1].ref];
    await ui.previewBulkCleanup();
    const cancel = ui.executeBulkCleanup(); await f.settle();
    assert.match(ui.confirmation.value.message, /node_modules\/.*目录.*3 项/s);
    assert.match(ui.confirmation.value.message, /移除目录：\/fixture/);
    ui.finishConfirmation(false); await cancel;
    assert.equal(f.calls.filter(call => call.route.endsWith('/delete')).length, 0);
    assert.equal(ui.bulkCleanupReady.value, true);
    const confirm = ui.executeBulkCleanup(); await f.settle(); ui.finishConfirmation(true); await confirm;
    assert.equal(f.calls.find(call => call.route === 'branch/merged/delete').data.discard_confirmation, true);
  });
  await check('merged cleanup stops on partial failure and preserves the completed release result', async f => {
    const ui = mergedCleanupFixture(f);
    f.handlers['branch/merged/delete'] = async () => ({ branch: f.trees[1].branch, released: true, deleted: false, status: 'failed', error: '分支锁定' });
    await ui.openBulkCleanup('merged'); ui.selectAllBulkCleanup(); await ui.previewBulkCleanup(); await ui.executeBulkCleanup();
    assert.deepEqual(plain(ui.bulkCleanupResults.value.map(row => row.status)), ['failed', 'pending']);
    assert.equal(ui.bulkCleanupResults.value[0].released, true);
    assert.match(ui.bulkCleanupResults.value[0].error, /分支锁定/);
    assert.equal(f.calls.filter(call => call.route.endsWith('/delete')).length, 1);
    assert.match(ui.bulkCleanupIssue.value, /停止后续项/);
  });
  await check('changed baseline and failed reads prevent merged cleanup execution', async f => {
    const ui = mergedCleanupFixture(f), preview = f.handlers['branch/merged/preview'];
    await ui.openBulkCleanup('merged'); ui.selectAllBulkCleanup();
    f.handlers['branch/merged/preview'] = async data => ({ ...await preview(data), base: 'c'.repeat(40) });
    await ui.previewBulkCleanup(); assert.equal(ui.bulkCleanupReady.value, false);
    await ui.executeBulkCleanup(); assert(!f.calls.some(call => call.route.endsWith('/delete')));
    ui.closeBulkCleanup(); f.handlers['branch/merged/list'] = async () => { throw Error('接口不存在'); };
    await ui.openBulkCleanup('merged'); assert.match(ui.bulkCleanupIssue.value, /重启/); assert.equal(ui.bulkCleanupRows.value.length, 0);
  });
  await check('the current review worktree remains excluded from merged cleanup selection', async f => {
    const ui = mergedCleanupFixture(f);
    await ui.enterReview(f.trees[1]);
    await ui.openBulkCleanup('merged');
    assert.match(ui.bulkCleanupRows.value.find(row => row.ref === f.trees[1].ref).reason, /当前审查工作区/);
    ui.selectAllBulkCleanup();
    assert.deepEqual(plain(ui.bulkCleanupSelected.value), [f.trees[2].ref]);
  });
  await check('merged cleanup template compiles and exposes the entry, ignore list, protected reasons and partial results', async f => {
    const ui = mergedCleanupFixture(f), html = read('index.html');
    const template = html.slice(html.indexOf('<dialog ref="bulkCleanupDialog"'), html.indexOf('<dialog ref="releaseDialog"'));
    const errors = [];
    const render = f.vue.compile(template, { decodeEntities: value => value, onError: error => errors.push(error.message) });
    assert.deepEqual(errors, []);
    function text(node) { return typeof node === 'string' ? node : Array.isArray(node) ? node.map(text).join('') : node && typeof node === 'object' ? text(node.children) : ''; }
    await ui.openBulkCleanup('merged');
    assert.match(text(render(f.vue.proxyRefs(ui), [])), /环境分支保留/);
    ui.bulkCleanupSelected.value = [f.trees[1].ref]; await ui.previewBulkCleanup();
    assert.match(text(render(f.vue.proxyRefs(ui), [])), /确认释放并删除本地分支/);
    assert.match(html, /@click="openBulkCleanup\('merged'\)"[^>]*>删除已合并的分支/);
  });
  await check('existing ordinary bulk release and delete retain their separate preview and execution routes', async f => {
    const ui = mergedCleanupFixture(f);
    assert.doesNotMatch(read('index.html'), /清理候选|cleanupDialog|scanCleanup/);
    assert.equal(ui.scanCleanup, undefined);
    assert.equal(ui.checkCleanup, undefined);
    f.handlers['worktree/release/preview'] = async data => {
      const tree = f.trees.find(tree => tree.path === data.path);
      return { ...tree, plan: tree.ref + '-plan', head: 'abc', force: false, generated: [] };
    };
    f.handlers['worktree/release'] = async data => {
      const tree = f.trees.find(tree => tree.ref + '-plan' === data.plan);
      return { branch: tree.branch, path: tree.path, head: 'abc' };
    };
    await ui.openBulkCleanup('release'); ui.selectAllBulkCleanup(); await ui.previewBulkCleanup();
    assert.equal(ui.bulkCleanupReady.value, true); await ui.executeBulkCleanup();
    assert.equal(f.calls.filter(call => call.route === 'worktree/release').length, 2);
    assert(ui.bulkCleanupResults.value.every(row => row.status === 'succeeded'));
    ui.closeBulkCleanup(); f.calls.length = 0;
    f.handlers['branch/delete/preview'] = async data => ({ ...f.uncreated, plan: 'delete-plan', ref: data.ref, head: 'abc',
      base: 'abc', base_ref: 'origin/new', upstream: 'origin/new', root_head: 'root', root_branch: 'master', root_path: '/fixture', force: false });
    f.handlers['branch/delete'] = async () => ({ branch: f.uncreated.branch });
    await ui.openBulkCleanup('delete'); ui.selectAllBulkCleanup(); await ui.previewBulkCleanup(); await ui.executeBulkCleanup();
    assert.equal(f.calls.filter(call => call.route === 'branch/delete/preview').length, 2, 'Existing deletion renews a one-use preview after config changes');
    assert.equal(f.calls.filter(call => call.route === 'branch/delete').length, 1);
    assert.equal(ui.bulkCleanupResults.value[0].status, 'succeeded');
    assert(!f.calls.some(call => call.route.startsWith('branch/merged/')));
  });
  for (const file of ['index.html', 'app.js', 'schedule.js', 'batch.js']) {
    assert(!/<select\b/i.test(read(file)), `${file}: selection controls must use the workbench UI, not an OS-native popup`);
  }
  await check('saved favorites survive a fresh browser origin and a remote branch becoming local', async f => {
    await f.ui.pinBranch(f.trees[1]);
    assert.deepEqual(plain(f.pinned), ['refs/heads/pre', 'refs/heads/test', f.trees[1].ref]);
    const nextOrigin = fixture(f.pinned);
    try {
      await nextOrigin.ready();
      assert.equal(nextOrigin.ui.isPinned(nextOrigin.trees[1]), true);
      const remote = { branch: 'topic', ref: 'refs/remotes/origin/topic', name: 'origin/topic', remote: 'origin', path: '' };
      await nextOrigin.ui.pinBranch(remote);
      assert.equal(nextOrigin.ui.isPinned({ ...remote, ref: 'refs/heads/topic', remote: '' }), true);
      assert.deepEqual(plain(nextOrigin.pinned), ['refs/heads/pre', 'refs/heads/test', f.trees[1].ref, 'refs/heads/topic']);
    } finally { nextOrigin.close(); }
  });
  await check('favorite save failure keeps the previous selection and reports the error', async f => {
    f.failPreferences = true;
    await f.ui.pinBranch(f.trees[1]);
    assert.equal(f.ui.isPinned(f.trees[1]), false);
    assert.match(f.ui.error.value, /常用分支保存失败/);
  });
  await check('changing a favorite keeps disclosure focus after regrouping or falls back when filtered out', async f => {
    const ui = f.ui, tree = f.trees[1];
    ui.toggleBranchDetails(tree);
    await ui.pinBranch(tree);
    assert.equal(ui.expandedBranch.value, tree.ref);
    assert.equal(f.focused, ui.branchDetailsId(tree) + '-toggle');
    ui.treeFilter.value = 'pinned'; await f.settle();
    await ui.pinBranch(tree); await f.settle();
    assert.equal(ui.expandedBranch.value, '');
    assert.equal(f.focused, 'overview');
  });
  {
    const f = fixture();
    try {
      f.storage.set('worktree-merge-preferences:/fixture', JSON.stringify({ pinned: ['refs/remotes/origin/topic'], recent: [] }));
      await f.ready();
      assert.deepEqual(plain(f.pinned), ['refs/heads/topic']);
      assert.equal(f.ui.isPinned({ branch: 'topic', ref: 'refs/heads/topic' }), true);
      checks++; console.log('ok - current-origin favorites migrate to stable branch keys');
    } finally { f.close(); }
  }
  {
    const f = fixture(['refs/heads/saved']);
    try {
      f.failPreferences = true;
      await f.ready();
      assert.equal(f.ui.preferencesLoaded.value, false);
      assert.match(f.ui.storageWarning.value, /常用分支读取失败/);
      await f.ui.pinBranch(f.trees[1]);
      assert.deepEqual(plain(f.pinned), ['refs/heads/saved']);
      f.failPreferences = false;
      await f.ui.refresh();
      assert.equal(f.ui.preferencesLoaded.value, true);
      assert.equal(f.ui.isPinned({ branch: 'saved', ref: 'refs/heads/saved' }), true);
      checks++; console.log('ok - failed favorite reads block writes and retry on refresh');
    } finally { f.close(); }
  }
  await check('one disclosure contains details and actions, opens one branch and restores focus without selecting a worktree', async f => {
    const ui = f.ui, tree = f.trees[1];
    assert.equal(ui.mergePane.value, 'overview'); assert.match(ui.branchWorkspace(tree).label, /未检查/);
    ui.toggleBranchDetails(tree);
    assert.equal(ui.expandedBranch.value, tree.ref); assert.equal(ui.path.value, '');
    ui.toggleBranchDetails(f.trees[2]); assert.equal(ui.expandedBranch.value, f.trees[2].ref);
    await ui.closeBranchDetails(); assert.equal(f.focused, ui.branchDetailsId(f.trees[2]) + '-toggle');
    assert.equal(ui.expandedBranch.value, '');
    assert.equal(f.calls.length, 0);
    assert.match(ui.branchWorkspace(f.uncreated).label, /未创建/);
    assert.match(ui.branchWorkspace({ ...tree, missing: true }).label, /不存在/);
    const html = read('index.html');
    assert(!/branchMenu|toggleBranchMenu|closeBranchMenu|branch-more|branch-menu-row/.test(html));
    const details = html.slice(html.indexOf('<section :id="branchDetailsId(w)"'), html.indexOf('</section></td></tr>'));
    for (const action of ['updateBranch(w)', 'pinBranch(w)', 'copyPath(w.path)', 'openService(w)', 'previewRelease(w)', 'previewBranchDelete(w)']) {
      assert(details.includes(action), `${action} stays in the single disclosure`);
      assert.equal(details.split(action).length - 1, 1, `${action} appears once`);
    }
    assert(html.includes('@keydown.esc.stop="closeBranchDetails()"'));
    assert(!details.includes('复制名称'));
    ui.toggleBranchDetails(tree); ui.search.value = 'no matching branch'; await f.settle();
    assert.equal(ui.expandedBranch.value, '');
  });
  await check('branch details retain the copy icon and update only the confirmed row', async f => {
    const ui = f.ui, tree = f.trees[1];
    assert.match(read('index.html'), /@click="updateBranch\(w\)"[^>]*>更新<\/button>/);
    assert(!read('index.html').includes('copyBranch(w.branch);closeBranchMenu()'));
    assert.match(read('index.html'), /class="branch-copy"[^>]*@click\.stop="copyBranch\(w.branch\)"/);
    await ui.enterReview(f.trees[2]);
    ui.treeFilter.value = 'worktree'; ui.search.value = '需求'; await f.settle();
    f.calls.length = 0;
    const pending = ui.updateBranch(tree); await f.settle();
    assert.equal(ui.confirmation.value.title, '更新当前分支');
    assert(ui.confirmation.value.message.includes('origin/' + tree.branch));
    assert.equal(f.calls.filter(call => call.route === 'branch/update').length, 0);
    ui.finishConfirmation(true); await pending;
    assert.deepEqual(plain(f.calls.find(call => call.route === 'branch/update').data), {
      path: tree.path, branch: tree.branch, head: 'abc', confirmed: true
    });
    assert.equal(ui.path.value, f.trees[2].path);
    assert.equal(ui.mergePane.value, 'review');
    assert.equal(ui.treeFilter.value, 'worktree'); assert.equal(ui.search.value, '需求');
    assert.match(ui.message.value, /已更新/);
  });
  await check('branch conversation opens the matching worktree in Codex before the service action', async f => {
    const ui = f.ui, tree = f.trees[1];
    const html = read('index.html'), actions = html.slice(html.indexOf('class="branch-row-actions"'), html.indexOf('</div></td>', html.indexOf('class="branch-row-actions"')));
    assert(actions.indexOf('openBranchConversation(w)') < actions.indexOf('启动</button>'));
    const pending = ui.openBranchConversation(tree); await f.settle(); await pending;
    assert.deepEqual(plain(f.calls.find(call => call.route === 'codex/new').data), { path: tree.path, branch: tree.branch });
    assert.match(ui.message.value, /已在 Codex 打开「M-2-需求乙」的新对话.*提示已预填/);
    assert.equal(ui.error.value, '');
    f.calls.length = 0;
    await ui.openBranchConversation(f.uncreated);
    await ui.openBranchConversation({ ...tree, missing: true });
    assert.equal(f.calls.length, 0);
  });
  await check('current branch commit prefill follows the M-ID subject format and includes a description', async f => {
    const tree = f.trees[1];
    tree.branch = 'M-7139236919-【数据看板】V2.2.1新增环形堆叠柱状报表&周期对比&子报表筛选逻辑';
    tree.name = tree.branch; tree.ref = 'refs/heads/' + tree.branch;
    f.snapshotOverrides = { status: ' M src/changed.js' };
    await f.ui.enterReview(tree); f.calls.length = 0;
    await f.ui.reviewCurrentChanges();
    assert.equal(f.ui.commitMessage.value, 'fix: M-7139236919 【数据看板】V2.2.1新增环形堆叠柱状报表&周期对比&子报表筛选逻辑');
    assert(f.ui.commitMessage.value.length <= 108);
    assert.equal((f.ui.commitMessage.value.match(/M-7139236919/g) || []).length, 1);
  });
  await check('staging an all-staged current branch keeps the selected workspace and opens its commit preview', async f => {
    const tree = f.trees[1];
    tree.branch = 'M-7141925811-【结算】月度对账工具';
    tree.name = tree.branch; tree.ref = 'refs/heads/' + tree.branch;
    f.snapshotOverrides = { status: 'A  src/already-staged.js' };
    f.changeFiles = [{ path: 'src/already-staged.js', staged: true, unstaged: false, untracked: false }];
    await f.ui.enterReview(tree); f.calls.length = 0;
    await f.ui.reviewCurrentChanges();
    assert.equal(f.ui.selectableStageFiles.value.length, 0);
    await f.ui.stageCurrentFiles();
    assert.equal(f.ui.state.value.path, tree.path);
    assert.equal(f.ui.state.value.branch, tree.branch);
    assert.equal(f.ui.commitPreview.value.mode, 'worktree');
    assert.equal(f.calls.find(call => call.route === 'stage').data.files.length, 0);
  });
  await check('master supports merge review and local commits while retaining same-name updates and push protection', async f => {
    const ui = f.ui, tree = f.trees[0];
    await ui.enterReview(tree);
    assert.equal(ui.mergePane.value, 'review'); assert.equal(ui.path.value, tree.path);
    const buttonTemplate = read('index.html').match(/<button[^>]*class="branch-enter-review"[\s\S]*?<\/button>/)[0];
    const renderButton = f.vue.compile(buttonTemplate, { decodeEntities: value => value });
    assert.equal(renderButton(f.vue.proxyRefs({ ...ui, w: tree }), []).props.disabled, false);
    assert.equal(ui.canPush.value, false);
    ui.state.value = { ...f.snapshot(tree), status: ' M common.txt' };
    assert.equal(ui.canCommitCurrent.value, true);
    ui.state.value = { ...f.snapshot(tree), operations: ['MERGE_HEAD'] };
    assert.equal(ui.canCommit.value, true);
    ui.state.value = { ...ui.state.value, conflicts: ['common.txt'] };
    assert.equal(ui.canReviewResolution.value, true);
    ui.state.value = f.snapshot(tree);
    assert.equal(ui.canUpdateBranch(tree), true);
    const pending = ui.updateBranch(tree); await f.settle();
    assert.equal(ui.confirmation.value.title, '更新当前分支');
    assert(ui.confirmation.value.message.includes('origin/master'));
    assert.equal(f.calls.filter(call => call.route === 'branch/update').length, 0);
    ui.finishConfirmation(true); await pending;
    assert.deepEqual(plain(f.calls.find(call => call.route === 'branch/update').data), {
      path: tree.path, branch: 'master', head: 'abc', confirmed: true
    });
    assert.match(ui.message.value, /已更新 master/); assert.equal(ui.error.value, '');
    assert.equal(ui.mergePane.value, 'review'); assert.equal(ui.path.value, tree.path);
  });
  await check('dirty master and a backend missing local-master update support stop before confirmation or mutation', async f => {
    const ui = f.ui, tree = f.trees[0];
    for (const [update_blocked, expected] of [
      ['存在暂存、未暂存或未跟踪文件，请先自行处理。', /存在暂存/],
      [undefined, /尚未加载本地 master 更新支持.*重启后台.*重新连接工作台/]
    ]) {
      f.snapshotOverrides = { update_blocked };
      await ui.updateBranch(tree);
      assert.match(ui.error.value, expected); assert.equal(ui.confirmation.value, null);
      assert.equal(ui.message.value, ui.error.value); assert.equal(ui.messageType.value, 'error');
      assert.equal(f.calls.filter(call => call.route === 'branch/update').length, 0);
    }
  });
  await check('cancelling an update and unprepared or busy branches never send a mutation', async f => {
    const ui = f.ui, tree = f.trees[1];
    const pending = ui.updateBranch(tree); await f.settle(); ui.finishConfirmation(false); await pending;
    assert.equal(f.calls.filter(call => call.route === 'branch/update').length, 0);
    f.calls.length = 0;
    for (const unavailable of [f.uncreated, { ...tree, missing: true }]) {
      assert.equal(ui.canUpdateBranch(unavailable), false); await ui.updateBranch(unavailable);
    }
    for (const lock of ['busy', 'batchRunning', 'scheduleLocked']) {
      ui[lock].value = true; assert.equal(ui.canUpdateBranch(tree), false); await ui.updateBranch(tree); ui[lock].value = false;
    }
    for (const preview of ['plan', 'commitPreview', 'pushPreview', 'resolutionPreview']) {
      ui[preview].value = { plan: 'keep' }; assert.equal(ui.canUpdateBranch(tree), false); await ui.updateBranch(tree);
      assert.equal(ui[preview].value.plan, 'keep'); ui[preview].value = null;
    }
    assert.equal(f.calls.length, 0);
  });
  await check('an update failure preserves the selected branch and surfaces the error without retry', async f => {
    const ui = f.ui, tree = f.trees[1];
    await ui.enterReview(tree); f.failUpdate = true;
    const pending = ui.updateBranch(tree); await f.settle(); ui.finishConfirmation(true); await pending;
    assert.match(ui.error.value, /历史已分叉/);
    assert.equal(ui.message.value, ui.error.value); assert.equal(ui.messageType.value, 'error'); assert.equal(ui.path.value, tree.path);
    assert.equal(f.calls.filter(call => call.route === 'branch/update').length, 1);
  });
  await check('an old backend gives a recovery instruction and never retries the update', async f => {
    const ui = f.ui, tree = f.trees[1];
    await ui.enterReview(tree); f.updateHttpCode = 404;
    const pending = ui.updateBranch(tree); await f.settle(); ui.finishConfirmation(true); await pending;
    assert.match(ui.error.value, /后台版本较旧.*重启后台.*重新连接工作台.*未更新分支/);
    assert.equal(ui.path.value, tree.path); assert.equal(ui.state.value.head, 'abc');
    assert.equal(ui.message.value, ui.error.value); assert.equal(ui.messageType.value, 'error');
    assert.equal(f.calls.filter(call => call.route === 'branch/update').length, 1);
  });
  for (const kind of ['unchanged', 'ahead']) {
    await check(`an ${kind} branch reports that no incoming update was needed`, async f => {
      f.updateKind = kind;
      const pending = f.ui.updateBranch(f.trees[1]); await f.settle(); f.ui.finishConfirmation(true); await pending;
      assert.match(f.ui.message.value, kind === 'ahead' ? /本地领先远端.*本地提交已保留/ : /已与 origin 同名分支一致/);
      assert.equal(f.ui.error.value, '');
    });
  }
  await check('project switch retains a pending review and only connects after it is cleared', async f => {
    const ui = f.ui, project = { id: 'a'.repeat(64), path: '/another', name: '另一个项目' };
    ui.plan.value = { plan: 'pending' };
    await ui.connectProject(project);
    assert.equal(f.calls.filter(call => call.route === 'projects/connect').length, 0);
    ui.plan.value = null;
    await ui.connectProject(project);
    assert.equal(f.calls.filter(call => call.route === 'projects/connect').length, 1);
    assert.equal(f.assigned, 'http://127.0.0.1:8765/#project-token');
    assert.equal(ui.overview.value.root, '/fixture');
  });
  await check('stopped project opens a status prompt and starts only on explicit action', async f => {
    const ui = f.ui, project = { id: 'b'.repeat(64), path: '/stopped', name: '未启动项目' };
    f.projectStatus = 'stopped';
    ui.projectDialog.value = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
    await ui.connectProject(project);
    assert.equal(ui.projectDialog.value.open, true);
    assert.equal(f.assigned, undefined);
    assert.deepEqual(plain(f.calls.filter(call => call.route === 'projects/connect').map(call => call.data.start)), [false]);
    await ui.connectProject(ui.pendingProject.value, true);
    assert.deepEqual(plain(f.calls.filter(call => call.route === 'projects/connect').map(call => call.data.start)), [false, true]);
    assert.equal(f.assigned, 'http://127.0.0.1:8765/#project-token');
  });
  await check('renaming a project does not resubmit unchanged service or Feishu configuration', async f => {
    const ui = f.ui;
    await ui.loadProjects();
    ui.projectForm.value = plain(ui.projectSettings.value);
    ui.projectDisplayName.value = '新项目名';
    await ui.saveProjectSettings();
    const update = f.calls.find(call => call.route === 'projects/update');
    assert.deepEqual(plain(update.data), { id: 'a'.repeat(64), name: '新项目名' });
  });
  await check('review round trip preserves filters, scroll, selected source and unfinished preview', async f => {
    const ui = f.ui, tree = f.trees[1];
    ui.search.value = '需求'; ui.treeFilter.value = 'worktree'; ui.branchSort.value = 'name'; ui.toggleBranchDetails(tree);
    await f.settle(); ui.overviewPane.value.scrollTop = 210; f.moduleScroll.scrollTop = 80; f.context.scrollY = 40;
    await ui.enterReview(tree); await f.settle();
    assert.equal(ui.mergePane.value, 'review'); assert.equal(ui.path.value, tree.path);
    assert.equal(f.calls.filter(call => call.route === 'target').length, 1);
    assert.equal(ui.branchWorkspace(tree).label, '工作区干净');
    ui.source.value = f.trees[2].ref; await f.settle();
    ui.plan.value = { plan: 'existing', count: 1 }; ui.commitMessage.value = '保留说明';
    await ui.returnToOverview(); assert.equal(ui.overviewPane.value.scrollTop, 210);
    assert.equal(f.moduleScroll.scrollTop, 80); assert.equal(f.context.scrollY, 40);
    assert.equal(ui.search.value, '需求'); assert.equal(ui.treeFilter.value, 'worktree');
    assert.equal(ui.branchSort.value, 'name'); assert.equal(ui.expandedBranch.value, tree.ref);
    const before = f.calls.length; await ui.enterReview(tree);
    assert.equal(f.calls.length, before); assert.equal(ui.plan.value.plan, 'existing');
    assert.equal(ui.source.value, f.trees[2].ref); assert.equal(ui.commitMessage.value, '保留说明');
  });
  await check('switching a reviewed branch requires confirmation and cancellation retains everything', async f => {
    const ui = f.ui;
    await ui.enterReview(f.trees[1]); ui.commitPreview.value = { mode: 'worktree', plan: 'old' }; ui.commitMessage.value = '未提交说明';
    await ui.returnToOverview(); const count = f.calls.length;
    const cancelled = ui.enterReview(f.trees[2]); await f.settle();
    assert.equal(ui.confirmation.value.title, '切换接收分支'); ui.finishConfirmation(false); await cancelled;
    assert.equal(f.calls.length, count); assert.equal(ui.path.value, f.trees[1].path); assert.equal(ui.commitMessage.value, '未提交说明');
    const accepted = ui.enterReview(f.trees[2]); await f.settle(); ui.finishConfirmation(true); await accepted;
    assert.equal(ui.path.value, f.trees[2].path); assert.equal(ui.mergePane.value, 'review'); assert.equal(ui.commitPreview.value, null);
  });
  await check('creation confirmation, failure, missing directories and locks retain their existing guards', async f => {
    const ui = f.ui;
    const cancel = ui.enterReview(f.uncreated); await f.settle(); assert.equal(ui.confirmation.value.title, '创建独立 worktree');
    ui.finishConfirmation(false); await cancel; assert.equal(f.calls.length, 0);
    await ui.enterReview({ ...f.trees[1], missing: true });
    ui.busy.value = true; await ui.enterReview(f.trees[1]); ui.busy.value = false; assert.equal(f.calls.length, 0);
    f.failTarget = true; await ui.enterReview(f.trees[1]); assert.equal(ui.mergePane.value, 'overview'); assert.equal(ui.error.value, 'target unavailable');
    f.failTarget = false;
    const create = ui.enterReview(f.uncreated); await f.settle(); ui.finishConfirmation(true); await create;
    assert.equal(ui.state.value.branch, 'new'); assert.equal(ui.mergePane.value, 'review');
  });
  await check('quick navigation, schedule links and saved review reach the dedicated review area', async f => {
    const ui = f.ui;
    const template = read('index.html').match(/<div class="quick-footer">[\s\S]*?<\/dialog>/)[0].replace(/<\/dialog>$/, '');
    const render = f.vue.compile(template, { decodeEntities: value => value });
    for (const mode of ['attention', 'search']) {
      for (const locked of [false, true]) {
        const footer = render(f.vue.proxyRefs({ ...ui, quickMode: mode, feishuEnabled: true,
          attentionBusy: locked, quickBusy: locked, navigationLocked: false }), []);
        const actions = footer.children.find(child => child.props?.class === 'quick-footer-actions');
        assert(actions, 'Footer buttons share a right-aligned action group');
        assert(!footer.children.some(child => child.type === 'button'));
        const buttons = actions.children.filter(child => child.type === 'button');
        assert.equal(buttons.length, mode === 'attention' ? 2 : 1);
        assert(buttons.every(button => button.props.disabled === locked));
      }
    }
    await ui.navigateBranch(f.trees[1].ref); assert.equal(ui.mergePane.value, 'review');
    await ui.returnToOverview(); await ui.openTaskBranch({ work_item_id: '1', ref: f.trees[1].ref, title: '关联需求' });
    assert.equal(ui.mergePane.value, 'review'); assert.equal(ui.activeTask.value.title, '关联需求');
    ui.resume.value = { view: 'merge', mergePane: 'review', filter: 'running', sort: 'name', path: f.trees[1].path, branch: f.trees[1].branch, source: '', search: '需求' };
    await ui.restoreWorkspace(); assert.equal(ui.mergePane.value, 'review'); assert.equal(ui.treeFilter.value, 'running'); assert.equal(ui.branchSort.value, 'name');
    await ui.navigateBranch(f.uncreated.ref); assert.equal(ui.mergePane.value, 'overview'); assert.equal(ui.expandedBranch.value, f.uncreated.ref);
  });
  await check('service filtering and name order do not infer unqueried Git state', async f => {
    const ui = f.ui;
    f.services = { [f.trees[1].path]: { status: 'running', ports: [8081] }, [f.trees[2].path]: { status: 'starting', ports: [] } };
    await ui.refreshServices();
    ui.treeFilter.value = 'running'; assert.deepEqual(plain(ui.filteredTrees.value.map(tree => tree.ref)), [f.trees[1].ref]);
    ui.treeFilter.value = 'all'; ui.search.value = '需求'; ui.branchSort.value = 'name';
    assert.deepEqual(plain(ui.orderedBranchGroups.value.flatMap(group => group.trees.map(tree => tree.ref))), [f.trees[2].ref, f.trees[1].ref]);
    assert.match(ui.branchWorkspace(f.trees[1]).label, /未检查/);
  });
  await check('source dropdown fits above or below, preserves selection on Escape and dismisses only outside scrolling', async f => {
    const ui = f.ui;
    const menu = { style: {}, open: false, matches() { return this.open; }, showPopover() { this.open = true; }, hidePopover() { this.open = false; },
      contains(target) { return target === menu; }, getBoundingClientRect() { return { height: Math.min(320, parseFloat(this.style.maxHeight)) }; } };
    ui.sourceMenu.value = menu;
    ui.openSource(); await f.settle(); assert.equal(menu.open, true); assert.equal(menu.style.top, '94px');
    f.events.get('scroll')({ target: menu }); assert.equal(ui.sourceOpen.value, true);
    ui.sourceKeydown({ key: 'Escape' }); assert.equal(menu.open, false); assert.equal(ui.source.value, '');
    f.sourceRect = { left: 760, top: 10, bottom: 42, width: 200 };
    ui.openSource(); await f.settle(); assert.equal(menu.style.top, '48px'); assert.equal(menu.style.left, '592px');
    ui.sourceKeydown({ key: 'ArrowDown', preventDefault() {} });
    ui.sourceKeydown({ key: 'Enter', preventDefault() {} });
    assert.equal(ui.source.value, f.trees[1].ref); assert.equal(menu.open, false);
    ui.openSource(); await f.settle(); f.events.get('scroll')({ target: {} }); assert.equal(menu.open, false);
    ui.openSource(); await f.settle(); f.events.get('resize')(); assert.equal(menu.open, false);
    ui.openSource(); ui.closeSource(); await f.settle(); assert.equal(menu.open, false);
    ui.openSource(); await f.settle(); ui.busy.value = true; await f.settle(); assert.equal(menu.open, false); ui.busy.value = false;
    ui.openSource(); await f.settle(); ui.view.value = 'schedule'; await f.settle(); assert.equal(menu.open, false);
    assert.equal(f.calls.length, 0);
  });
  console.log(`Branch navigation checks passed: ${checks}.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
