// Pure-memory Vue checks: no HTTP server, browser, or development process is started.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, 'web', name), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function fixture() {
  const timers = new Map(), mounted = [], unmounted = [], events = new Map(), calls = [], storage = new Map();
  const trees = [{ path: '/fixture', branch: 'master' }, { path: '/fixture/task', branch: 'M-task' }];
  const overview = { root: '/fixture', worktrees: trees, refs: [], branches: trees.map(tree => ({ ...tree, ref: 'refs/heads/' + tree.branch })) };
  const state = { items: Object.fromEntries(trees.map(tree => [tree.path, { status: 'stopped', ports: [] }])), statusError: '', startError: '', statusHold: null, startHold: null,
    stopPreviewError: '', stopError: '', stopPreviewHold: null, stopHold: null, stopPreview: null, stopStatus: 'stopping' };
  let timerId = 0, ui;
  const context = vm.createContext({ console, Intl, Date, URL, AbortController,
    location: { hash: '', pathname: '/' },
    localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
    sessionStorage: { getItem() { return null; } },
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; }, clearTimeout: id => timers.delete(id),
    async fetch(url, options) {
      const route = url.replace('/api/', ''), payload = options.body ? JSON.parse(options.body) : null;
      calls.push({ route, payload });
      let value;
      if (route === 'overview') value = plain(overview);
      else if (route === 'status') value = { ...trees.find(tree => tree.path === payload.path), status: '', conflicts: [], operations: [] };
      else if (route === 'task-links') value = { items: {}, warning: '' };
      else if (route === 'services/status') {
        if (state.statusHold) await state.statusHold;
        if (state.statusError) throw Error(state.statusError);
        value = { items: plain(state.items), error: '' };
      } else if (route === 'services/start/preview') {
        if (state.startError) throw Error(state.startError);
        value = { plan: 'backend-start-plan', path: payload.path, branch: payload.branch, settings: { mode: 'test', port: 8185, java_home: '', config_file: '' } };
      } else if (route === 'services/start') {
        if (state.startHold) await state.startHold;
        if (state.startError) throw Error(state.startError);
        state.items[payload.path] = { status: 'starting', ports: [], pids: [123], mode: payload.backend ? 'custom' : 'test', backend: payload.backend || 'https://test.example', log_path: '/logs/task.service.log' };
        value = { items: plain(state.items), error: '' };
      } else if (route === 'services/stop/preview') {
        state.previewSignal = options.signal;
        if (state.stopPreviewHold) await state.stopPreviewHold;
        if (state.stopPreviewError) throw Error(state.stopPreviewError);
        value = state.stopPreview || { plan: 'preview-plan', ...payload, processes: [{ pid: 123, ports: [8081] }, { pid: 456, ports: [] }] };
        state.stopTarget = value.path;
      } else if (route === 'services/stop') {
        state.stopSignal = options.signal;
        if (state.stopHold) await state.stopHold;
        if (state.stopError) throw Error(state.stopError);
        state.items[state.stopTarget] = { status: state.stopStatus, ports: [] };
        value = { items: plain(state.items), error: '' };
      } else throw Error('Unexpected request: ' + route);
      return { ok: true, json: async () => value };
    },
  });
  context.window = context;
  vm.runInContext(read('vendor/vue.global.prod.js'), context);
  const vue = context.Vue, scope = vue.effectScope();
  context.document = { visibilityState: 'visible', querySelector: () => null, getElementById: () => null,
    addEventListener: (name, fn) => events.set(name, fn), removeEventListener: name => events.delete(name) };
  context.Vue = { ...vue, onMounted: fn => mounted.push(fn), onUnmounted: fn => unmounted.push(fn), createApp(options) { ui = scope.run(options.setup); return { mount() {} }; } };
  context.SchedulePanel = {}; context.BatchDialog = {};
  vm.runInContext(read('app.js'), context);
  const dialog = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  ui.serviceDialog.value = dialog;
  ui.confirmationDialog.value = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  return { ui, context, vue, state, trees, overview, timers, events, calls, dialog,
    async ready() { ui.overview.value = plain(overview); await vue.nextTick(); await ui.refreshServices(); },
    async mount() { for (const fn of mounted) await fn(); await vue.nextTick(); },
    async settle() { for (let i = 0; i < 8; i++) await vue.nextTick(); },
    close() { unmounted.forEach(fn => fn()); scope.stop(); },
    async tick(ms) { const found = [...timers].find(([, timer]) => timer.ms === ms); assert(found, 'Expected timer: ' + ms); timers.delete(found[0]); await found[1].fn(); await vue.nextTick(); },
  };
}

let checks = 0;
async function check(name, run) { const f = fixture(); try { await run(f); checks++; console.log('ok - ' + name); } finally { f.close(); } }
(async () => {
  await check('template compiles and table service controls remain independent of review navigation', async f => {
    const html = read('index.html'), errors = [];
    f.vue.compile(html.slice(html.indexOf('<div id="app"'), html.indexOf('<noscript>')), { decodeEntities: value => value, onError: error => errors.push(error.message) });
    assert.deepEqual(errors, []);
    assert.match(html, /<table class="branch-overview-table"/);
    assert(html.includes('@click="enterReview(w)"'));
    assert(html.includes('@click="toggleBranchDetails(w)"'));
    assert(!html.includes(':href="servicePortUrl(port)"'));
    assert(html.includes('@click="copyServiceAddress(port,currentService)"'));
    assert(!html.includes('>启动测试</button>'));
    assert(html.includes('@click="stopService(w)"'));
    assert(html.includes('@click="stopService(serviceTarget)"'));
  });
  await check('status is unknown until read, safe ports only, external running has no claimed environment', async f => {
    const tree = f.trees[1];
    assert.equal(f.ui.canStartService(tree), false);
    f.state.items[tree.path] = { status: 'running', ports: [8081, 8081, 0, 65536, '8082', 'javascript:alert(1)'] };
    await f.ready();
    assert.deepEqual(plain(f.ui.serviceFor(tree).ports), [8081]);
    assert.equal(f.ui.servicePortUrl(8081), 'http://127.0.0.1:8081/');
    assert.equal(f.ui.servicePortUrl('8081'), '');
    assert.equal(f.ui.serviceBackendLabel(f.ui.serviceFor(tree)), '后端未确认');
    assert.equal(f.ui.serviceAddressKnown(f.ui.serviceFor(tree)), false);
    assert.equal(f.ui.canStartService(tree), false);
    assert.equal(f.ui.canStartService({ branch: 'not-checked-out' }), false);
    await f.ui.openService(tree, true);
    assert(!f.calls.some(call => call.route === 'services/start'));
  });
  await check('LAN copy uses observed URLs and stays hidden for stale or local-only services', async f => {
    const tree = f.trees[1], url = 'http://192.168.1.20:8081/';
    f.state.items[tree.path] = { status: 'running', ports: [8081], lan_urls: {
      8081: [url, url, 'http://10.2.3.4:8081/', 'javascript:alert(1)', 'http://0.0.0.0:8081/',
        'http://127.0.0.1:8081/', 'http://192.168.1.20:8082/'],
    } };
    await f.ready();
    const item = f.ui.serviceFor(tree), copied = [];
    f.context.navigator = { clipboard: { async writeText(value) { copied.push(value); } } };
    assert.deepEqual(plain(f.ui.serviceLanUrls(8081, item)), [url, 'http://10.2.3.4:8081/']);
    await f.ui.copyServiceAddress(8081, item, url);
    assert.deepEqual(copied, [url]);
    await f.ui.copyServiceAddress(8081, item, 'http://192.168.1.99:8081/');
    assert.deepEqual(copied, [url]);
    assert.deepEqual(plain(f.ui.serviceLanUrls(8081, { ...item, status: 'unknown' })), []);
    assert.deepEqual(plain(f.ui.serviceLanUrls(8081, { status: 'running', ports: [8081] })), []);
    assert.deepEqual(plain(f.ui.serviceLanUrls(8082, item)), []);
    assert(read('index.html').includes('@click="copyServiceAddress(port,currentService,url)"'));
    assert(read('index.html').includes('@click="copyServiceAddress(port,serviceFor(w),url)"'));
  });
  await check('multiple detected backends remain separate process evidence', async f => {
    const tree = f.trees[1];
    f.state.items[tree.path] = { status: 'running', ports: [8081, 8082], backend_label: '多个后端，见进程明细',
      processes: [{ pid: 123, ports: [8081], backends: [{ backend: 'https://test.example', source: 'runtime', label: '运行时识别' }] },
        { pid: 456, ports: [8082], backends: [{ backend: 'https://other.example', source: 'launch', label: '已核验启动记录' }] }] };
    await f.ready();
    const item = f.ui.serviceFor(tree);
    assert.equal(f.ui.serviceBackendLabel(item), '多个后端，见进程明细');
    assert.equal(f.ui.serviceAddressKnown(item), false);
    assert.deepEqual(plain(item.processes.map(process => process.backends[0].backend)), ['https://test.example', 'https://other.example']);
  });
  await check('one-click test launch binds exact tree, locks duplicate and Git actions, then shows confirmed port', async f => {
    await f.ready(); const tree = f.trees[1], gate = deferred(); f.state.startHold = gate.promise;
    const pending = f.ui.openService(tree, true); await f.vue.nextTick();
    assert.equal(f.dialog.open, true); assert.equal(f.ui.serviceSubmitting.value, true);
    assert.equal(f.ui.navigationLocked.value, true); assert.equal(f.ui.reloadLocked.value, true);
    await f.ui.startService(); await f.ui.refresh(); await f.ui.selectTarget({ branch: 'new', ref: 'refs/heads/new' });
    await f.ui.switchView('schedule');
    assert.equal(f.ui.view.value, 'merge');
    assert.equal(f.calls.filter(call => call.route === 'services/start').length, 1);
    assert(!f.calls.some(call => call.route === 'target'));
    assert.deepEqual(f.calls.find(call => call.route === 'services/start').payload, { path: tree.path, branch: tree.branch, backend: '' });
    gate.resolve(); await pending;
    assert.equal(f.ui.serviceBusy.value, false); assert.equal(f.ui.navigationLocked.value, false); assert.equal(f.ui.currentService.value.log_path, '/logs/task.service.log');
    f.state.items[tree.path] = { status: 'running', ports: [8083], mode: 'test', backend: 'https://test.example' };
    await f.ui.refreshServices();
    assert.equal(f.ui.serviceBusy.value, false); assert.deepEqual(plain(f.ui.currentService.value.ports), [8083]);
  });
  await check('custom backend validates origin and preserves error; failed process permits explicit retry', async f => {
    await f.ready(); const tree = f.trees[1]; await f.ui.openService(tree);
    for (const value of ['javascript:alert(1)', 'http://user:pass@example.com', 'https://example.com/api', 'https://example.com/?a=1']) {
      f.ui.serviceBackendUrl.value = value; await f.ui.startService(); assert.match(f.ui.serviceIssue.value, /后端地址/);
    }
    assert(!f.calls.some(call => call.route === 'services/start'));
    f.ui.serviceBackendUrl.value = ' http://192.168.1.10:8185 '; await f.ui.startService();
    assert.equal(f.calls.find(call => call.route === 'services/start').payload.backend, 'http://192.168.1.10:8185');
    assert.equal(f.ui.serviceBackendLabel(f.ui.currentService.value), '自定义后端');
    f.state.items[tree.path] = { status: 'failed', ports: [], error: '退出码 1', log_path: '/logs/task.service.log' };
    await f.ui.refreshServices(); assert.equal(f.ui.canStartService(tree), true);
    assert.equal(f.ui.currentService.value.error, '退出码 1'); assert.equal(f.dialog.open, true);
  });
  await check('unknown network/start results prohibit repeats and keep the actionable error', async f => {
    await f.ready(); const tree = f.trees[1]; await f.ui.openService(tree);
    f.state.startError = '无法读取依赖'; await f.ui.startService();
    assert.equal(f.ui.serviceIssue.value, '无法读取依赖'); assert.equal(f.ui.currentService.value.status, 'unknown');
    await f.ui.startService(); assert.equal(f.calls.filter(call => call.route === 'services/start').length, 1);
    f.state.startError = ''; await f.ui.refreshServices(); assert.equal(f.ui.canStartService(tree), true);
    f.state.statusError = '连接断开'; await f.ui.refreshServices();
    assert.equal(f.ui.canStartService(tree), false); assert.match(f.ui.currentService.value.error, /连接断开/);
    assert.equal(f.ui.serviceIssue.value, '无法读取依赖');
  });
  await check('Git locks and stale worktree identity prevent startup', async f => {
    await f.ready(); const tree = f.trees[1];
    for (const lock of ['busy', 'batchRunning', 'scheduleLocked']) {
      f.ui[lock].value = true; await f.ui.openService(tree, true); assert.equal(f.dialog.open, false); f.ui[lock].value = false;
    }
    await f.ui.openService(tree); f.ui.serviceTarget.value.branch = 'changed'; await f.ui.startService();
    assert(!f.calls.some(call => call.route === 'services/start'));
    const gate = deferred(); f.state.statusHold = gate.promise;
    const pending = f.ui.refreshServices(); assert.equal(f.ui.reloadLocked.value, true);
    f.ui.overview.value = { root: '/other', worktrees: [], branches: [], refs: [] }; await f.vue.nextTick();
    gate.resolve(); await pending; assert.equal(f.ui.serviceTarget.value, null); assert.equal(f.dialog.open, false);
    assert.equal(f.ui.serviceFor(tree).status, 'unknown');
  });
  await check('stop preview lists exact worktree/PIDs/ports, has no human-wait timeout, and cancel sends no stop', async f => {
    const tree = f.trees[1]; f.state.items[tree.path] = { status: 'running', ports: [8081] }; await f.ready();
    const pending = f.ui.stopService(tree); await f.settle();
    assert.deepEqual(f.calls.find(call => call.route === 'services/stop/preview').payload, { path: tree.path, branch: tree.branch });
    assert(!f.calls.some(call => call.route === 'services/stop'));
    assert.equal(f.ui.confirmation.value.confirmLabel, '确认停止');
    for (const value of [tree.path, tree.branch, 'PID 123', '8081', 'PID 456', '尚未监听', '不修改分支或文件']) assert(f.ui.confirmation.value.message.includes(value));
    assert.equal(f.ui.servicePendingLabel.value, '等待确认停止…');
    assert.equal(f.ui.navigationLocked.value, true);
    assert(![...f.timers.values()].some(timer => [12000, 20000].includes(timer.ms)));
    await f.ui.stopService(tree); assert.equal(f.calls.filter(call => call.route === 'services/stop/preview').length, 1);
    f.ui.finishConfirmation(false); await pending;
    assert(!f.calls.some(call => call.route === 'services/stop'));
    assert.equal(f.ui.navigationLocked.value, false); assert.equal(f.ui.canStopService(tree), true);
  });
  await check('confirmed stop submits reviewed token once, polls stopping, unlocks, then permits start', async f => {
    const tree = f.trees[1]; f.state.items[tree.path] = { status: 'starting', ports: [] }; await f.mount(); await f.ui.refreshServices();
    const gate = deferred(); f.state.stopHold = gate.promise;
    const pending = f.ui.stopService(tree); await f.settle(); f.ui.finishConfirmation(true); await f.settle();
    assert.deepEqual(f.calls.find(call => call.route === 'services/stop').payload, { plan: 'preview-plan', confirmed: true });
    assert.equal(f.ui.servicePendingLabel.value, '正在提交停止请求…');
    assert([...f.timers.values()].some(timer => timer.ms === 20000));
    await f.ui.stopService(tree); assert.equal(f.calls.filter(call => call.route === 'services/stop').length, 1);
    gate.resolve(); await pending;
    assert.equal(f.ui.currentService.value.status, 'stopping'); assert.equal(f.ui.navigationLocked.value, false);
    assert.equal(f.ui.canStopService(tree), false); assert.equal(f.ui.canStartService(tree), false);
    assert([...f.timers.values()].some(timer => timer.ms === 2500));
    f.state.items[tree.path] = { status: 'stopped', ports: [] }; await f.tick(2500);
    assert.equal(f.ui.canStartService(tree), true); assert.equal(f.ui.canStopService(tree), false);
    assert([...f.timers.values()].some(timer => timer.ms === 12000));
  });
  await check('stop preview/request errors remain in dialog and ambiguous result forbids repetition', async f => {
    const tree = f.trees[1]; f.state.items[tree.path] = { status: 'running', ports: [8081] }; await f.ready();
    f.state.stopPreviewError = '无法检查进程'; await f.ui.stopService(tree);
    assert.equal(f.ui.serviceIssue.value, '无法检查进程'); assert.equal(f.ui.currentService.value.status, 'running');
    assert(!f.calls.some(call => call.route === 'services/stop')); assert.equal(f.dialog.open, true);
    f.state.stopPreviewError = ''; f.state.stopError = '停止结果未确认';
    const pending = f.ui.stopService(tree); await f.settle(); f.ui.finishConfirmation(true); await pending;
    assert.equal(f.ui.serviceIssue.value, '停止结果未确认'); assert.equal(f.ui.currentService.value.status, 'unknown');
    assert.equal(f.ui.canStopService(tree), false); assert.equal(f.ui.canStartService(tree), false);
    await f.ui.stopService(tree); assert.equal(f.calls.filter(call => call.route === 'services/stop').length, 1);
  });
  await check('mismatched preview and changed target during confirmation never stop', async f => {
    const tree = f.trees[1]; f.state.items[tree.path] = { status: 'running', ports: [8081] }; await f.ready();
    f.state.stopPreview = { plan: 'wrong', path: '/other', branch: tree.branch, processes: [{ pid: 123, ports: [8081] }] };
    await f.ui.stopService(tree); assert.match(f.ui.serviceIssue.value, /不一致/); assert.equal(f.ui.confirmation.value, null);
    f.state.stopPreview = null;
    const pending = f.ui.stopService(tree); await f.settle();
    f.ui.overview.value = { ...plain(f.overview), worktrees: [{ ...tree, branch: 'changed' }] }; await f.vue.nextTick();
    await pending;
    assert.equal(f.ui.confirmation.value, null); assert.equal(f.ui.serviceSubmitting.value, false);
    assert(!f.calls.some(call => call.route === 'services/stop'));
  });
  await check('repository change or unmount during stop preview discards later response', async f => {
    const tree = f.trees[1]; f.state.items[tree.path] = { status: 'running', ports: [8081] }; await f.ready();
    const gate = deferred(); f.state.stopPreviewHold = gate.promise;
    const pending = f.ui.stopService(tree); await f.settle();
    f.ui.overview.value = { root: '/other', worktrees: [], branches: [], refs: [] }; await f.vue.nextTick();
    assert.equal(f.state.previewSignal.aborted, true);
    gate.resolve(); await pending; assert.equal(f.ui.confirmation.value, null);
    f.state.stopPreviewHold = null; await f.ready();
    const second = deferred(); f.state.stopPreviewHold = second.promise;
    const unmountPending = f.ui.stopService(tree); await f.settle(); f.close();
    assert.equal(f.state.previewSignal.aborted, true); second.resolve(); await unmountPending;
    assert(!f.calls.some(call => call.route === 'services/stop')); assert.equal(f.timers.size, 0);
  });
  await check('polling slows after startup, pauses while hidden, resumes on visibility and cleans up', async f => {
    await f.mount(); await f.ui.refreshServices();
    assert([...f.timers.values()].some(timer => timer.ms === 12000));
    await f.ui.openService(f.trees[1], true);
    assert([...f.timers.values()].some(timer => timer.ms === 2500));
    f.context.document.visibilityState = 'hidden'; f.events.get('visibilitychange')();
    assert(![...f.timers.values()].some(timer => timer.ms === 2500 || timer.ms === 12000));
    const before = f.calls.length; await f.ui.refreshServices(); assert.equal(f.calls.length, before);
    f.state.items[f.trees[1].path] = { status: 'running', ports: [8081], mode: 'test' };
    f.context.document.visibilityState = 'visible'; await f.events.get('visibilitychange')(); await f.vue.nextTick();
    assert.equal(f.ui.serviceFor(f.trees[1]).status, 'running');
    assert([...f.timers.values()].some(timer => timer.ms === 12000));
    f.close(); assert.equal(f.timers.size, 0); assert(!f.events.has('visibilitychange'));
  });
  await check('backend start previews profile and shared impact before sending one confirmed start', async f => {
    await f.ready(); f.ui.serviceOptions.value = { kind: 'backend', supported: true };
    const pending = f.ui.openService(f.trees[1], true); await f.settle();
    assert(f.calls.some(call => call.route === 'services/start/preview'));
    assert(!f.calls.some(call => call.route === 'services/start'));
    assert.match(f.ui.confirmation.value.message, /8185/);
    assert.match(f.ui.confirmation.value.message, /test/);
    assert.match(f.ui.confirmation.value.message, /全部|所有/);
    assert(![...f.timers.values()].some(timer => timer.ms === 20000));
    f.ui.finishConfirmation(true); await pending;
    const starts = f.calls.filter(call => call.route === 'services/start');
    assert.equal(starts.length, 1);
    assert.equal(starts[0].payload.plan, 'backend-start-plan');
    assert.equal(starts[0].payload.confirmed, true);
  });
  await check('backend cancellation and failed preview preserve stopped state without starting', async f => {
    await f.ready(); f.ui.serviceOptions.value = { kind: 'backend', supported: true };
    const pending = f.ui.openService(f.trees[1], true); await f.settle();
    f.ui.finishConfirmation(false); await pending;
    assert(!f.calls.some(call => call.route === 'services/start'));
    assert.equal(f.ui.serviceFor(f.trees[1]).status, 'stopped');
    f.state.startError = 'Java Home 无效';
    await f.ui.startService();
    assert.match(f.ui.serviceIssue.value, /Java Home/);
    assert.equal(f.ui.serviceFor(f.trees[1]).status, 'stopped');
  });
  await check('shared backend blocks another worktree and update while frontend services stay independent', async f => {
    f.state.items[f.trees[0].path] = { status: 'running', ports: [8185] };
    await f.ready(); f.ui.serviceOptions.value = { kind: 'backend', supported: true };
    assert.equal(f.ui.canStartService(f.trees[1]), false);
    assert.equal(f.ui.canUpdateBranch(f.trees[1]), false);
    await f.ui.openService(f.trees[1], true);
    assert(!f.calls.some(call => call.route === 'services/start/preview'));
    f.state.items[f.trees[0].path] = { status: 'stopped', ports: [] };
    await f.ui.refreshServices();
    assert.equal(f.ui.canStartService(f.trees[1]), true);
    assert.equal(f.ui.canUpdateBranch(f.trees[0]), false);
    f.ui.serviceOptions.value = { kind: 'frontend', supported: true };
    f.state.items[f.trees[0].path] = { status: 'running', ports: [8080] };
    await f.ui.refreshServices();
    assert.equal(f.ui.canStartService(f.trees[1]), true);
  });
  console.log(`Service UI checks passed: ${checks} scenarios.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
