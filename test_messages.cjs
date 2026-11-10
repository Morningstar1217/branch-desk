// Pure-memory feedback checks using the bundled Vue; no services or browser required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, 'web', name), 'utf8');

function fixture() {
  const timers = new Map(), storage = new Map(), unmounted = [];
  const f = { timers, shown: [], hidden: 0, failBind: false, failSave: false, failList: false };
  let timerId = 0;
  const context = vm.createContext({ console, Intl, Date,
    location: { hash: '', pathname: '/' },
    localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
    sessionStorage: { getItem() { return null; } },
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: id => timers.delete(id),
    fetch() { throw Error('Unexpected network request'); },
  });
  context.window = context;
  vm.runInContext(read('vendor/vue.global.prod.js'), context);
  const vue = context.Vue, scope = vue.effectScope();
  context.document = { querySelector: () => null, getElementById: () => null, removeEventListener() {} };
  context.Vue = { ...vue, onMounted() {}, onUnmounted: fn => unmounted.push(fn),
    createApp(options) { f.app = scope.run(options.setup); return { mount() {} }; } };
  vm.runInContext(read('schedule.js') + '\nthis.SchedulePanel = SchedulePanel;', context);
  vm.runInContext(read('batch.js'), context);
  vm.runInContext(read('app.js'), context);
  const element = { isConnected: true, open: false,
    matches: () => element.open,
    showPopover() { this.open = true; f.shown.push(f.app.message.value); },
    hidePopover() { this.open = false; f.hidden++; },
    focus() { throw Error('Messages must not take focus'); },
  };
  f.app.messageElement.value = element;
  const branches = ['task', 'pre'].map(branch => ({ branch, name: branch, ref: 'refs/heads/' + branch, path: '/' + branch }));
  const api = async route => {
    if (route === 'overview') return { branches };
    if (route === 'task-links') return { items: { 1: { branch_ref: 'refs/heads/task' } }, warning: '' };
    if (route === 'task-links/bind') {
      if (f.failBind) throw Error('关联保存失败');
      return { items: { 1: { branch_ref: 'refs/heads/task' } }, warning: '' };
    }
    if (route === 'schedule/save') {
      if (f.failSave) throw Error('保存结果未确认');
      return { message: '已保存，并从飞书重新读取核验。' };
    }
    if (route === 'schedule/overview') {
      if (f.failList) throw Error('列表读取失败');
      return { user: { key: 'me' }, rows: [] };
    }
    if (route === 'batch/status') return null;
    throw Error('Unexpected API call: ' + route);
  };
  f.schedule = scope.run(() => context.SchedulePanel.setup({ api, showMessage: f.app.showMessage,
    workspaceKey: '', locked: false, confirmAction: async () => true }, { emit() {} }));
  f.schedule.linkDialog.value = { showModal() {}, close() {} };
  f.batch = scope.run(() => context.BatchDialog.setup({ api, showMessage: f.app.showMessage,
    overview: { root: '/fixture', branches, refs: branches }, selectedPath: '/pre', disabled: false }, { emit() {} }));
  f.batch.modal.value = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  return Object.assign(f, { element, vue, context,
    close() { unmounted.forEach(fn => fn()); scope.stop(); },
    fire(id) { const timer = timers.get(id); assert(timer); timers.delete(id); timer.fn(); },
  });
}

(async () => {
  const f = fixture();
  try {
    const html = read('index.html');
    const messageTag = html.match(/<div\b[^>]*ref="messageElement"[^>]*>/)?.[0];
    assert(messageTag, 'A single persistent message host is required');
    for (const attribute of ['popover="manual"', ':role="messageType===\'error\' ? \'alert\' : \'status\'"', 'aria-atomic="true"']) assert(messageTag.includes(attribute));
    assert(messageTag.includes(':class="messageType===\'error\' ? \'danger\' : \'success\'"'));
    const workspace = html.slice(html.indexOf('<main id="merge-workspace"'));
    assert(!/<(?:div|p)\b[^>]*v-if="error"/.test(workspace), 'Global errors use the message host without duplicate page banners');
    assert(html.includes('<details v-if="error" class="workspace-details"><summary>最近错误详情</summary><pre class="status-output">{{ error }}</pre></details>'));
    assert(!/autofocus|tabindex|v-if/.test(messageTag));
    assert(!/v-if="notice"|v-if="targetReady"/.test(html + read('schedule.js')));
    assert(read('schedule.js').includes('v-if="error"'));
    const compileErrors = [];
    f.vue.compile(html.slice(html.indexOf('<div id="app"'), html.indexOf('<noscript>')),
      { decodeEntities: text => text, onError: error => compileErrors.push(error.message) });
    assert.deepEqual(compileErrors, []);

    await f.app.showMessage('已复制');
    assert.equal(f.element.open, true); assert.equal(f.app.message.value, '已复制');
    let [firstId, firstTimer] = [...f.timers][0]; assert.equal(firstTimer.ms, 3000);
    await f.app.showMessage('已复制');
    assert.equal(f.shown.length, 2); assert.equal(f.timers.has(firstId), false);
    assert.equal(f.timers.size, 1);
    const secondId = [...f.timers.keys()][0];
    await f.app.showMessage('下一条反馈');
    assert.equal(f.timers.has(secondId), false); assert.equal(f.app.message.value, '下一条反馈');
    f.fire([...f.timers.keys()][0]);
    assert.equal(f.app.message.value, ''); assert.equal(f.element.open, false);
    const older = f.app.showMessage('旧反馈'), newer = f.app.showMessage('最新反馈');
    await Promise.all([older, newer]);
    assert.equal(f.shown.at(-1), '最新反馈'); assert.equal(f.timers.size, 1);

    for (let attempt = 0; attempt < 2; attempt++) {
      await f.app.copyBranch('master'); await f.vue.nextTick();
      assert.match(f.app.message.value, /复制失败/);
      assert.equal(f.app.messageType.value, 'error'); assert.equal(f.element.open, true);
      assert.equal(f.timers.size, 1); assert.equal([...f.timers.values()][0].ms, 3000);
      f.fire([...f.timers.keys()][0]);
      assert.equal(f.app.message.value, ''); assert.equal(f.element.open, false);
    }
    await f.app.showMessage('已复制');
    assert.equal(f.app.messageType.value, 'success');

    await f.schedule.openLink({ work_item_id: '1', name: '测试需求' });
    await f.schedule.saveLink(); await f.vue.nextTick();
    assert.match(f.app.message.value, /已保存本机任务关联/);
    await f.schedule.saveLink(true); await f.vue.nextTick();
    assert.match(f.app.message.value, /已解除本机任务关联/);
    f.app.clearMessage(); f.failBind = true;
    await f.schedule.saveLink(true);
    assert.match(f.schedule.error.value, /关联保存失败/); assert.equal(f.app.message.value, '');
    f.failBind = false;
    f.schedule.editor.value = { work_item_id: '1', node_id: 'front', user: { key: 'me' }, before: {} };
    f.schedule.plan.value = { plan: 'save' }; f.failSave = true;
    await f.schedule.save();
    assert.match(f.schedule.error.value, /保存结果未确认/); assert.equal(f.app.message.value, '');
    assert(f.schedule.editor.value);
    f.failSave = false; f.failList = true; f.schedule.plan.value = { plan: 'save-again' };
    await f.schedule.save(); await f.vue.nextTick();
    assert.match(f.app.message.value, /已保存/); assert.match(f.schedule.error.value, /列表刷新失败/);
    assert.equal(f.schedule.editor.value, null);

    await f.batch.open('all-in');
    f.batch.sources.value = ['refs/heads/task']; f.batch.targets.value = ['refs/heads/pre'];
    f.batch.combinationName.value = '常用'; f.batch.saveCombination(); await f.vue.nextTick();
    assert.match(f.app.message.value, /已保存分支与顺序/); assert.equal(f.batch.combinationNotice.value, '');
    f.app.clearMessage(); f.batch.combinationName.value = '常用'; f.batch.saveCombination();
    assert.match(f.batch.combinationNotice.value, /已有同名组合/); assert.equal(f.app.message.value, '');
    const saved = f.batch.combinations.value[0];
    f.batch.applyCombination(saved); await f.vue.nextTick();
    assert.match(f.app.message.value, /已填入/); assert.equal(f.batch.combinationNotice.value, '');
    f.app.clearMessage(); f.batch.applyCombination({ ...saved, sources: ['refs/heads/deleted'] });
    assert.match(f.batch.combinationNotice.value, /已删除或不可用/); assert.equal(f.app.message.value, '');
    f.batch.removeCombination(saved); await f.vue.nextTick();
    assert.match(f.app.message.value, /已移除/);

    const pending = f.app.showMessage('正在销毁的页面');
    f.close(); await pending;
    assert.equal(f.timers.size, 0); assert.equal(f.app.message.value, ''); assert.equal(f.element.open, false);
    console.log('Message checks passed: three-second top-layer feedback, repeated messages, supersession, unmount cleanup, no focus, schedule success/error separation and batch combination validation.');
  } catch (error) { f.close(); throw error; }
})().catch(error => { console.error(error); process.exitCode = 1; });
