// Pure-memory checks using the bundled Vue; no installation required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const vueContext = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, 'web/vendor/vue.global.prod.js'), 'utf8'), vueContext);
const Vue = vueContext.Vue;
const source = fs.readFileSync(path.join(__dirname, 'web/schedule.js'), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
const ids = rows => plain(rows.map(row => row.work_item_id));
const version = (id, date) => ({ id, name: '版本 ' + id, date, status: 'ready' });
const releaseRows = [
  { work_item_id: '101', name: '往周开发本周上线', versions: [version('v1', '2026-10-27'), version('v2', '2026-10-27')] },
  { work_item_id: '102', name: '两个上线日期', versions: [version('v1', '2026-10-27'), version('v3', '2026-10-29')] },
  ...['103', '104', '105', '106', '107'].map(id => ({ work_item_id: id, name: '需求 ' + id, versions: [version('v3', '2026-10-29')] })),
];
const branch = ref => ({ branch_ref: ref });
const links = {
  '101': branch('refs/heads/M-shared'), '102': branch('refs/heads/M-second'),
  '103': branch('refs/heads/pre'), '105': branch('refs/heads/master'),
  '106': branch('refs/remotes/origin/M-remote'), '107': branch('refs/heads/M-shared'),
};
const scheduleRows = [{ work_item_id: '900', node_id: 'front', node_name: '前端开发', name: '本周开发',
  start: '2026-10-26', end: '2026-10-28', points: 0, passed: false,
  url: 'https://project.feishu.cn/platform/story/detail/900', editable: true }];

function fixture(now = '2026-10-26T16:30:00Z') {
  const mounted = [], unmounted = [], calls = [], merges = [], events = [];
  const state = { rows: plain(releaseRows), scheduleRows: plain(scheduleRows), links: plain(links), failLinks: false, failRelease: false, failMerge: false, holdRelease: null };
  const timers = new Map(), storage = new Map();
  let timerId = 0;
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return new Date(now).getTime(); }
  }
  const context = vm.createContext({
    Vue: { ...Vue, onMounted: fn => mounted.push(fn), onUnmounted: fn => unmounted.push(fn) },
    Date: Clock, Intl, document: { getElementById: () => null },
    localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: id => timers.delete(id), addEventListener() {}, removeEventListener() {},
  });
  vm.runInContext(source + '\nthis.component = SchedulePanel;', context);
  const props = Vue.reactive({ locked: false, workspaceKey: '/fixture', nodeName: '前端开发', focusTask: null,
    confirmAction: async () => true,
    mergeRelease: async tasks => {
      if (state.failMerge) throw Error('分支关联已经变化');
      merges.push(plain(tasks));
    },
    api: async (route, payload) => {
      calls.push({ route, payload: payload === undefined ? null : plain(payload) });
      switch (route) {
        case 'development/status': return {};
        case 'task-links':
          if (state.failLinks) throw Error('关联读取失败');
          return { items: plain(state.links), warning: '' };
        case 'schedule/overview': return { user: { key: 'me', name: '本人' }, project: '平台事业部', rows: plain(state.scheduleRows), total_points: 0 };
        case 'schedule/releases': return { items: Object.fromEntries(payload.work_item_ids.map(id => [id, { status: 'unassigned', versions: [] }])) };
        case 'schedule/release-overview':
          if (state.failRelease) throw Error('上线日期未完整读取');
          if (state.holdRelease) await state.holdRelease;
          return { user: { key: 'me', name: '本人' }, project: '平台事业部', rows: plain(state.rows)
            .map(row => ({ ...row, versions: row.versions.filter(item => item.date >= payload.start && item.date <= payload.end) }))
            .filter(row => row.versions.length) };
        default: throw Error('Unexpected API call: ' + route);
      }
    },
  });
  const scope = Vue.effectScope();
  const ui = scope.run(() => context.component.setup(props, { emit: (...event) => events.push(event) }));
  return { ui, props, state, calls, merges, events, component: context.component, setNow(value) { now = value; },
    async mount() { for (const fn of mounted) await fn(); await Vue.nextTick(); },
    close() { for (const fn of unmounted) fn(); scope.stop(); },
  };
}

let checks = 0;
async function check(name, run) { await run(); checks++; console.log('ok - ' + name); }
async function withFixture(run, now) { const f = fixture(now); try { await run(f); } finally { f.close(); } }

(async () => {
  await check('Vue templates compile', () => withFixture(({ component }) => {
    const errors = [];
    assert(!/&#?\w+;/.test(component.template));
    Vue.compile(component.template, { decodeEntities: text => text, onError: error => errors.push(error.message) });
    const html = fs.readFileSync(path.join(__dirname, 'web/index.html'), 'utf8');
    Vue.compile(html.slice(html.indexOf('<div id="app"'), html.indexOf('<noscript>')), { decodeEntities: text => text, onError: error => errors.push(error.message) });
    assert.deepEqual(errors, []);
  }));

  await check('Beijing week boundaries across month/year and Sunday midnight', async () => {
    for (const [now, start, tuesday, thursday, end] of [
      ['2026-10-26T16:30:00Z', '2026-10-26', '2026-10-27', '2026-10-29', '2026-11-01'],
      ['2026-12-31T17:00:00Z', '2026-12-28', '2026-12-29', '2026-12-31', '2027-01-03'],
      ['2027-01-03T15:59:59Z', '2026-12-28', '2026-12-29', '2026-12-31', '2027-01-03'],
      ['2027-01-03T16:00:00Z', '2027-01-04', '2027-01-05', '2027-01-07', '2027-01-10'],
    ]) await withFixture(({ ui }) => {
      assert.deepEqual(plain(ui.releasePresets.value.map(item => [item.start, item.id, item.end])),
        [[start, '', end], [start, tuesday, end], [start, thursday, end]]);
      assert.deepEqual(plain(ui.releasePresets.value.map(item => item.count)), ['—', '—', '—']);
    }, now);
  });

  await check('a mounted page resolves this week again after the week changes', () => withFixture(async f => {
    await f.ui.switchScheduleView('release');
    const previousTuesday = f.ui.releasePresets.value[1];
    f.setNow('2026-11-01T16:01:00Z');
    await f.ui.chooseReleasePreset(previousTuesday);
    assert.equal(f.ui.releaseFilter.value, '2026-11-03');
    assert.deepEqual(plain(f.ui.loadedReleaseRange.value), { start: '2026-11-02', end: '2026-11-08' });
    f.ui.toggleAllReleases();
    f.ui.releaseStart.value = '2026-11-04';
    assert.equal(f.ui.releaseSelectionLocked.value, true);
    assert.deepEqual(plain(f.ui.selectedReleases.value), []);
    await f.ui.mergeSelectedReleases(); assert.equal(f.merges.length, 0);
  }));

  await check('release query includes older development and counts each multi-version requirement once', () => withFixture(async f => {
    await f.mount();
    assert.deepEqual(ids(f.ui.rows.value), ['900']);
    f.ui.search.value = '本周'; f.ui.rowFilter.value = 'unfinished';
    const originalData = f.ui.data.value, originalRange = plain(f.ui.loadedRange.value);
    await f.ui.switchScheduleView('release');
    assert.deepEqual(f.calls.find(call => call.route === 'schedule/release-overview').payload, { start: '2026-10-26', end: '2026-11-01' });
    assert.deepEqual(ids(f.ui.releaseRows.value), ['101', '102', '103', '104', '105', '106', '107']);
    assert.deepEqual(plain(f.ui.releasePresets.value.map(item => item.count)), [7, 2, 6]);
    await f.ui.chooseReleasePreset(f.ui.releasePresets.value[1]);
    assert.deepEqual(ids(f.ui.releaseRows.value), ['101', '102']);
    await f.ui.chooseReleasePreset(f.ui.releasePresets.value[2]);
    assert.deepEqual(ids(f.ui.releaseRows.value), ['102', '103', '104', '105', '106', '107']);
    assert.equal(f.calls.filter(call => call.route === 'schedule/release-overview').length, 1);
    await f.ui.switchScheduleView('schedule');
    assert.equal(f.ui.data.value, originalData);
    assert.equal(f.ui.search.value, '本周'); assert.equal(f.ui.rowFilter.value, 'unfinished');
    assert.deepEqual(plain(f.ui.loadedRange.value), originalRange);
  }));
  await check('configured node filters a different Feishu project without silently emptying its schedule', () => withFixture(async f => {
    f.props.nodeName = '后端开发';
    f.state.scheduleRows = [{ ...scheduleRows[0], work_item_id: '901', node_name: '后端开发' }, scheduleRows[0]];
    await f.mount();
    assert.deepEqual(ids(f.ui.rows.value), ['901']);
    assert.equal(f.ui.nodeName.value, '后端开发');
  }));

  await check('custom date range and return to current week', () => withFixture(async f => {
    await f.ui.switchScheduleView('release');
    f.ui.releaseStart.value = '2026-10-29'; f.ui.releaseEnd.value = '2026-10-29';
    await f.ui.refreshRelease();
    assert.equal(f.ui.releaseFilter.value, '');
    assert.deepEqual(plain(f.ui.loadedReleaseRange.value), { start: '2026-10-29', end: '2026-10-29' });
    assert.deepEqual(ids(f.ui.releaseRows.value), ['102', '103', '104', '105', '106', '107']);
    assert.deepEqual(plain(f.ui.releasePresets.value.map(item => item.count)), ['—', '—', '—']);
    await f.ui.chooseReleasePreset(f.ui.releasePresets.value[1]);
    assert.deepEqual(ids(f.ui.releaseRows.value), ['101', '102']);
    assert.equal(f.ui.releaseStart.value, '2026-10-26');
  }));

  await check('selection excludes unlinked/master/pre/remote and resets after search, date or link changes', () => withFixture(async f => {
    await f.ui.switchScheduleView('release');
    assert.deepEqual(ids(f.ui.selectableReleases.value), ['101', '102', '107']);
    for (const id of ['103', '104', '105', '106']) assert(f.ui.releaseBranchIssue(f.ui.releaseRows.value.find(row => row.work_item_id === id)));
    f.ui.toggleAllReleases(); assert.equal(f.ui.allReleasesSelected.value, true);
    assert.equal(f.ui.selectedReleaseBranches.value, 2);
    f.ui.releaseSearch.value = '往周';
    assert.deepEqual(ids(f.ui.releaseRows.value), ['101']); assert.deepEqual(plain(f.ui.selectedReleases.value), []);
    f.ui.toggleAllReleases(); f.ui.releaseSearch.value = 'v3';
    assert.deepEqual(plain(f.ui.selectedReleases.value), []);
    f.ui.releaseSearch.value = ''; f.ui.toggleAllReleases();
    await f.ui.chooseReleasePreset(f.ui.releasePresets.value[1]);
    assert.deepEqual(plain(f.ui.selectedReleases.value), []);
    f.ui.toggleAllReleases(); f.ui.links.value = { ...f.ui.links.value, '101': branch('refs/heads/M-changed') };
    assert.deepEqual(plain(f.ui.selectedReleases.value), []);
  }));

  await check('merge callback receives selected requirements with shared branches and errors stay visible', () => withFixture(async f => {
    await f.ui.switchScheduleView('release'); f.ui.toggleAllReleases();
    await f.ui.mergeSelectedReleases();
    assert.deepEqual(f.merges, [[{ work_item_id: '101', ref: 'refs/heads/M-shared' },
      { work_item_id: '102', ref: 'refs/heads/M-second' }, { work_item_id: '107', ref: 'refs/heads/M-shared' }]]);
    assert.equal(f.ui.selectedReleaseBranches.value, 2);
    f.state.failMerge = true; await f.ui.mergeSelectedReleases();
    assert.match(f.ui.error.value, /分支关联已经变化/);
    assert.equal(f.ui.busy.value, false);
    assert(f.calls.every(call => ['task-links', 'schedule/release-overview'].includes(call.route)));
  }));

  await check('limit is 30 unique branches, not 30 requirements', () => withFixture(async f => {
    f.state.rows = Array.from({ length: 31 }, (_, i) => ({ work_item_id: String(1000 + i), name: '需求 ' + i, versions: [version('v1', '2026-10-27')] }));
    f.state.links = Object.fromEntries(f.state.rows.map(row => [row.work_item_id, branch('refs/heads/M-' + row.work_item_id)]));
    await f.ui.switchScheduleView('release'); f.ui.toggleAllReleases();
    assert.equal(f.ui.selectedReleaseBranches.value, 31);
    await f.ui.mergeSelectedReleases(); assert.equal(f.merges.length, 0); assert.match(f.ui.error.value, /30/);
    f.ui.links.value = { ...f.ui.links.value, '1030': branch('refs/heads/M-1000') };
    f.ui.toggleAllReleases(); assert.equal(f.ui.selectedReleaseBranches.value, 30);
    await f.ui.mergeSelectedReleases(); assert.equal(f.merges[0].length, 31);
  }));

  await check('failed query is not an empty result and failed links prevent selection', () => withFixture(async f => {
    f.state.failRelease = true; await f.ui.switchScheduleView('release');
    assert.equal(f.ui.releaseData.value, null); assert.match(f.ui.error.value, /未完整读取/);
    assert.deepEqual(plain(f.ui.releasePresets.value.map(item => item.count)), ['—', '—', '—']);
    f.state.failRelease = false; await f.ui.refreshRelease();
    const prior = f.ui.releaseData.value, priorRange = plain(f.ui.loadedReleaseRange.value);
    f.state.failRelease = true; f.ui.releaseStart.value = '2026-10-30'; await f.ui.refreshRelease();
    assert.equal(f.ui.releaseData.value, prior); assert.deepEqual(plain(f.ui.loadedReleaseRange.value), priorRange);
    assert.match(f.ui.error.value, /未完整读取/);
    f.state.failRelease = false; f.state.failLinks = true;
    f.ui.releaseStart.value = '2026-10-26'; await f.ui.refreshRelease();
    assert.equal(f.ui.linksReady.value, false); assert.match(f.ui.linksError.value, /读取失败/);
    assert.equal(f.ui.selectableReleases.value.length, 0); assert.equal(f.ui.selectedReleaseRows.value.length, 0);
  }));

  await check('busy, editor, global lock and development prevent selecting or submitting', () => withFixture(async f => {
    await f.ui.switchScheduleView('release');
    for (const set of [
      value => { f.ui.busy.value = value; },
      value => { f.props.locked = value; },
      value => { f.ui.editor.value = value ? { before: {}, work_item_id: '900', node_id: 'front', user: { key: 'me' } } : null; },
      value => { f.ui.development.value = value ? { '900': { status: 'running' } } : {}; },
    ]) {
      set(true); assert.equal(f.ui.releaseSelectionLocked.value, true);
      f.ui.toggleAllReleases(); assert.equal(f.ui.selectedReleases.value.length, 0);
      f.ui.selectedReleases.value = ['101']; await f.ui.mergeSelectedReleases();
      assert.equal(f.merges.length, 0); f.ui.selectedReleases.value = []; set(false);
    }
    let resolve;
    f.state.holdRelease = new Promise(done => { resolve = done; });
    const pending = f.ui.refreshRelease(); await new Promise(setImmediate);
    const requests = f.calls.length;
    await f.ui.refreshRelease(); await f.ui.chooseReleasePreset(f.ui.releasePresets.value[1]);
    await f.ui.switchScheduleView('schedule');
    assert.equal(f.calls.length, requests); assert.equal(f.ui.scheduleView.value, 'release');
    resolve(); await pending; assert.equal(f.ui.busy.value, false);
  }));
  console.log(`Schedule UI: ${checks} checks passed; no browser, server, network or Git writes.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
