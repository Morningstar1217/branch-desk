/* global Vue */
const SchedulePanel = {
  props: { api: { type: Function, required: true }, confirmAction: { type: Function, required: true }, mergeRelease: { type: Function, required: true }, showMessage: { type: Function, required: true }, workspaceKey: { type: String, default: '' }, spaceId: { type: String, default: '' }, legacySpaceId: { type: String, default: '' }, nodeName: { type: String, default: '前端开发' }, locked: { type: Boolean, default: false }, focusTask: { type: Object, default: null } },
  emits: ['open-branch', 'loaded', 'locked', 'dirty'],
  setup(props, context) {
    const { ref, computed, watch, nextTick, onMounted, onUnmounted } = Vue;
    const today = () => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date());
    const addDays = (value, amount) => {
      const d = new Date(value + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + amount);
      return d.toISOString().slice(0, 10);
    };
    const start = ref(''), end = ref(''), search = ref('');
    const data = ref(null), releases = ref({});
    let scheduleRead = 0;
    const rowFilter = ref('all'), refreshedAt = ref(''), loadedRange = ref(null);
    const scheduleView = ref('schedule'), releaseData = ref(null), releaseStart = ref(''), releaseEnd = ref('');
    const releaseFilter = ref(''), releaseSearch = ref(''), selectedReleases = ref([]), loadedReleaseRange = ref(null);
    const releaseUpdated = ref(''), releaseToday = ref(today());
    const releaseDays = () => {
      const current = releaseToday.value, weekday = new Date(current + 'T12:00:00Z').getUTCDay();
      const monday = addDays(current, -((weekday + 6) % 7));
      return { start: monday, end: addDays(monday, 6), tuesday: addDays(monday, 1), thursday: addDays(monday, 3) };
    };
    const releaseRows = computed(() => (releaseData.value?.rows || []).filter(row =>
      (!releaseFilter.value || row.versions.some(version => version.date === releaseFilter.value)) &&
      `${row.name} ${row.work_item_id} ${row.versions.map(version => `${version.name} ${version.date}`).join(' ')}`
        .toLowerCase().includes(releaseSearch.value.trim().toLowerCase())));
    const releasePresets = computed(() => {
      const days = releaseDays();
      return [{ key: 'week', id: '', label: '本周全部', start: days.start, end: days.end },
        { key: 'tuesday', id: days.tuesday, label: '本周二', start: days.start, end: days.end },
        { key: 'thursday', id: days.thursday, label: '本周四', start: days.start, end: days.end }].map(item => ({ ...item,
          count: loadedReleaseRange.value?.start === days.start && loadedReleaseRange.value?.end === days.end
            ? (releaseData.value?.rows || []).filter(row => !item.id || row.versions.some(version => version.date === item.id)).length : '—' }));
    });
    const draftFields = ['start', 'end', 'points', 'actual'];
    const localDrafts = ref({}), pendingDraft = ref(null), draftWarning = ref(''), draftSaved = ref(false);
    let draftStorageKey = '';
    const busy = ref(false), error = ref(''), editor = ref(null), draft = ref({}), plan = ref(null);
    const development = ref({}), developmentReady = ref(false), developmentError = ref('');
    const links = ref({}), linksReady = ref(false), linksError = ref('');
    const moreId = ref('');
    let moreMenu = null;
    const linkDialog = ref(null), linkRow = ref(null), linkRef = ref(''), linkSearch = ref(''), linkBranches = ref([]), linkExpected = ref('');
    const locked = computed(() => props.locked);
    const linkOptions = computed(() => linkBranches.value.filter(branch =>
      `${branch.name} ${branch.path}`.toLowerCase().includes(linkSearch.value.toLowerCase())));
    const linkValid = computed(() => linkBranches.value.some(branch => branch.ref === linkRef.value));
    const developing = computed(() => Object.values(development.value).some(job => job.status === 'running'));
    let developmentTimer, stopped = false;
    const display = value => value === null || value === undefined || value === '' ? '未填写' : value;
    const formatTotal = value => typeof value === 'number'
      ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 6 }).format(value) : display(value);
    const searchedRows = computed(() => (data.value?.rows || []).filter(row =>
      `${row.name} ${row.node_name} ${row.work_item_id} ${(releases.value[row.work_item_id]?.versions || []).map(version => `${version.name} ${version.date}`).join(' ')}`.toLowerCase().includes(search.value.trim().toLowerCase())));
    const matchesFilter = (row, filter) => filter === 'all' ||
      filter === 'unfinished' && !row.passed ||
      filter === 'today' && row.start && row.end && row.start <= today() && row.end >= today() ||
      filter === 'linked' && linksReady.value && !!links.value[row.work_item_id]?.branch_ref;
    const rowFilters = computed(() => [['all', '全部'], ['unfinished', '未完成'], ['today', '今日排期'], ['linked', '已关联分支']]
      .map(([id, label]) => ({ id, label, count: id === 'linked' && !linksReady.value ? '—' : searchedRows.value.filter(row => matchesFilter(row, id)).length })));
    const rows = computed(() => searchedRows.value.filter(row => matchesFilter(row, rowFilter.value)).sort((a, b) =>
        Number(!!a.passed) - Number(!!b.passed) ||
        (a.start || '9999-12-31').localeCompare(b.start || '9999-12-31')));
    const draftDirty = computed(() => !!editor.value && draftFields.some(key =>
      String(draft.value[key] ?? '') !== String(editor.value.before[key] ?? '')));
    const draftId = value => JSON.stringify([value.work_item_id, value.lookup_node_id || value.node_id]);
    const hasDraft = row => !!localDrafts.value[draftId(row)];
    const validDraftValue = (field, value) => value === '' ||
      (field === 'start' || field === 'end' ? typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) :
        (typeof value === 'number' || typeof value === 'string' && value.trim() && value.length <= 64) && Number.isFinite(Number(value)));
    function readDrafts(user, force = false) {
      const key = props.workspaceKey && props.spaceId && user?.key ? 'branch-desk-schedule-drafts:' + JSON.stringify([props.workspaceKey, props.spaceId, user.key]) : '';
      if (key === draftStorageKey && !force) return;
      draftStorageKey = key; localDrafts.value = {}; draftWarning.value = '';
      if (!key) return;
      try {
        const legacyKey = props.spaceId === props.legacySpaceId ? 'branch-desk-schedule-drafts:' + JSON.stringify([props.workspaceKey, user.key]) : '';
        const raw = localStorage.getItem(key) || (legacyKey && localStorage.getItem(legacyKey)) || '{}';
        if (raw.length > 256000) throw Error('oversized');
        const saved = JSON.parse(raw);
        if (!saved || Array.isArray(saved) || typeof saved !== 'object' || Object.keys(saved).length > 100 ||
            !Object.entries(saved).every(([id, item]) => item && typeof item.work_item_id === 'string' &&
              /^\d{1,20}$/.test(item.work_item_id) && typeof item.node_id === 'string' && item.node_id.length <= 200 &&
              id === draftId(item) && Number.isFinite(item.updated) && item.values &&
              draftFields.every(field => validDraftValue(field, item.values[field])))) throw Error('invalid');
        localDrafts.value = saved;
      } catch { draftStorageKey = ''; draftWarning.value = '本地草稿无法读取，已有存储未覆盖；请保留当前页面或先保存排期。'; }
    }
    function writeDrafts(items) {
      if (!draftStorageKey) return false;
      try {
        if (Object.keys(items).length > 100) throw Error('full');
        localStorage.setItem(draftStorageKey, JSON.stringify(items));
        localDrafts.value = items; draftWarning.value = ''; return true;
      } catch { draftWarning.value = '本地草稿保存失败，刷新或关闭页面可能丢失未提交修改。'; return false; }
    }
    function removeStoredDraft() {
      if (!editor.value) return;
      readDrafts(editor.value.user, true);
      const items = { ...localDrafts.value }; delete items[draftId(editor.value)];
      writeDrafts(items); draftSaved.value = false;
    }
    function persistDraft() {
      if (!editor.value || pendingDraft.value) return;
      if (!draftDirty.value) {
        if (hasDraft(editor.value)) removeStoredDraft();
        draftSaved.value = false; return;
      }
      readDrafts(editor.value.user, true);
      const item = { work_item_id: editor.value.work_item_id, node_id: editor.value.lookup_node_id || editor.value.node_id,
        values: Object.fromEntries(draftFields.map(key => [key, draft.value[key] ?? ''])), updated: Date.now() };
      if (!draftFields.every(field => validDraftValue(field, item.values[field]))) {
        draftSaved.value = false; draftWarning.value = '部分字段暂不能保存为本地草稿，请核对输入并保留当前页面。'; return;
      }
      draftSaved.value = writeDrafts({ ...localDrafts.value, [draftId(item)]: item });
      if (!draftStorageKey) draftWarning.value = '当前无法保存本地草稿，请保留页面或先保存排期。';
    }
    function restoreDraft() {
      if (!pendingDraft.value || busy.value) return;
      const values = Object.fromEntries(draftFields.map(key => [key, pendingDraft.value.values[key]]));
      pendingDraft.value = null; draft.value = values; plan.value = null;
    }
    function discardDraft() {
      if (busy.value) return;
      removeStoredDraft(); pendingDraft.value = null;
    }
    async function requestLeave() {
      if (busy.value || stopped) return false;
      closeMore();
      if (!draftDirty.value) return true;
      persistDraft(); const current = editor.value;
      const accepted = await props.confirmAction({ title: '排期尚未保存到飞书', confirmLabel: '保留修改并切换',
        message: draftSaved.value ? '草稿已保存在本机，尚未写入飞书。切换后保留表单，返回可继续编辑。' :
          '修改会保留在当前页面，但尚未保存为本地草稿。请勿刷新或关闭页面；返回后可继续编辑。' });
      return accepted && !busy.value && !stopped && editor.value === current;
    }
    function closeMore(restoreFocus = false) {
      if (!moreMenu) return;
      const trigger = moreMenu.previousElementSibling;
      moreMenu.hidePopover();
      if (restoreFocus === true && trigger.isConnected) trigger.focus({ preventScroll: true });
    }
    function dismissMore(event) {
      if (event.type !== 'scroll' || !moreMenu?.contains(event.target)) closeMore();
    }
    function moreToggled(event) {
      if (event.newState === 'open') {
        moreMenu = event.currentTarget; moreId.value = moreMenu.id;
        const menu = moreMenu;
        menu.style.visibility = 'hidden';
        requestAnimationFrame(() => positionMore(menu));
        globalThis.addEventListener?.('scroll', dismissMore, true);
        globalThis.addEventListener?.('resize', dismissMore);
      } else if (event.currentTarget === moreMenu) {
        moreMenu = null; moreId.value = '';
        globalThis.removeEventListener?.('scroll', dismissMore, true);
        globalThis.removeEventListener?.('resize', dismissMore);
      }
    }
    function positionMore(menu) {
      if (!menu.matches(':popover-open')) return;
      const trigger = menu.previousElementSibling;
      const rect = trigger.getBoundingClientRect(), gap = 6, edge = 8;
      const below = Math.max(0, innerHeight - rect.bottom - gap - edge);
      const above = Math.max(0, rect.top - gap - edge);
      menu.style.maxHeight = Math.max(below, above) + 'px';
      const { width, height } = menu.getBoundingClientRect();
      menu.style.left = Math.max(edge, Math.min(rect.right - width, innerWidth - width - edge)) + 'px';
      menu.style.top = Math.max(edge, height <= below ? rect.bottom + gap : rect.top - height - gap) + 'px';
      menu.style.visibility = '';
      menu.querySelector('button:not(:disabled), a')?.focus({ preventScroll: true });
    }
    function selectMore(event) {
      if (event.target.closest('button:not(:disabled), a')) closeMore(true);
    }
    async function run(action) {
      if (busy.value) return;
      busy.value = true; error.value = '';
      try { await action(); } catch (e) { error.value = e.message; }
      finally { busy.value = false; }
    }
    async function readSchedule() {
      const request = ++scheduleRead;
      const range = { start: start.value, end: end.value };
      const overview = await props.api('schedule/overview', range);
      if (stopped || request !== scheduleRead) return;
      const rows = overview.rows.filter(row => row.node_name === props.nodeName);
      const total_points = rows.reduce((total, row) => {
        const points = Number(row.points);
        return total + (Number.isFinite(points) ? points : 0);
      }, 0);
      data.value = { ...overview, rows, total_points };
      releases.value = {};
      loadedRange.value = range; refreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
      readDrafts(data.value.user);
      context?.emit('loaded', data.value.rows);
      readReleases(data.value);
    }
    async function readReleases(snapshot) {
      const ids = [...new Set(snapshot.rows.map(row => row.work_item_id))];
      for (let offset = 0; offset < ids.length; offset += 50) {
        if (stopped || data.value !== snapshot) return;
        const batch = ids.slice(offset, offset + 50);
        let items;
        try { items = (await props.api('schedule/releases', { work_item_ids: batch })).items; }
        catch { items = {}; }
        if (stopped || data.value !== snapshot) return;
        releases.value = { ...releases.value, ...Object.fromEntries(batch.map(id => {
          const item = items?.[id];
          return [id, item && ['ready', 'unassigned', 'unavailable'].includes(item.status) && Array.isArray(item.versions) &&
            (item.status !== 'ready' || item.versions.length) ? item : { status: 'unavailable', versions: [] }];
        })) };
      }
    }
    function refresh() { return run(readSchedule); }
    function week(offset = 0) {
      if (busy.value || editor.value) return;
      const current = today(), weekday = new Date(current + 'T12:00:00Z').getUTCDay();
      start.value = addDays(current, -((weekday + 6) % 7) + offset * 7);
      end.value = addDays(start.value, 6);
      return refresh();
    }
    function shiftWeek(amount) {
      if (busy.value || editor.value) return;
      if (!start.value || !end.value) return week(amount);
      start.value = addDays(start.value, amount * 7); end.value = addDays(end.value, amount * 7);
      return refresh();
    }
    function pollDevelopment() {
      clearTimeout(developmentTimer);
      if (!stopped && developing.value) developmentTimer = setTimeout(readDevelopment, 2500);
    }
    async function readDevelopment() {
      try {
        development.value = await props.api('development/status');
        developmentReady.value = true; developmentError.value = '';
      } catch (e) {
        developmentReady.value = false;
        developmentError.value = e.message === '接口不存在' ? '开发功能需重启工具后启用。' : '读取开发状态失败：' + e.message;
      }
      pollDevelopment();
    }
    async function readLinks() {
      try {
        const value = await props.api('task-links');
        links.value = value.items; linksReady.value = !value.warning; linksError.value = value.warning || '';
      } catch (e) { linksReady.value = false; linksError.value = '读取任务关联失败：' + e.message; }
    }
    const releaseBranchIssue = row => {
      if (!linksReady.value) return '关联尚未读取成功';
      const ref = links.value[row.work_item_id]?.branch_ref;
      if (!ref) return '请先关联需求分支';
      if (!ref.startsWith('refs/heads/') || ['refs/heads/master', 'refs/heads/pre'].includes(ref)) return '需关联可合入 pre 的本地需求分支';
      return '';
    };
    const selectableReleases = computed(() => releaseRows.value.filter(row => !releaseBranchIssue(row)));
    const selectedReleaseRows = computed(() => selectableReleases.value.filter(row => selectedReleases.value.includes(row.work_item_id)));
    const selectedReleaseBranches = computed(() => new Set(selectedReleaseRows.value.map(row => links.value[row.work_item_id].branch_ref)).size);
    const allReleasesSelected = computed(() => selectableReleases.value.length > 0 && selectedReleaseRows.value.length === selectableReleases.value.length);
    const releaseRangePending = computed(() => !loadedReleaseRange.value || releaseStart.value !== loadedReleaseRange.value.start || releaseEnd.value !== loadedReleaseRange.value.end);
    const releaseSelectionLocked = computed(() => busy.value || props.locked || !!editor.value || developing.value || releaseRangePending.value);
    watch([releaseRows, releaseStart, releaseEnd, links, linksReady], () => { selectedReleases.value = []; }, { flush: 'sync' });
    async function readReleaseSchedule(range, filter = '') {
      let result;
      try { result = await props.api('schedule/release-overview', range); }
      catch (e) { throw new Error(e.message === '排期接口不存在。' || e.message === '接口不存在' ? '版本上线查询需要重启工作台后台后启用。' : e.message); }
      if (stopped) return;
      releaseData.value = result; loadedReleaseRange.value = range; releaseFilter.value = filter;
      releaseUpdated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
      selectedReleases.value = [];
    }
    function refreshRelease() {
      if (editor.value || props.locked) return;
      return run(async () => {
        await readLinks();
        await readReleaseSchedule({ start: releaseStart.value, end: releaseEnd.value });
      });
    }
    function chooseReleasePreset(preset) {
      if (busy.value || editor.value || props.locked) return;
      releaseToday.value = today();
      preset = releasePresets.value.find(item => item.key === preset.key);
      if (!preset) return;
      releaseStart.value = preset.start; releaseEnd.value = preset.end;
      if (loadedReleaseRange.value?.start === preset.start && loadedReleaseRange.value?.end === preset.end) {
        releaseFilter.value = preset.id; error.value = ''; return;
      }
      return run(async () => {
        await readLinks();
        await readReleaseSchedule({ start: preset.start, end: preset.end }, preset.id);
      });
    }
    async function switchScheduleView(value) {
      if (busy.value || editor.value || props.locked) return;
      closeMore(); releaseToday.value = today(); scheduleView.value = value;
      if (value === 'release' && !releaseData.value) await chooseReleasePreset(releasePresets.value[0]);
    }
    function toggleAllReleases() {
      if (releaseSelectionLocked.value) return;
      selectedReleases.value = allReleasesSelected.value ? [] : selectableReleases.value.map(row => row.work_item_id);
    }
    function mergeSelectedReleases() {
      if (releaseSelectionLocked.value || !selectedReleaseRows.value.length) return;
      if (selectedReleaseBranches.value > 30) { error.value = '每批最多合并 30 个分支，请减少勾选后再试。'; return; }
      const tasks = selectedReleaseRows.value.map(row => ({ work_item_id: row.work_item_id, ref: links.value[row.work_item_id].branch_ref }));
      return run(() => props.mergeRelease(tasks));
    }
    function openLink(row) {
      if (busy.value || props.locked) return;
      return run(async () => {
        const overview = await props.api('overview');
        await readLinks();
        if (!linksReady.value) return;
        linkRow.value = row; linkSearch.value = '';
        linkBranches.value = overview.branches.filter(branch => branch.ref.startsWith('refs/heads/') && branch.branch !== 'master');
        linkExpected.value = links.value[row.work_item_id]?.branch_ref || '';
        const matches = linkBranches.value.filter(branch => branch.branch === 'M-' + row.work_item_id || branch.branch.startsWith('M-' + row.work_item_id + '-'));
        linkRef.value = linkExpected.value || (matches.length === 1 ? matches[0].ref : '');
        linkDialog.value.showModal();
      });
    }
    function saveLink(unlink = false) {
      if (busy.value || props.locked || !linkRow.value || (!unlink && !linkValid.value)) return;
      return run(async () => {
        const value = await props.api('task-links/bind', { work_item_id: linkRow.value.work_item_id,
          ref: unlink ? '' : linkRef.value, expected_ref: linkExpected.value, confirmed: true });
        if (value.warning) throw new Error(value.warning);
        links.value = value.items; linkDialog.value.close();
        props.showMessage(unlink ? '已解除本机任务关联，分支和会话保留。' : '已保存本机任务关联，可直接进入分支查看状态。');
      });
    }
    function openLinkedBranch(row) {
      const ref = links.value[row.work_item_id]?.branch_ref;
      if (!busy.value && !props.locked && ref) context?.emit('open-branch', { work_item_id: row.work_item_id, ref, title: row.name });
    }
    async function startDevelopment(row, restart = false) {
      if (stopped || busy.value || props.locked || !developmentReady.value || developing.value) return;
      const previousUrl = development.value[row.work_item_id]?.url;
      if (restart && !previousUrl) return;
      if (restart) {
        if (!await props.confirmAction({ title: '重新读取需求', confirmLabel: '重新读取并生成计划',
          message: '重新读取此需求并生成新计划？\n将新建 Codex 会话，保留旧会话及已有代码，不自动继续开发。\n若旧会话仍在开发，请先停止旧任务，避免同时修改同一 worktree。' })) return;
        if (stopped || busy.value || props.locked || !developmentReady.value || developing.value || development.value[row.work_item_id]?.url !== previousUrl) return;
      }
      return run(async () => {
        try {
          const request = { work_item_id: row.work_item_id };
          if (restart) request.restart_from = previousUrl;
          const job = await props.api('development/start', request);
          development.value = { ...development.value, [row.work_item_id]: job };
          pollDevelopment();
        } catch (e) {
          await readDevelopment();
          if (!development.value[row.work_item_id]) development.value = { ...development.value,
            [row.work_item_id]: { status: 'blocked', url: '', summary: '读取受阻：' + e.message } };
          throw e;
        }
      });
    }
    async function edit(row) {
      await run(async () => {
        const value = await props.api('schedule/detail', { work_item_id: row.work_item_id, node_id: row.node_id });
        row.url = value.url;
        readDrafts(value.user, true);
        editor.value = { ...value, name: row.name, lookup_node_id: row.node_id };
        pendingDraft.value = localDrafts.value[draftId(editor.value)] || null; draftSaved.value = false;
        draft.value = Object.fromEntries(Object.entries(value.before).map(([key, item]) => [key, item ?? '']));
        plan.value = null;
      });
      if (editor.value) {
        await nextTick();
        document.getElementById('schedule-start')?.focus();
      }
    }
    async function cancel() {
      if (busy.value) return;
      const current = editor.value;
      if (draftDirty.value && !await props.confirmAction({ title: '放弃本次排期修改？', confirmLabel: '放弃修改',
        message: '将删除此节点的本地草稿，飞书排期保持原值。取消可继续编辑。' })) return;
      if (busy.value || stopped || editor.value !== current) return;
      if (!pendingDraft.value) removeStoredDraft();
      editor.value = null; plan.value = null;
      pendingDraft.value = null;
      nextTick(() => document.getElementById('schedule-search')?.focus());
    }
    function openItem(row) {
      if (busy.value) return;
      const page = window.open('about:blank', '_blank');
      if (page) page.opener = null;
      return run(async () => {
        try {
          const value = await props.api('schedule/link', { work_item_id: row.work_item_id });
          row.url = value.url;
          if (page) page.location.replace(value.url);
          else props.showMessage('需求链接已就绪，请点击对应行的“需求详情”。');
        } catch (e) { if (page) page.close(); throw e; }
      });
    }
    function clear() { if (!busy.value && !pendingDraft.value) { draft.value = { start: '', end: '', points: '', actual: '' }; plan.value = null; } }
    function preview() {
      if (!editor.value || pendingDraft.value) return;
      return run(async () => {
        plan.value = null;
        plan.value = await props.api('schedule/preview', { work_item_id: editor.value.work_item_id,
          node_id: editor.value.node_id, fingerprint: editor.value.fingerprint, values: { ...draft.value } });
      });
    }
    function save() {
      if (!plan.value || busy.value) return;
      return run(async () => {
        const id = plan.value.plan; plan.value = null;
        const result = await props.api('schedule/save', { plan: id });
        removeStoredDraft(); pendingDraft.value = null;
        editor.value = null;
        props.showMessage(result.message);
        try { await readSchedule(); }
        catch (e) { error.value = '保存已回读核验，但列表刷新失败：' + e.message; }
      });
    }
    watch(draft, () => { plan.value = null; persistDraft(); }, { deep: true, flush: 'sync' });
    watch(draftDirty, value => context?.emit('dirty', value));
    watch([rows, busy, locked, editor, development], () => closeMore(), { flush: 'sync' });
    watch([busy, editor], () => context?.emit('locked', busy.value || !!editor.value));
    watch(() => props.focusTask, request => {
      if (!request || editor.value) return;
      scheduleView.value = 'schedule'; search.value = request.id;
      rowFilter.value = 'all';
      nextTick(() => document.getElementById('schedule-search')?.focus());
    }, { immediate: true });
    onMounted(async () => { globalThis.addEventListener?.('pagehide', persistDraft); await Promise.all([readDevelopment(), readLinks()]); await week(); });
    onUnmounted(() => { closeMore(); persistDraft(); stopped = true; clearTimeout(developmentTimer); globalThis.removeEventListener?.('pagehide', persistDraft); context?.emit('dirty', false); context?.emit('locked', false); });
    return { start, end, search, data, releases, nodeName: computed(() => props.nodeName),
      scheduleView, releaseData, releaseStart, releaseEnd, releaseFilter, releaseSearch, selectedReleases, loadedReleaseRange, releaseUpdated,
      releaseRows, releasePresets, releaseBranchIssue, selectableReleases, selectedReleaseRows, selectedReleaseBranches, allReleasesSelected,
      releaseRangePending, releaseSelectionLocked, refreshRelease, chooseReleasePreset, switchScheduleView, toggleAllReleases, mergeSelectedReleases,
      rowFilter, rowFilters, refreshedAt, loadedRange, localDrafts, pendingDraft, draftWarning, draftSaved, draftDirty, hasDraft, restoreDraft, discardDraft, requestLeave, moreId, moreToggled, selectMore,
      rows, busy, error, editor, draft, plan, development, developmentReady, developmentError, developing,
      links, linksReady, linksError, linkDialog, linkRow, linkRef, linkSearch, linkExpected, linkOptions, linkValid, locked, openLink, saveLink, openLinkedBranch,
      startDevelopment, display, formatTotal, refresh, week, shiftWeek, edit, cancel, openItem, clear, preview, save };
  },
  template: `
    <main class="schedule-page">
      <dialog ref="linkDialog" class="batch-dialog release-dialog task-link-dialog" aria-labelledby="task-link-heading" @cancel="busy && $event.preventDefault()">
        <div class="preview-heading"><h2 id="task-link-heading">关联需求分支</h2><button @click="linkDialog.close()" :disabled="busy">取消</button></div>
        <p v-if="linkRow" class="wrap">{{ linkRow.name }} <span class="hint">· {{ linkRow.work_item_id }}</span></p>
        <p class="hint">仅保存本机关联。选择分支后点击确认，不创建工作区或执行 Git 写入。</p>
        <p v-if="error" class="notice danger" role="alert">{{ error }}</p>
        <label for="task-branch-search">查找本地分支</label><input id="task-branch-search" v-model="linkSearch" type="search" placeholder="搜索分支或目录" :disabled="busy" autofocus>
        <fieldset class="task-branch-options" :disabled="busy || locked"><legend class="sr-only">选择任务对应的分支</legend>
          <label v-for="branch in linkOptions" :key="branch.ref" class="batch-option" :class="{selected:linkRef===branch.ref}"><input type="radio" name="task-branch" :value="branch.ref" v-model="linkRef"><span>{{ branch.branch }}<small>{{ branch.path || '尚未创建工作区' }}</small></span></label>
          <p v-if="!linkOptions.length" class="hint">没有匹配的本地分支。</p>
        </fieldset>
        <p class="hint wrap">将关联：{{ linkRef.replace('refs/heads/', '') || '请先选择分支' }}</p>
        <div class="page-actions"><button v-if="linkExpected" @click="saveLink(true)" :disabled="busy || locked">解除关联</button><button class="primary" @click="saveLink()" :disabled="busy || locked || !linkValid">确认关联</button></div>
      </dialog>
      <div class="schedule-heading"><div><h1>我的排期</h1><p class="muted">{{ data?.project || '飞书项目' }} · {{ data?.user?.name || '正在确认飞书身份…' }} · 北京时间</p></div><a v-if="data?.project_url" :href="data.project_url + '/overview'" target="_blank" rel="noopener noreferrer">打开飞书工作台 ↗</a></div>
      <div v-if="error" class="notice danger" role="alert">{{ error }}</div>
      <div v-if="developmentError" class="notice warning" role="status">{{ developmentError }}</div>
      <div v-if="linksError" class="notice warning" role="status">{{ linksError }}</div>
      <p v-if="draftWarning" class="notice warning" role="status">{{ draftWarning }}</p>
      <p v-if="focusTask && search===focusTask.id" class="hint" role="status">正在定位需求 {{ focusTask.id }}；搜索仅覆盖已加载的排期，无结果时请调整日期范围后查询。</p>
      <p v-if="developing" class="loading" role="status">Codex 正在对应项目中自动运行本地 skill；读取结束后打开计划会话，集中确认计划与实施权限范围。</p>
      <p v-if="busy" class="loading" role="status">正在读取或保存数据，请稍候…</p>
      <div class="schedule-filters schedule-views" role="group" aria-label="排期视图">
        <button @click="switchScheduleView('schedule')" :aria-pressed="scheduleView==='schedule'" :disabled="busy || locked || !!editor">开发排期</button>
        <button @click="switchScheduleView('release')" :aria-pressed="scheduleView==='release'" :disabled="busy || locked || !!editor">版本上线</button>
      </div>
      <section v-if="scheduleView==='release'" class="panel release-schedule" aria-labelledby="release-schedule-heading">
        <div class="schedule-list-tools"><h2 id="release-schedule-heading">按版本上线时间查看 <span class="badge">{{ releaseRows.length }}</span></h2><label class="sr-only" for="release-search">搜索上线需求或版本</label><input id="release-search" v-model="releaseSearch" type="search" placeholder="搜索需求、编号或版本…" :disabled="busy"></div>
        <div class="schedule-filters" role="group" aria-label="本周上线日期">
          <button v-for="preset in releasePresets" :key="preset.key" @click="chooseReleasePreset(preset)" :aria-pressed="loadedReleaseRange?.start===preset.start && loadedReleaseRange?.end===preset.end && releaseFilter===preset.id" :disabled="busy || locked">{{ preset.label }}<span v-if="preset.id" class="hint">{{ preset.id.slice(5) }}</span><span class="badge">{{ preset.count }}</span></button>
        </div>
        <div class="schedule-toolbar"><label>上线开始<input type="date" v-model="releaseStart" :disabled="busy"></label><label>上线结束<input type="date" v-model="releaseEnd" :disabled="busy"></label><button @click="refreshRelease" :disabled="busy || locked || !releaseStart || !releaseEnd">查询上线需求</button></div>
        <p v-if="releaseRangePending && releaseData" class="hint" role="status">日期已修改，请点击“查询上线需求”更新列表后选择。</p>
        <p class="hint">本人负责{{ nodeName }}的需求，包含往周开发、本次日期范围上线的需求；按飞书版本计划发布日期筛选。</p>
        <div class="release-selection-bar"><label class="schedule-check"><input type="checkbox" :checked="allReleasesSelected" :indeterminate="selectedReleaseRows.length>0 && !allReleasesSelected" @change="toggleAllReleases" :disabled="releaseSelectionLocked || !selectableReleases.length">全选可合并需求</label><span class="hint">已选 {{ selectedReleaseRows.length }} 个需求 · {{ selectedReleaseBranches }} 个分支</span><button class="primary" @click="mergeSelectedReleases" :disabled="releaseSelectionLocked || !selectedReleaseRows.length || selectedReleaseBranches>30">批量合并到 pre</button></div>
        <p v-if="selectedReleaseBranches>30" class="notice warning" role="status">每批最多 30 个分支，请减少勾选。</p>
        <p class="hint">选择后进入批量合并预览，确认后才合并并本地提交；推送仍需单独确认。未关联分支的需求请先关联。</p>
        <div class="release-schedule-table" tabindex="0" role="region" aria-label="上线需求列表，可左右滚动"><table><thead><tr><th aria-label="选择需求"></th><th>需求</th><th>版本上线（计划）</th><th>关联分支</th><th>操作</th></tr></thead><tbody>
          <tr v-for="row in releaseRows" :key="row.work_item_id"><td><input type="checkbox" v-model="selectedReleases" :value="row.work_item_id" :aria-label="'选择需求：'+row.name" :disabled="releaseSelectionLocked || !!releaseBranchIssue(row)"></td><td><strong>{{ row.name }}</strong><div class="hint">{{ row.work_item_id }}</div></td><td><div v-for="version in row.versions" :key="version.id" class="schedule-release-entry"><strong class="schedule-release-date">{{ version.date }}</strong><div class="schedule-release-name">{{ version.name }}</div></div></td><td><span class="release-branch-name">{{ links[row.work_item_id]?.branch_ref?.replace('refs/heads/', '') || '未关联' }}</span><div v-if="releaseBranchIssue(row)" class="hint">{{ releaseBranchIssue(row) }}</div></td><td><div class="schedule-row-actions"><button @click="openLink(row)" :disabled="busy || locked">{{ links[row.work_item_id]?.branch_ref ? '修改关联' : '关联分支' }}</button><a v-if="row.url" :href="row.url" target="_blank" rel="noopener noreferrer">需求详情 ↗</a></div></td></tr>
          <tr v-if="!releaseRows.length"><td colspan="5" class="schedule-no-rows">{{ busy ? '正在读取上线需求…' : !releaseData ? '尚未读取上线需求，请查询或重试。' : '此上线日期和搜索条件下没有需求。' }}</td></tr>
        </tbody></table></div>
        <div v-if="loadedReleaseRange" class="schedule-summary"><span>已读取上线范围 {{ loadedReleaseRange.start }} — {{ loadedReleaseRange.end }}</span><span v-if="releaseFilter">当前筛选 {{ releaseFilter }}</span><span class="range-note">最近读取 {{ releaseUpdated }} · 北京时间</span></div>
      </section>
      <section v-show="scheduleView==='schedule'" class="panel" :inert="!!editor">
        <div class="schedule-toolbar"><div class="week-switch" role="group" aria-label="按周切换"><button @click="shiftWeek(-1)" :disabled="busy">上一周</button><button @click="week()" :disabled="busy">本周</button><button @click="shiftWeek(1)" :disabled="busy">下一周</button></div><label>开始日期<input type="date" v-model="start" :disabled="busy"></label><label>结束日期<input type="date" v-model="end" :disabled="busy"></label><button class="primary" @click="refresh" :disabled="busy || !start || !end">查询排期</button></div>
        <div class="schedule-summary"><template v-if="data"><span>本次{{ nodeName }}估分<b>{{ formatTotal(data.total_points) }}</b></span></template><span v-if="refreshedAt" class="schedule-updated" role="status">最近读取 {{ refreshedAt }}<template v-if="loadedRange"> · {{ loadedRange.start }} — {{ loadedRange.end }}</template></span><span class="range-note">北京时间 · 查询范围最多 90 天</span></div>
      </section>
      <section v-if="editor" class="panel schedule-editor" aria-labelledby="schedule-edit-title">
        <div class="panel-label">填写本人排期 · {{ editor.user.name }}</div><h2 id="schedule-edit-title">{{ editor.name }}</h2><p class="muted">{{ editor.node_name }} · <a :href="editor.url" target="_blank" rel="noopener noreferrer">需求详情 ↗</a></p>
        <div v-if="pendingDraft" class="draft-recovery" role="status"><span>发现此节点的本地草稿。已重新读取飞书当前值，恢复后请核对并重新预览。</span><div><button @click="restoreDraft" :disabled="busy">恢复草稿</button><button @click="discardDraft" :disabled="busy">删除草稿</button></div></div>
        <p v-else-if="draftDirty" class="hint" role="status">{{ draftSaved ? '草稿已保存在本机，尚未写入飞书。' : '修改尚未写入飞书；本地草稿当前不可用。' }}</p>
        <form @submit.prevent="preview">
          <div class="schedule-fields"><label for="schedule-start">开始日期<input id="schedule-start" type="date" v-model="draft.start" :disabled="busy || !!pendingDraft"></label><label for="schedule-end">结束日期<input id="schedule-end" type="date" v-model="draft.end" :disabled="busy || !!pendingDraft"></label><label for="schedule-points">估分<input id="schedule-points" type="number" min="0" step="any" v-model="draft.points" :disabled="busy || !!pendingDraft" placeholder="未填写"></label><label for="schedule-actual">实际工时<input id="schedule-actual" type="number" min="0" step="any" v-model="draft.actual" :disabled="busy || !!pendingDraft" placeholder="未填写"></label></div>
          <p class="hint">估分、实际工时沿用飞书字段的数值单位；0 会保留为 0。日期需同时填写或同时清空。仅修改本人记录。</p>
          <div class="schedule-actions"><button type="button" @click="clear" :disabled="busy || !!pendingDraft">清空本人排期字段</button><button type="button" @click="cancel" :disabled="busy">取消</button><button class="primary" type="submit" :disabled="busy || !!pendingDraft">预览修改</button></div>
        </form>
        <div v-if="plan" class="schedule-confirm"><h2>确认写入飞书</h2><table><thead><tr><th>字段</th><th>原值</th><th>修改为</th></tr></thead><tbody><tr v-for="change in plan.changes" :key="change.field"><th>{{ change.label }}</th><td>{{ display(change.before) }}</td><td :class="{cleared:change.after===null || change.after===''}">{{ change.after===null || change.after==='' ? '清空' : change.after }}</td></tr></tbody></table><p class="hint">预览 5 分钟有效。确认后会写入飞书项目，并重新读取核验。</p><button class="primary" @click="save" :disabled="busy">确认保存到飞书</button></div>
      </section>
      <section v-show="scheduleView==='schedule'" class="panel" :inert="!!editor">
        <div class="schedule-list-tools"><h2>已排期 <span class="badge">{{ rows.length }}</span></h2><label class="sr-only" for="schedule-search">搜索任务、版本或上线日期</label><input id="schedule-search" v-model="search" placeholder="搜索任务、节点、编号、版本或上线日期…" :disabled="busy"></div>
        <div class="schedule-filters" role="group" aria-label="筛选已加载的排期"><button v-for="filter in rowFilters" :key="filter.id" @click="rowFilter=filter.id" :aria-pressed="rowFilter===filter.id" :disabled="busy || (filter.id==='linked' && !linksReady)">{{ filter.label }} <span class="badge">{{ filter.count }}</span></button><span class="hint">仅{{ nodeName }} · 当前已加载日期范围</span></div>
        <p class="table-scroll-hint">左右滚动查看日期、版本上线、估分与操作。</p><div class="schedule-table" tabindex="0" role="region" aria-label="排期列表，可左右滚动查看所有列"><table><thead><tr><th>任务 / 节点</th><th>开始日期</th><th>结束日期</th><th>版本上线（计划）</th><th>估分</th><th>操作</th></tr></thead><tbody><tr v-for="(row,index) in rows" :key="row.work_item_id + ':' + row.node_id + ':' + index"><td><strong>{{ row.name }}</strong><div class="hint">{{ row.work_item_id }} · {{ row.node_name }} <span v-if="row.passed" class="badge">已完成</span></div></td><td>{{ display(row.start) }}</td><td>{{ display(row.end) }}</td><td class="schedule-release">
          <template v-if="releases[row.work_item_id]?.status==='ready'"><div v-for="version in releases[row.work_item_id].versions" :key="version.id" class="schedule-release-entry"><strong v-if="version.date" class="schedule-release-date">{{ version.date }}</strong><span v-else class="schedule-release-state" :class="{'release-unavailable':version.status==='unavailable'}">{{ version.status==='unavailable' ? '读取失败' : '未填写发布日期' }}</span><div class="schedule-release-name">{{ version.name || '版本信息读取失败' }}</div></div></template>
          <span v-else class="schedule-release-state" :class="{'release-unavailable':releases[row.work_item_id]?.status==='unavailable'}">{{ !releases[row.work_item_id] ? '读取中…' : releases[row.work_item_id].status==='unassigned' ? '未关联版本' : '读取失败' }}</span>
        </td><td>{{ display(row.points) }}</td><td><div class="schedule-row-actions">
          <button v-if="row.developable!==false && !development[row.work_item_id]?.url" class="primary" @click="startDevelopment(row)" :disabled="busy || locked || developing || !developmentReady" title="读取需求并生成计划，实施仍需在 Codex 中确认">{{ ['failed','blocked'].includes(development[row.work_item_id]?.status) ? '重试读取需求' : '开发' }}</button>
          <a v-if="development[row.work_item_id]?.url && development[row.work_item_id].status!=='running'" :href="development[row.work_item_id].url">打开开发任务 ↗</a>
          <span v-if="development[row.work_item_id]?.status==='running'" class="task-running" role="status">读取需求中…</span>
          <button v-if="links[row.work_item_id]?.branch_ref" @click="openLinkedBranch(row)" :disabled="busy || locked || !linksReady" :title="links[row.work_item_id].branch_ref.replace('refs/heads/', '')">进入分支</button>
          <div class="task-more"><button type="button" class="task-more-trigger" :id="'schedule-more-trigger-'+index" :aria-label="'更多操作：'+row.name" :aria-expanded="moreId==='schedule-more-'+index" :aria-controls="'schedule-more-'+index" :popovertarget="'schedule-more-'+index" :disabled="busy || locked">更多 <svg class="dropdown-chevron" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></button><div :id="'schedule-more-'+index" class="task-more-content" popover="auto" role="group" :aria-labelledby="'schedule-more-trigger-'+index" @beforetoggle="moreToggled($event)" @click.capture="selectMore($event)">
            <button v-if="row.editable" @click="edit(row)" :disabled="busy || locked">{{ hasDraft(row) ? '填写 / 调整 · 有草稿' : '填写 / 调整' }}</button>
            <button @click="openLink(row)" :disabled="busy || locked">{{ links[row.work_item_id]?.branch_ref ? '修改关联' : '关联分支' }}</button>
            <button v-if="row.developable!==false && development[row.work_item_id]?.url && development[row.work_item_id].status!=='running'" @click="startDevelopment(row,true)" :disabled="busy || locked || developing || !developmentReady" title="新建会话重新读取并生成计划，保留旧会话和已有代码">重新读取需求</button>
            <a v-if="development[row.work_item_id]?.previous_url" :href="development[row.work_item_id].previous_url">上次开发任务 ↗</a>
            <button v-if="row.url.endsWith('/overview')" @click="openItem(row)" :disabled="busy">需求详情 ↗</button><a v-else :href="row.url" target="_blank" rel="noopener noreferrer">需求详情 ↗</a>
            <p v-if="links[row.work_item_id]?.branch_ref" class="hint wrap" :title="links[row.work_item_id].branch_ref.replace('refs/heads/', '')">关联分支：{{ links[row.work_item_id].branch_ref.replace('refs/heads/', '') }}</p>
            <p v-if="!row.editable" class="hint">工作项 / 子任务请在飞书详情操作</p>
            <p v-if="development[row.work_item_id]" class="hint wrap" :title="development[row.work_item_id].summary">{{ development[row.work_item_id].summary }}</p>
          </div></div>
        </div><span v-if="hasDraft(row)" class="draft-badge">本地草稿</span><p v-if="development[row.work_item_id]?.warning" class="hint development-status" role="status">{{ development[row.work_item_id].warning }}</p></td></tr><tr v-if="!rows.length"><td colspan="6" class="schedule-no-rows">{{ busy ? '正在加载…' : rowFilter==='linked' && !linksReady ? '任务关联尚未读取成功，请重试读取关联。' : '没有匹配的'+nodeName+'任务，可调整筛选、搜索或日期范围。' }}</td></tr></tbody></table></div>
      </section>
    </main>`
};
