// Navigation uses the bundled Vue in memory: no browser, Git writes, or service processes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, 'web', name), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
function fixture(pinned = null) {
  const f = { calls: [], storage: new Map(), pinned, failPreferences: false, failTarget: false, projectStatus: 'running', focused: '', services: {}, moduleScroll: { scrollTop: 0 }, events: new Map() };
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
      if (route === 'overview') result = f.overview;
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
(async () => {
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
