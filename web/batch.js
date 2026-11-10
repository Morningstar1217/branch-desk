/* global Vue */
window.BatchDialog = (() => {
  const { ref, computed, watch, nextTick, onMounted, onUnmounted } = Vue;
  return {
    props: ['api', 'confirmAction', 'showMessage', 'overview', 'disabled', 'selectedPath'],
    emits: ['locked', 'changed'],
    setup(props, { emit }) {
      const modal = ref(null), body = ref(null), mode = ref('all-in'), sources = ref([]), targets = ref([]);
      const sourceSearch = ref(''), targetSearch = ref(''), preview = ref(null);
      const job = ref(null), screen = ref('edit'), loading = ref(false), error = ref(''), pushPreview = ref(null), resumePreview = ref(null);
      const rowAction = ref(null);
      const combinations = ref([]), combinationName = ref(''), combinationNotice = ref(''), combinationWarning = ref('');
      let combinationRoot = '';
      let timer, stopped = false;
      const queueRunning = computed(() => job.value?.status === 'running' || job.value?.push?.status === 'running');
      const running = computed(() => queueRunning.value || !!job.value?.rows?.some(row => row.repair?.status === 'running'));
      const title = computed(() => mode.value === 'all-in' ? '批量合并' : '批量同步');
      const sourceOptions = computed(() => props.overview.refs.filter(r => r.name.toLowerCase().includes(sourceSearch.value.toLowerCase())));
      const targetOptions = computed(() => (props.overview.branches || []).filter(r => r.ref && r.name.toLowerCase().includes(targetSearch.value.toLowerCase())));
      const ready = computed(() => sources.value.length && targets.value.length && sources.value.length <= 30 && targets.value.length <= 30);
      const step = computed(() => screen.value === 'job' ? resumePreview.value ? '确认修复并继续' : pushPreview.value ? '确认推送' : '执行结果' : preview.value ? '预览确认' : '选择与排序');
      const label = value => value.replace(/^refs\/(heads|remotes)\//, '');
      const statusLabel = value => ({ pending: '未执行', running: '执行中', succeeded: '已完成', skipped: '已包含', failed: '失败', stopped: '已停止' })[value] || value;
      function readCombinations() {
        combinationRoot = props.overview.root; combinations.value = []; combinationNotice.value = ''; combinationWarning.value = '';
        if (!combinationRoot) return;
        try {
          const saved = JSON.parse(localStorage.getItem('branch-desk-combinations:' + combinationRoot) || '[]');
          const validRefs = values => Array.isArray(values) && values.length > 0 && values.length <= 30 &&
            values.every(value => typeof value === 'string' && /^refs\/(heads|remotes)\/.{1,1024}$/.test(value)) && new Set(values).size === values.length;
          if (!Array.isArray(saved) || saved.length > 8 || !saved.every(item => item && typeof item.name === 'string' && item.name.trim() && item.name.length <= 40 &&
              ['all-in','sync'].includes(item.mode) && validRefs(item.sources) && validRefs(item.targets) &&
              (item.mode === 'all-in' ? item.targets.length === 1 : item.sources.length === 1)) || new Set(saved.map(item => item.name)).size !== saved.length) throw new Error('Invalid combinations');
          combinations.value = saved.map(({name,mode,sources,targets}) => ({name,mode,sources,targets}));
        } catch { combinationWarning.value = '常用组合无法读取；仍可手动选择分支，已有存储不会自动覆盖。'; }
      }
      function storeCombinations(items) {
        if (!combinationRoot || combinationRoot !== props.overview.root || combinationWarning.value) return false;
        try { localStorage.setItem('branch-desk-combinations:' + combinationRoot, JSON.stringify(items)); combinations.value = items; return true; }
        catch { combinationWarning.value = '浏览器未允许保存常用组合；手动合并不受影响。'; return false; }
      }
      function saveCombination() {
        if (loading.value || running.value || props.disabled || !ready.value) return;
        const name = combinationName.value.trim(); combinationNotice.value = '';
        if (!name || name.length > 40) { combinationNotice.value = '请填写 1–40 字的组合名称。'; return; }
        if (combinations.value.some(item => item.name === name)) { combinationNotice.value = '已有同名组合，请更换名称或先移除旧组合。'; return; }
        if (combinations.value.length >= 8) { combinationNotice.value = '最多保存 8 个组合，请先移除不用的组合。'; return; }
        if (storeCombinations([...combinations.value, { name, mode: mode.value, sources: [...sources.value], targets: [...targets.value] }])) {
          combinationName.value = ''; props.showMessage('已保存分支与顺序，使用时仍须重新预览确认。');
        }
      }
      function applyCombination(item) {
        if (loading.value || running.value || props.disabled) return;
        preview.value = null; pushPreview.value = null; combinationNotice.value = '';
        const missing = [...item.sources.filter(ref => !props.overview.refs.some(value => value.ref === ref)),
          ...item.targets.filter(ref => !props.overview.branches?.some(value => value.ref === ref && !value.missing))];
        if (missing.length) { combinationNotice.value = '组合包含已删除或不可用的分支：' + missing.map(label).join('、') + '。请手动调整后另存。'; return; }
        mode.value = item.mode; sources.value = [...item.sources]; targets.value = [...item.targets];
        sourceSearch.value = ''; targetSearch.value = '';
        props.showMessage('已填入分支与顺序，请预览最新状态后再确认。');
      }
      function removeCombination(item) {
        if (loading.value || running.value || props.disabled) return;
        if (storeCombinations(combinations.value.filter(value => value.name !== item.name))) {
          combinationNotice.value = ''; props.showMessage('已移除本机保存的组合，分支不变。');
        }
      }
      watch([sources, targets], () => { preview.value = null; });
      watch(step, async () => {
        rowAction.value = null;
        await nextTick();
        if (modal.value?.open && body.value) { body.value.scrollTop = 0; body.value.focus(); }
      });

      function toggle(list, value, multiple) {
        if (loading.value) return;
        const selected = list === 'sources' ? sources : targets;
        selected.value = multiple ? selected.value.includes(value) ? selected.value.filter(r => r !== value) : [...selected.value, value] : [value];
      }
      function move(list, index, delta) {
        const selected = list === 'sources' ? sources : targets;
        const values = [...selected.value], to = index + delta;
        if (to < 0 || to >= values.length || loading.value) return;
        [values[index], values[to]] = [values[to], values[index]];
        selected.value = values;
      }
      function setJob(value) {
        const wasRunning = running.value;
        if (job.value?.id !== value?.id) rowAction.value = null;
        if (job.value?.id !== value?.id || value?.push?.status === 'running') pushPreview.value = null;
        if (job.value?.id !== value?.id || !['failed','stopped'].includes(value?.status) || value?.rows?.some(row => row.repair?.status === 'running')) resumePreview.value = null;
        job.value = value;
        emit('locked', running.value);
        if (wasRunning && !running.value) emit('changed', value?.push || value);
      }
      async function poll() {
        clearTimeout(timer);
        try {
          setJob(await props.api('batch/status'));
          if (error.value.startsWith('读取批量状态失败：')) error.value = '';
        } catch (e) { error.value = '读取批量状态失败：' + e.message; }
        if (!stopped && running.value) timer = setTimeout(poll, 2000);
      }
      async function open(value, selection = null) {
        if (stopped || props.disabled || loading.value || modal.value?.open) {
          if (selection) throw new Error('批量入口暂不可用，请先完成或关闭当前操作。');
          return;
        }
        const root = props.overview.root;
        loading.value = true; error.value = '';
        try {
          await poll();
          if (stopped || props.disabled || props.overview.root !== root || modal.value?.open) {
            if (selection) throw new Error('工作台状态已变化，请重新选择上线需求。');
            return;
          }
          if (selection && error.value) throw new Error(error.value);
          if (running.value) {
            mode.value = job.value.mode; screen.value = 'job';
            if (selection) error.value = '已有批量任务正在执行，本次上线需求尚未填入，请待当前任务结束。';
          } else {
            if (selection && (selection.sources.some(ref => !props.overview.refs.some(branch => branch.ref === ref)) ||
                selection.targets.some(ref => !props.overview.branches?.some(branch => branch.ref === ref && !branch.missing)))) {
              throw new Error('所选来源或 pre 分支已不可用，请刷新后重新选择。');
            }
            readCombinations(); combinationName.value = '';
            mode.value = value; screen.value = 'edit'; preview.value = null; pushPreview.value = null; resumePreview.value = null;
            sourceSearch.value = ''; targetSearch.value = '';
            sources.value = selection ? [...selection.sources] : value === 'sync' && props.overview.refs.some(r => r.ref === 'refs/remotes/origin/master') ? ['refs/remotes/origin/master'] : [];
            const current = props.overview.branches?.find(r => r.path === props.selectedPath && r.branch && r.ref);
            targets.value = selection ? [...selection.targets] : value === 'all-in' && current ? [current.ref] : [];
          }
          modal.value.showModal();
        } finally { loading.value = false; }
      }
      async function generate() {
        if (!ready.value || loading.value) return;
        loading.value = true; error.value = ''; preview.value = null;
        try { preview.value = await props.api('batch/preview', { mode: mode.value, sources: sources.value, targets: targets.value }); }
        catch (e) { error.value = e.message; }
        finally { loading.value = false; }
      }
      function backToEdit() {
        if (loading.value) return;
        preview.value = null; error.value = '';
      }
      async function start() {
        if (!preview.value || loading.value) return;
        loading.value = true; error.value = '';
        const id = preview.value.plan; preview.value = null;
        try {
          setJob(await props.api('batch/start', { plan: id, auto_commit: true }));
          screen.value = 'job';
          await poll();
        } catch (e) {
          error.value = e.message;
          await poll();
          if (running.value) screen.value = 'job';
        } finally { loading.value = false; }
      }
      async function cancel() {
        if (!queueRunning.value || loading.value) return;
        const id = job.value.id;
        if (!await props.confirmAction({ title: '停止后续批量任务', confirmLabel: '停止后续任务',
          message: '停止后续批量任务？已完成的提交和推送不会回退，当前 Git 命令或冲突处理的现场会保留。' })) return;
        if (!queueRunning.value || loading.value || job.value.id !== id) return;
        loading.value = true; error.value = '';
        try { setJob(await props.api('batch/cancel', { id })); }
        catch (e) { error.value = e.message; }
        finally { loading.value = false; }
      }
      function recoverable(index) {
        return ['failed','stopped'].includes(job.value?.status) && ['failed','stopped'].includes(job.value.rows[index]?.status) && job.value.rows[index]?.recoverable;
      }
      async function repair(index) {
        if (running.value || loading.value || props.disabled || !recoverable(index)) return;
        const id = job.value.id, target = job.value.rows[index].target;
        loading.value = true; error.value = ''; resumePreview.value = null;
        rowAction.value = { index, label: '等待确认修复…' };
        try {
          if (!await props.confirmAction({ title: '使用 Codex 修复失败项', confirmLabel: '开始修复',
            message: '将在 ' + target + ' 的现有工作区中分析失败原因，仅修复本次问题相关代码；不提交、不推送。已完成项和当前合并现场会保留，修复后仍需检查差异并确认继续。' })) { rowAction.value = null; return; }
          if (running.value || props.disabled || job.value?.id !== id || !recoverable(index)) { rowAction.value = null; return; }
          rowAction.value = { index, label: '正在启动 Codex 修复…' };
          setJob(await props.api('batch/repair', { id, index }));
          await poll();
        } catch (e) { error.value = e.message; await poll(); }
        finally { loading.value = false; }
      }
      async function previewResume(index) {
        if (running.value || loading.value || props.disabled || !recoverable(index)) return;
        loading.value = true; error.value = ''; resumePreview.value = null;
        rowAction.value = { index, label: '正在检查修复结果…' };
        try { resumePreview.value = await props.api('batch/resume/preview', { id: job.value.id }); }
        catch (e) { error.value = e.message; }
        finally { loading.value = false; }
      }
      async function resume() {
        if (!resumePreview.value || running.value || loading.value || props.disabled) return;
        const plan = resumePreview.value.plan; resumePreview.value = null;
        loading.value = true; error.value = '';
        try { setJob(await props.api('batch/resume', { plan })); await poll(); }
        catch (e) { error.value = e.message; await poll(); }
        finally { loading.value = false; }
      }
      async function previewPush() {
        if (running.value || loading.value || job.value?.status !== 'succeeded') return;
        loading.value = true; error.value = ''; pushPreview.value = null;
        try { pushPreview.value = await props.api('batch/push/preview', { id: job.value.id }); }
        catch (e) { error.value = e.message; }
        finally { loading.value = false; }
      }
      async function push() {
        if (!pushPreview.value || running.value || loading.value) return;
        const plan = pushPreview.value.plan; pushPreview.value = null;
        loading.value = true; error.value = '';
        try { setJob(await props.api('batch/push', { plan })); await poll(); }
        catch (e) { error.value = e.message; await poll(); }
        finally { loading.value = false; }
      }
      function showJob() {
        if (loading.value) return;
        screen.value = 'job'; mode.value = job.value.mode;
        if (!modal.value.open) modal.value.showModal();
      }
      onMounted(poll);
      onUnmounted(() => { stopped = true; clearTimeout(timer); });
      return { modal, body, step, mode, sources, targets, sourceSearch, targetSearch, preview, pushPreview, resumePreview, job, screen, loading, error, rowAction,
        combinations, combinationName, combinationNotice, combinationWarning, saveCombination, applyCombination, removeCombination,
        running, queueRunning, title, sourceOptions, targetOptions, ready, label, statusLabel, toggle, move, open, generate, backToEdit, start, cancel, repair, previewResume, resume, previewPush, push, showJob, poll };
    },
    template: `
      <dialog ref="modal" class="batch-dialog batch-workflow" aria-labelledby="batch-title">
        <div class="batch-header">
          <div class="preview-heading"><div><h2 id="batch-title">{{ title }}</h2></div><button @click="modal.close()">关闭</button></div>
          <p class="muted">{{ mode==='all-in' ? '多个来源分支 → 一个目标分支，按所选顺序依次合并。' : '一个来源分支 → 多个目标分支，按所选顺序逐个同步。' }}</p>
          <ol class="batch-steps" aria-label="批量合并步骤"><li :aria-current="screen==='edit' && !preview ? 'step' : undefined"><span>1</span>选择与排序</li><li :aria-current="screen==='edit' && preview ? 'step' : undefined"><span>2</span>预览确认</li><li :aria-current="screen==='job' ? 'step' : undefined"><span>3</span>执行结果</li></ol>
        </div>
        <div ref="body" class="batch-body" tabindex="-1" role="region" :aria-label="step">
          <div v-if="error && !rowAction" class="notice danger" role="alert">{{ error }}</div>
          <p v-if="loading && !rowAction" class="loading" role="status">正在核对，请稍候…</p>
          <template v-if="screen==='edit'">
            <div v-if="!preview" class="batch-config">
              <details class="batch-combinations batch-card"><summary>常用组合 <span class="badge">{{ combinations.length }} / 8</span></summary>
                <p class="hint">仅在本浏览器按仓库保存分支与执行顺序；每次使用都要重新预览确认。</p>
                <p v-if="combinationWarning" class="notice warning" role="status">{{ combinationWarning }}</p>
                <p v-if="combinationNotice" class="hint wrap" role="status">{{ combinationNotice }}</p>
                <ul><li v-for="item in combinations" :key="item.name"><span class="wrap"><strong>{{ item.name }}</strong><small class="hint">{{ item.mode==='all-in' ? '批量合并' : '多分支同步' }} · {{ item.sources.length }} → {{ item.targets.length }}</small></span><button @click="applyCombination(item)" :disabled="loading || running || disabled">填入</button><button @click="removeCombination(item)" :aria-label="'移除组合 '+item.name" :disabled="loading || running || disabled">移除</button></li></ul>
                <div class="combination-save"><input v-model="combinationName" maxlength="40" aria-label="常用组合名称" placeholder="组合名称，例如 日常同步" :disabled="loading || !!combinationWarning"><button @click="saveCombination" :disabled="loading || !ready || !!combinationWarning || !combinationName.trim()">保存当前组合</button></div>
              </details>
              <div class="batch-pickers">
                <section class="batch-card"><div class="batch-card-heading"><h3>{{ mode==='all-in' ? '来源分支（多选）' : '来源分支（单选）' }}</h3><span class="badge">已选 {{ sources.length }}</span></div>
                  <input v-model="sourceSearch" aria-label="搜索批量来源分支" placeholder="搜索本地 / 远端来源…" :disabled="loading">
                  <div class="batch-options" tabindex="0" role="region" aria-label="来源分支列表"><label v-for="r in sourceOptions" :key="r.ref" class="batch-option" :class="{selected:sources.includes(r.ref)}"><input :type="mode==='all-in' ? 'checkbox' : 'radio'" name="batch-source" :checked="sources.includes(r.ref)" :disabled="loading" @change="toggle('sources',r.ref,mode==='all-in')"><span>{{ r.name }}</span></label><p v-if="!sourceOptions.length" class="muted">没有匹配的分支</p></div>
                </section>
                <section class="batch-card"><div class="batch-card-heading"><h3>{{ mode==='sync' ? '目标分支（多选）' : '目标分支（单选）' }}</h3><span class="badge">已选 {{ targets.length }}</span></div>
                  <input v-model="targetSearch" aria-label="搜索批量目标分支" placeholder="搜索目标分支…" :disabled="loading">
                  <div class="batch-options" tabindex="0" role="region" aria-label="目标分支列表"><label v-for="r in targetOptions" :key="r.ref" class="batch-option" :class="{selected:targets.includes(r.ref),muted:r.missing}"><input :type="mode==='sync' ? 'checkbox' : 'radio'" name="batch-target" :checked="targets.includes(r.ref)" :disabled="loading || r.missing" @change="toggle('targets',r.ref,mode==='sync')"><span>{{ r.name }}<small>{{ r.missing ? '工作区目录不存在' : r.path ? '复用现有 worktree' : '执行时创建独立 worktree' }}</small></span></label><p v-if="!targetOptions.length" class="muted">没有匹配的分支</p></div>
                </section>
              </div>
              <section class="batch-order batch-card"><div class="batch-card-heading"><h3>执行顺序</h3><span class="badge">{{ (mode==='all-in' ? sources : targets).length }} 项</span></div><p class="hint">按点击顺序加入，用箭头调整。</p>
                <ol tabindex="0" aria-label="执行顺序列表"><li v-for="(r,index) in (mode==='all-in' ? sources : targets)" :key="r"><span>{{ label(r) }}</span><div class="batch-order-actions"><button @click="move(mode==='all-in' ? 'sources' : 'targets',index,-1)" :disabled="loading || index===0" :aria-label="'上移 '+label(r)">↑</button><button @click="move(mode==='all-in' ? 'sources' : 'targets',index,1)" :disabled="loading || index===(mode==='all-in' ? sources : targets).length-1" :aria-label="'下移 '+label(r)">↓</button></div></li><li v-if="!(mode==='all-in' ? sources : targets).length" class="batch-empty">{{ mode==='all-in' ? '选择来源分支后显示合入顺序' : '选择目标分支后显示同步顺序' }}</li></ol>
              </section>
              <p class="hint batch-selection-note">origin/master 可作为来源，本地 master 可作为合入目标并创建本地提交；master 仍禁止推送。引用使用本地缓存；需最新版本时先在页面刷新远端引用。目标须干净，缺失的 worktree 会按需创建并初始化 Husky。</p>
            </div>
            <section v-else class="batch-review" aria-labelledby="batch-review-heading">
              <div class="batch-card-heading"><h3 id="batch-review-heading">确认方向与顺序</h3><span class="badge">{{ preview.rows.length }} 项</span></div><p class="hint">提交数量与文件概览以当前目标为基准，后续实际结果取决于前序合并；预览不保证无冲突。</p>
              <ol class="batch-results"><li v-for="(r,index) in preview.rows" :key="index"><strong>{{ label(r.source) }} → {{ r.target }}</strong><p class="hint">来源 {{ r.source_sha.slice(0,10) }} · {{ r.count }} 个候选提交</p><details><summary>查看文件概览</summary><pre class="diff" tabindex="0">{{ r.stat || '无文件差异' }}</pre></details></li></ol>
            </section>
          </template>
          <section v-else-if="job" aria-live="polite">
            <section v-if="resumePreview" class="batch-review" aria-labelledby="batch-resume-heading"><h3 id="batch-resume-heading">检查修复并继续</h3>
              <p class="hint wrap">分支 {{ resumePreview.branch }} · {{ resumePreview.path }} · HEAD {{ resumePreview.head.slice(0,10) }}</p>
              <p>确认后提交本次合并，再继续剩余 {{ resumePreview.remaining }} 项（含当前项）；已完成项会保留，不推送。</p>
              <h3>提交说明</h3><pre class="diff" tabindex="0">{{ resumePreview.message }}</pre>
              <h3>将暂存并提交的文件</h3><pre class="diff" tabindex="0">{{ resumePreview.files || '无文件变更，将记录合并关系。' }}
{{ resumePreview.stat }}</pre>
              <details class="commit-diff"><summary>审查本次合并与修复差异</summary><pre class="diff" tabindex="0">{{ resumePreview.patch || '没有代码差异' }}</pre></details>
              <p class="hint">预览后内容变化会阻止继续；提交仍执行项目 hooks，失败会再次保留现场。</p>
            </section>
            <section v-else-if="pushPreview" class="batch-push-review"><h3>确认批量推送</h3><p class="hint">仅推送本次合并的目标分支，按下列顺序执行。保留 pre-push hooks；不强推、不推送 master，失败或结果不确定时停止后续项。</p><ol class="batch-results"><li v-for="r in pushPreview.rows" :key="r.branch"><strong>{{ r.branch }} → {{ r.remote }}/{{ r.branch }}</strong><p class="hint wrap">{{ r.url }} · {{ r.ref }} · {{ r.head.slice(0,10) }}</p><p>{{ r.unchanged ? '远端已一致，将跳过' : r.first ? '首次创建同名远端并设置跟踪' : '仅快进推送' }} · {{ r.count }} 个待推送提交</p><details v-if="r.count"><summary>查看待推送提交</summary><pre class="diff" tabindex="0">{{ r.commits }}</pre></details></li></ol></section>
            <template v-else>
              <div class="notice" :class="job.status==='succeeded' ? 'success' : job.status==='failed' ? 'danger' : 'warning'" role="status">{{ job.push ? '本轮批量合并已完成；推送状态以下方结果为准。' : job.summary }}</div>
              <ol class="batch-results"><li v-for="(r,index) in job.rows" :key="index" :data-status="r.status"><div class="preview-heading"><strong>{{ label(r.source) }} → {{ r.target }}</strong><span class="badge">{{ statusLabel(r.status) }}</span></div><p class="batch-result-summary"><span v-if="r.resolution?.status==='failed'" class="hint">上次自动处理结果：</span>{{ r.summary }}</p><p v-if="r.commit" class="hint">本地合并提交：{{ r.commit.slice(0,10) }} · 推送状态以下方结果为准</p><p v-if="r.path" class="hint wrap batch-result-path">{{ r.path }}</p><p v-if="r.resolution?.url && r.resolution.status!=='running'"><a :href="r.resolution.url">在 Codex 中查看冲突处理 ↗</a></p>
                <template v-if="r.repair"><p class="notice" :class="r.repair.status==='failed' ? 'danger' : r.repair.status==='succeeded' ? 'success' : 'warning'" role="status">{{ r.repair.summary }}</p><p v-if="r.repair.warning" class="notice warning">{{ r.repair.warning }}</p><p v-if="r.repair.status==='running'" class="hint">Codex 正在修复；进程退出后可打开会话，检查修复结果再继续。</p><p v-else-if="r.repair.url"><a :href="r.repair.url">在 Codex 中查看本次修复 ↗</a></p></template>
                <div v-if="['failed','stopped'].includes(job.status) && ['failed','stopped'].includes(r.status) && r.recoverable" class="page-actions"><button @click="repair(index)" :disabled="loading || running || disabled">{{ r.repair ? '再次使用 Codex 修复' : '使用 Codex 修复' }}</button><button class="primary" @click="previewResume(index)" :disabled="loading || running || disabled">检查修复并继续</button></div>
                <template v-if="rowAction?.index===index"><p v-if="loading" class="loading batch-action-feedback" role="status">{{ rowAction.label }}</p><div v-if="error" class="notice danger batch-action-feedback" role="alert">{{ error }}</div><p v-if="error.includes('修复范围外')" class="hint">请保留现有改动，关闭弹窗后选择此分支，使用“检查并提交”重新审查当前暂存内容；处理完当前项后，再重新预览剩余分支。</p></template>
              </li></ol>
              <section v-if="job.push"><h3>批量推送结果</h3><div class="notice" :class="job.push.status==='succeeded' ? 'success' : job.push.status==='failed' ? 'danger' : 'warning'" role="status">{{ job.push.summary }}</div><ol class="batch-results"><li v-for="r in job.push.rows" :key="r.branch" :data-status="r.status"><div class="preview-heading"><strong>{{ r.branch }} → {{ r.remote }}/{{ r.branch }}</strong><span class="badge">{{ statusLabel(r.status) }}</span></div><p>{{ r.summary }}</p><p class="hint">{{ r.head.slice(0,10) }} · {{ r.ref }}</p></li></ol></section>
            </template>
          </section>
        </div>
        <div class="batch-footer">
          <template v-if="screen==='edit'">
            <p v-if="preview" class="hint batch-confirm-note">确认后将按上述顺序合并，由 Codex 处理冲突，并自动创建本地合并提交（执行正常 hooks）。失败即停止，保留现场；不推送。</p>
            <p v-else class="hint">{{ sources.length }} 个来源 → {{ targets.length }} 个目标 · 每侧最多 30 项 · 预览后再确认执行</p>
            <div class="batch-footer-actions"><button v-if="preview" @click="backToEdit" :disabled="loading">返回调整</button><button v-if="preview" class="primary" @click="start" :disabled="loading">确认合并并本地提交</button><button v-else class="primary" @click="generate" :disabled="loading || !ready">预览批量合并</button></div>
          </template>
          <template v-else-if="job">
            <p class="hint">关闭弹窗或刷新页面不会终止后台队列。重启工具不会自动续跑；失败后请先检查现场。</p>
            <div v-if="resumePreview" class="batch-footer-actions"><button @click="resumePreview=null" :disabled="loading">返回结果</button><button class="primary" @click="resume" :disabled="loading || running || disabled">确认提交并继续 {{ resumePreview.remaining }} 项</button></div>
            <div v-else-if="pushPreview" class="batch-footer-actions"><button @click="pushPreview=null" :disabled="loading">取消预览</button><button class="primary" @click="push" :disabled="loading || running">确认批量推送到同名远端</button></div>
            <div v-else class="batch-footer-actions"><button @click="poll" :disabled="loading">刷新进度</button><button v-if="job.status==='succeeded' && !running && job.push?.status!=='succeeded'" class="primary" @click="previewPush" :disabled="loading">{{ job.push ? '重新预览批量推送' : '批量推送' }}</button><button v-if="queueRunning" @click="cancel" :disabled="loading">停止后续任务</button><button v-else-if="!running" @click="open(mode)" :disabled="loading">重新选择分支</button></div>
          </template>
        </div>
      </dialog>`
  };
})();
