const byId = id => document.getElementById(id);
const state = {
  jobHistory: new Set(), taskRoot: '', busy: false, page: 'board', controlVerified: false,
  boardJob: null, boardReadyToInstall: false, stockJob: null, stockPlan: null, stockBlocker: null,
  backupParent: null, backupJob: null, backupRecovery: null, restoreTarget: null, restoreJob: null,
  dat: null, card: null, cardConflict: null, official: false, sdStep: 'entry', fromHelper: false, cameraRoute: false, waitingStock: false, restoreFailure: false,
  backupStarted: null, backupFinished: null, backupVerified: false, restoreStarted: null,
  restoreFinished: null, norStarted: null, norFinished: null, stockStarted: null
};

function setText(id, text) {
  const element = byId(id);
  element.dataset.sourceText = text == null ? '' : String(text);
  element.textContent = window.localization.translate(element.dataset.sourceText);
}
function value(row, key) { return row && row[key] != null ? String(row[key]) : ''; }
function newJob(kind, parent = state.taskRoot) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  return `${parent}\\${kind}-${stamp}-${Math.random().toString(16).slice(2, 8).toUpperCase()}`;
}
function parentOf(folder) { return folder.replace(/[\\/][^\\/]+$/, ''); }
function page(name) {
  if (state.busy) return;
  state.page = name;
  for (const item of document.querySelectorAll('.page')) item.classList.toggle('active', item.id === name);
  for (const item of document.querySelectorAll('.nav')) item.classList.toggle('active', item.dataset.page === name);
  setText('pageTitle', { board: '准备 Multi 转接头', prepareCamera: '准备相机', connection: '检查连接',
    stock: '从 SD 卡恢复原厂', backup: '备份相机', restore: '恢复已保存状态' }[name]);
}
function status(message, kind = '') {
  setText('statusText', message);
  byId('status').className = `status ${kind}`;
}
function confirmAction(title, detail) {
  return window.camera.confirm(window.localization.translate(title), window.localization.translate(detail),
    window.localization.selected());
}
function taskText(result) {
  const code = value(result, 'status');
  const headline = {
    READY: '检查完成，可以继续', VERIFIED: '已完成并验证',
    NO_WRITE: '已停止，未写入', INCOMPLETE: '任务尚未完成',
    NEEDS_USER_ACTION: '需要手动操作',
    ALREADY_INSTALLED: '转接头已安装', ALREADY_PRESENT: '卡上已有相同文件',
    NEEDS_CONFIRMATION: '需要确认替换', FAILURE_RECORDED: '已记录更新失败'
  }[code] || '需要查看任务状态';
  const next = value(result, 'next_action');
  const summary = value(result, 'summary');
  const hasSkips = ['VERIFIED', 'PARTIAL'].includes(code) && result?.success_with_skips === true;
  const skipped = Array.isArray(result?.skipped_regions) ? result.skipped_regions.map(row => row.name).filter(Boolean).join(', ') : '';
  const warning = hasSkips ? '\n' + (skipped ? `跳过区域：${skipped}` : '跳过区域：详情缺失。请查看日志。') : '';
  const label = code === 'PARTIAL' ? '恢复流程已完成，存在未恢复或未通过校验的区域' : hasSkips ? '可恢复区域已验证；有区域未恢复' : headline;
  return `${label} (${code})\n${summary}${next ? '\n' + next : ''}${warning}`;
}
function allJobs() {
  return [...new Set([...state.jobHistory, state.boardJob, state.stockJob, state.backupJob, state.restoreJob].filter(Boolean))];
}
async function refreshLogs() {
  try {
    const log = byId('logText'); const position = log.scrollTop;
    const raw = await window.camera.readLogs(); const previous = log.textContent;
    if (raw !== previous) {
      if (raw.startsWith(previous) && log.firstChild) log.firstChild.appendData(raw.slice(previous.length));
      else log.textContent = raw;
      log.dataset.sourceText = raw;
    }
    log.scrollTop = byId('followLogs').checked ? log.scrollHeight : position;
  } catch { /* retain the last available log */ }
}

async function run(request, resultId) {
  if (state.busy) return null;
  state.busy = true;
  if (request.job_dir || request.output_dir) state.jobHistory.add(request.job_dir || request.output_dir);
  status('正在检查…');
  document.querySelectorAll('button').forEach(button => { if (!button.classList.contains('nav')) button.disabled = true; });
  byId('progress').hidden = true;
  try {
    const result = await window.camera.run(request);
    if (resultId) setText(resultId, taskText(result));
    const code = value(result, 'status');
    status(`${code}: ${value(result, 'summary')}${result.success_with_skips ? ' · 有未恢复区域' : ''}`,
      result.success_with_skips ? 'warning' : code === 'VERIFIED' || code === 'READY' ? 'success' : '');
    await refreshLogs();
    return result;
  } catch (error) {
    const message = error.message || String(error);
    if (resultId) setText(resultId, `未完成：${message}`);
    status(message, 'error');
    await refreshLogs();
    return null;
  } finally {
    state.busy = false;
    document.querySelectorAll('button').forEach(button => { if (!button.classList.contains('nav')) button.disabled = false; });
    byId('installBoard').disabled = !state.boardReadyToInstall;
    byId('runStock').disabled = !state.stockPlan;
    byId('progress').hidden = true;
  }
}

function showRestorePhase(phase, label = '') {
  state.restorePhase = phase;
  byId('restoreStage').hidden = phase !== 'stage' && phase !== 'nor';
  byId('restoreVerify').hidden = phase !== 'verify';
  byId('restoreWrite').hidden = phase !== 'write';
  if (label) setText('restoreStageText', label);
}

function handleProgress(event) {
  const stage = {
    control: '正在验证 USB 1', bridge: '正在进入临时恢复模式', data: '正在验证 USB 2',
    prepare: '正在准备任务', reconcile: '正在检查设备', capture: '正在备份',
    backup_retry: '备份读取中断，正在重连', backup_retry_reset: '新分段已核验，连续重连归零',
    backup_reconnect: '正在等待相机重新连接', restore_retry: '恢复连接中断，正在核对',
    restore_reconnect: '正在安全检查后重新进入恢复连接', restore_local_verify: '正在校验本机备份…', restore_plan_summary: '正在计算本次计划片段数…',
    snapshot: '正在读取相机当前状态…', restore_plan: '正在计算恢复范围…',
    restore_capture_retry: '相机读取中断，正在重连…',
    restore_reentry_retry: '正在重试临时恢复连接…',
    restore_write_session_entry: '正在连接相机以写入恢复数据…',
    restore_reconnected: '相机已重连，正在核对写入状态…',
    restore_prior_state: '正在核对上次恢复的相机状态…',
    fragment_write_begin: '正在写入并校验片段…',
    restore_nor_reconnect: '启动数据阶段：正在重新连接相机…',
    restore_nor_prepare: '存储恢复已验证，正在准备重建启动数据…',
    restore_nor_install: '正在准备相机启动数据重建…',
    restore_nor_wait: '正在重建并校验启动数据，请保持供电和连接…',
    restore_nor_collect: '正在核对启动数据重建结果…',
    restore_nor_cleanup: '正在清理临时恢复入口…', restore_nor_normal: '正在返回正常模式',
    return_normal: '正在返回正常模式',
    mode: '正在检查存储卡连接', mount: '正在等待存储卡挂载', reboot: '正在准备存储卡连接'
  }[event.stage] || '正在执行任务';
  const attempt = event.retry ? ` ${event.retry}/${event.max_retries || 5}` : '';
  const total = event.total_retries ? ` · 累计 ${event.total_retries}` : '';
  status(`${stage}${attempt}${total}`);
  if (state.page === 'restore' && state.restoreStarted && !state.restoreFinished) {
    const norEvent = event.stage?.startsWith('restore_nor_');
    if (norEvent && !state.norStarted) state.norStarted = Date.now() - (event.elapsed_seconds || 0) * 1000;
    if (event.stage === 'restore_nor_wait' && !state.norWriterStarted)
      state.norWriterStarted = Date.now() - (event.elapsed_seconds || 0) * 1000;
    if (event.planned_fragments != null) state.planned = event.planned_fragments;
    if (event.verified_fragments != null) state.verified = event.verified_fragments;
    if (event.failed_fragments != null) state.failed = event.failed_fragments;
    if (event.completed_fragments != null) state.completed = event.completed_fragments;
    if (state.norStarted) {
      if (norEvent || ['bridge', 'restore_retry', 'restore_reentry_retry'].includes(event.stage))
        showRestorePhase('nor', stage + attempt);
    } else if (event.stage === 'restore_local_verify' && Number.isFinite(event.bytes_total)) {
      const bytes = event.bytes_done || 0;
      const totalBytes = event.bytes_total;
      showRestorePhase('verify');
      byId('restoreVerifyBar').value = totalBytes > 0 ? Math.min(1, bytes / totalBytes) : 0;
      setText('restoreBytes', `已校验 ${bytes.toLocaleString()} / ${totalBytes.toLocaleString()} 字节`);
    } else if (event.stage?.startsWith('fragment_write_')) {
      showRestorePhase('write');
    } else if (['prepare', 'control', 'bridge', 'uart', 'data', 'snapshot', 'restore_plan',
                'restore_reconnect', 'restore_retry', 'restore_capture_retry', 'restore_reentry_retry',
                'restore_write_session_entry', 'restore_reconnected', 'restore_prior_state'].includes(event.stage)) {
      showRestorePhase('stage', stage + attempt);
    }
    updateTimers();
    return;
  }
  if (typeof event.fraction === 'number' && !event.stage?.startsWith('restore_nor_')) {
    byId('progress').hidden = false;
    byId('progress').value = Math.max(0, Math.min(1, event.fraction));
  }
}
window.camera.onProgress(handleProgress);

async function checkBoard() {
  if (state.boardReadyToInstall) { byId('installBoard').click(); return; }
  if (!state.boardJob) {
    const job = newJob('board_install');
    const prepared = await run({ action: 'prepare', kind: 'board_install', inputs: {}, output_dir: job }, 'boardResult');
    if (!prepared || prepared.status !== 'READY') return;
    state.boardJob = job;
  }
  const result = await run({ action: 'reconcile', job_dir: state.boardJob }, 'boardResult');
  if (result && result.control_ping_verified) state.controlVerified = true;
  state.boardReadyToInstall = result?.status === 'READY';
  const needsBoot = [
    'Connect the RP2040 in BOOTSEL mode',
    'Disconnect USB, hold BOOT while reconnecting, then release BOOT and check again'
  ].includes(result?.next_action);
  byId('bootSteps').hidden = !needsBoot;
  if (needsBoot) setText('boardResult', taskText({ ...result, next_action: '' }));
  byId('installBoard').disabled = !state.boardReadyToInstall;
  setText('checkBoard', state.boardReadyToInstall ? '安装配套程序' : '检查转接头');
}
async function probeControl(resultId = 'connectionResult') {
  const result = await run({ action: 'connection_probe', camera_model: connectionCameraModel() }, resultId);
  state.controlVerified = result?.control?.status === 'READY';
  setText('stockAdapterState', state.controlVerified ? 'Multi 转接头已验证' : '请先检查 Multi 转接头');
  return result;
}
async function choose(kind, destination) {
  const selected = await window.camera.choose(kind);
  if (selected) setText(destination, selected);
  return selected;
}
async function prepareCard(confirmed = false) {
  if (state.cameraRoute && !byId('cameraVolumeConfirmed').checked) return setText('sdResult', '我确认所选路径是相机的存储卡');
  if (!state.dat || !state.card) return setText('sdResult', '先选择 DAT 文件和存储卡。');
  const request = { action: 'sd_card_prepare', source_dat: state.dat, volume: state.card,
    camera_model: byId('sdModel').value,
    confirm_replace: confirmed };
  if (confirmed) {
    request.expected_existing_sha256 = state.cardConflict.existing_sha256;
    request.expected_volume_device_identifier = state.cardConflict.volume_device_identifier;
  }
  const result = await run(request, 'sdResult');
  if (result?.status === 'NEEDS_CONFIRMATION') {
    state.cardConflict = result;
    if (await confirmAction('替换卡片上的 BODYDATA.DAT',
      '所选存储卡根目录已有不同的 DAT。确认后将替换该文件；请先核对卡片和文件。')) await prepareCard(true);
  } else if (result?.status === 'VERIFIED' || result?.status === 'ALREADY_PRESENT') {
    state.cardConflict = null;
    setText('sdCopyProof', taskText(result));
    setSDStep(state.official ? 'officialUpdate' : 'slot');
    if (!state.official) byId('stockCard').checked = false;
    state.stockJob = null;
    state.stockPlan = null;
    byId('runStock').disabled = true;
  }
}
async function startStock() {
  if (!['ILCE-7M4', 'ILCE-7RM5'].includes(byId('sdModel').value)) {
    return setText('stockResult', '此机型的转接头恢复尚不支持。');
  }
  if (![byId('stockModel'), byId('stockPower'), byId('stockCard')].every(box => box.checked)) {
    return setText('stockResult', '请确认机型、供电和卡槽 1 的 DAT。');
  }
  if (!state.controlVerified) return setText('stockResult', '请先检查转接头。');
  if (!state.stockJob) {
    const job = newJob('stock_sd_recovery');
    const prepared = await run({ action: 'prepare', kind: 'stock_sd_recovery',
      inputs: { sd_ready: true, camera_model: byId('sdModel').value }, output_dir: job }, 'stockResult');
    if (!prepared || prepared.status !== 'READY') return;
    state.stockJob = job;
  }
  const plan = await run({ action: 'stock_plan', job_dir: state.stockJob }, 'stockResult');
  if (!plan) return;
  if (plan.status === 'READY' && plan.plan_digest) {
    state.stockPlan = plan.plan_digest;
    byId('runStock').disabled = false;
    await runStock();
  } else if (plan.blocking_tasks?.length) {
    state.stockBlocker = plan.blocking_tasks[0].job_dir;
    showStockOutcome('prior');
  }
}
function showStockOutcome(which) {
  setSDStep('ready');
  state.waitingStock = which === 'current';
  byId('stockChecks').hidden = true;
  byId('stockOutcome').hidden = false;
  byId('stockWaitInstructions').hidden = which !== 'current';
  byId('sdBack').hidden = state.waitingStock;
  byId('completeStock').hidden = which !== 'current';
  for (const id of ['priorComplete', 'priorNoCard', 'priorBlink']) byId(id).hidden = which !== 'prior';
}
async function runStock() {
  if (!state.stockJob || !state.stockPlan) return;
  const yes = await confirmAction('确认原厂恢复方案',
    '将使用卡槽 1 存储卡中的固件包恢复相机。此操作可能导致数据丢失、相机无法启动，并可能影响原厂保修。请保持供电和连接。');
  if (!yes) return;
  const result = await run({ action: 'stock_run', job_dir: state.stockJob,
    confirmed_plan_digest: state.stockPlan }, 'stockResult');
  if (result?.stale_plan) {
    state.stockJob = null; state.stockPlan = null;
    setText('stockResult', '相机内容已变化，未写入。请重新读取并制定方案。');
  }
  if (result?.status === 'NEEDS_USER_ACTION' && result.stock_reboot_acknowledged) {
    state.stockStarted = Date.now();
    showStockOutcome('current');
    setText('stockResult', '重启指令已确认。请等待相机屏幕显示更新完成，再明确记录结果。');
  }
  if (result?.blocking_tasks?.length) {
    state.stockBlocker = result.blocking_tasks[0].job_dir;
    showStockOutcome('prior');
  }
}
async function stockOutcome(kind) {
  const job = kind === 'current' ? state.stockJob : state.stockBlocker;
  if (!job) return setText('stockResult', '没有可确认的更新任务。');
  const reason = kind === 'no_card' ? 'no_sd_card' : 'blink_5_long_6_short';
  const action = kind === 'current' || kind === 'prior_complete' ?
    'confirm_stock_update_complete' : 'confirm_stock_update_failed';
  const yes = await confirmAction('记录相机屏幕观察',
    action === 'confirm_stock_update_complete' ? '确认你已亲眼看到相机更新完成。' :
      '确认你亲眼观察到此次更新失败。记录后不会自动重新运行恢复。');
  if (!yes) return;
  const request = { action, job_dir: job, confirmed: true };
  if (action === 'confirm_stock_update_failed') request.reason = reason;
  const result = await run(request, 'stockResult');
  if (result?.status === 'VERIFIED' || result?.status === 'FAILURE_RECORDED') {
    state.stockBlocker = null;
    byId('stockOutcome').hidden = true;
    state.waitingStock = false; state.stockStarted = null; state.stockJob = state.stockPlan = null;
    setText('stockTimer', ''); byId('stockChecks').hidden = false; byId('sdBack').hidden = false;
  }
}
async function startRestore() {
  if (!byId('restorePower').checked) return setText('restoreResult', '请确认稳定供电。');
  if (!state.controlVerified) return setText('restoreResult', '请先检查转接头。');
  if (!state.restoreTarget) return setText('restoreResult', '请选择已验证备份。');
  state.restoreJob = null;
  state.restoreFailure = false;
  state.restoreStarted = Date.now();
  state.restoreFinished = state.norStarted = state.norFinished = state.norWriterStarted = null;
  state.planned = null; state.completed = state.verified = state.failed = 0;
  setText('norTimer', '');
  showRestorePhase('stage', '正在准备恢复…');
  const job = newJob('scoped_restore', parentOf(state.restoreTarget));
  const prepared = await run({ action: 'prepare', kind: 'scoped_restore',
    inputs: { target_snapshot: state.restoreTarget, restore_profile: 'capability' }, output_dir: job }, 'restoreResult');
  if (prepared?.status !== 'READY') {
    state.restoreFinished = Date.now();
    showRestorePhase(null);
    updateTimers();
    return;
  }
  state.restoreJob = job;
  showRestorePhase('stage', '正在连接相机…');
  const result = await run({ action: 'restore_run', job_dir: job }, 'restoreResult');
  state.restoreFinished = Date.now();
  showRestorePhase(null);
  if (state.norStarted) state.norFinished = state.restoreFinished;
  state.restoreFailure = !result || !['VERIFIED', 'PARTIAL'].includes(result.status);
  setText('startRestore', state.restoreFailure ? '从所选备份重新开始' : '开始恢复');
  if (result) {
    state.planned = result.planned_fragments ?? state.planned;
    state.verified = result.verified_fragments ?? state.verified;
    state.failed = result.failed_fragments ?? state.failed;
    if (result.restore_previous_pending) setText('restoreResult',
      '上次恢复的写入或重启结果尚未确认。请先确认相机已回到普通 USB 模式，再选同一份备份重新开始。');
    else if (result.emmc_status === 'VERIFIED' && result.nor_status === 'NOT_STARTED') {
      const reason = value(result, 'nor_skip_reason');
      setText('restoreResult', '存储恢复已验证，启动数据重建（NOR）尚未开始。' + (reason ? '\n' + reason : ''));
    }
    else if (result.status === 'PARTIAL') setText('restoreResult', taskText(result) + '\n' +
      (state.failed ? `恢复流程已完成，${state.failed}处写入校验未通过` : '没有可写的恢复片段；请查看跳过区域'));
    else if (result.status === 'VERIFIED' && result.nor_status === 'VERIFIED')
      setText('restoreResult', '存储恢复与启动数据重建已验证，相机已返回普通模式。请检查取景与菜单是否正常。');
    else if (result.status === 'VERIFIED' && !result.success_with_skips && result.nor_status !== 'UNSUPPORTED') { setText('restoreResult', '恢复尚未全部验证，请查看日志。\n' + value(result, 'summary')); status('恢复尚未全部验证，请查看日志。', 'warning'); }
    else if (result.nor_status === 'UNSUPPORTED') setText('restoreResult', taskText(result) + '\n本次未执行启动数据重建（NOR）；当前配置尚不支持。');
  }
  updateTimers();
}

function bind(id, action) { byId(id).addEventListener('click', () => { Promise.resolve(action()).catch(error => status(error.message, 'error')); }); }
for (const button of document.querySelectorAll('.nav')) bind(button.id || (button.id = `nav-${button.dataset.page}`), () => page(button.dataset.page));
bind('checkBoard', checkBoard);
bind('installBoard', async () => {
  if (!state.boardJob || !state.boardReadyToInstall) return;
  if (!await confirmAction('确认安装配套程序',
    '将配套程序安装到当前 BOOTSEL 中的 RP2040。安装后转接头会重新连接。')) return;
  const result = await run({ action: 'apply', job_dir: state.boardJob }, 'boardResult');
  if (result?.status === 'VERIFIED' || result?.status === 'ALREADY_INSTALLED') {
    state.boardReadyToInstall = false;
    setText('checkBoard', '检查转接头');
    byId('installBoard').disabled = true;
  }
});
async function checkConnection(readOnly) {
  if (state.busy) return;
  const before = await window.camera.fastbootDriverStatus();
  const driverPanel = byId('fastbootDriver');
  function showMissingDriver() {
    driverPanel.hidden = false;
    byId('openFastbootDriver').hidden = false;
    setText('fastbootDriverStatus', 'Windows 已识别 Fastboot，但缺少 WinUSB 驱动（Code 28）。');
  }
  if (before === 'driver_missing') {
    setText('connectionResult', '');
    showMissingDriver();
    await refreshLogs();
    return;
  }
  const action = readOnly || before === 'present' ? 'connection_probe' : 'connection_enter_ram';
  const result = await run({ action, confirmed: true, camera_model: connectionCameraModel() }, 'connectionResult');
  state.controlVerified = result?.control?.status === 'READY';
  if (result) {
    const labels = { READY: '连接已验证', UNAVAILABLE: '尚未就绪', MISSING: '未识别到设备',
      BOOTSEL: 'BOOTSEL', NORMAL_USB: '普通 USB 模式', MSC: 'MSC',
      FASTBOOT: '已进入临时恢复模式', AMBIGUOUS: '检测到多个设备', ERROR: '检查失败' };
    const label = entry => labels[entry] || entry || '';
    setText('connectionResult', taskText(result) +
      `\nUSB 1 (RP2040): ${label(result.control?.status)}\nUSB 2 (DATA): ${label(result.data?.status)}\n${label(result.current_mode)}`);
  }
  if (result?.data?.status === 'READY') {
    driverPanel.hidden = true;
    return;
  }
  const after = await window.camera.fastbootDriverStatus();
  if (after === 'driver_missing') showMissingDriver();
  else if (!driverPanel.hidden) {
    byId('openFastbootDriver').hidden = true;
    setText('fastbootDriverStatus', after === 'absent'
      ? 'Windows 当前未识别到 Fastboot 设备。' : 'Fastboot 只读检查未通过，请查看运行日志。');
  }
  await refreshLogs();
}
bind('checkConnection', () => checkConnection(false));
bind('removeFastbootWinUsb', async () => {
  if (state.busy) return;
  const resultNode = byId('fastbootRemovalResult');
  const buttons = [byId('checkConnection'), byId('openFastbootDriver'),
    byId('recheckFastboot'), byId('removeFastbootWinUsb')];
  const disabled = buttons.map(button => button.disabled);
  buttons.forEach(button => { button.disabled = true; });
  state.busy = true;
  resultNode.hidden = false;
  setText('fastbootRemovalResult', '正在检查 Fastboot 驱动…');
  try {
    const result = await window.camera.removeFastbootWinUsb(window.localization.selected());
    setText('fastbootRemovalResult', result.message);
    if (result.code === 'removed') {
      byId('fastbootDriver').hidden = true;
      setText('connectionResult', '尚未检查。');
    }
    await refreshLogs();
  } catch (error) {
    setText('fastbootRemovalResult', error.message || String(error));
  } finally {
    state.busy = false;
    buttons.forEach((button, index) => { button.disabled = disabled[index]; });
  }
});
bind('openFastbootDriver', () => window.camera.openFastbootDriver());
bind('recheckFastboot', () => checkConnection(true));
bind('probeControl', () => probeControl());
bind('stockControl', () => probeControl('stockResult'));
bind('backupControl', () => probeControl('backupResult'));
bind('restoreControl', () => probeControl('restoreResult'));
bind('chooseDAT', async () => { state.dat = await choose('dat', 'datPath') || state.dat; byId('chooseCard').disabled = byId('cameraCard').disabled = !state.dat; });
bind('chooseCard', async () => { const selected = await choose('directory', 'cardPath'); if (selected) { state.card = selected; state.cameraRoute = false; setSDStep('target'); } });
bind('changeCard', async () => { const selected = await choose('directory', 'cardPath'); if (selected) { state.card = selected; byId('cameraVolumeConfirmed').checked = false; setSDStep('target'); } });
bind('cameraCard', async () => {
  const result = await run({ action: 'prepare_card_connection', route: 'camera', camera_model: byId('sdModel').value }, 'sdResult');
  if (result?.status === 'READY') {
    const chosen = await choose('directory', 'cardPath');
    if (chosen && await confirmAction('确认相机存储卡', '确认该可移动盘确实是当前相机卡槽中的存储卡。'))
      { state.card = chosen; state.cameraRoute = true; byId('cameraVolumeConfirmed').checked = false; setSDStep('target'); }
  }
});
bind('copyDAT', () => prepareCard());
bind('startStock', startStock);
bind('runStock', runStock);
bind('completeStock', () => stockOutcome('current'));
bind('priorComplete', () => stockOutcome('prior_complete'));
bind('priorNoCard', () => stockOutcome('no_card'));
bind('priorBlink', () => stockOutcome('blink'));
bind('chooseRestore', async () => {
  const selected = await window.camera.choose('directory');
  if (selected) {
    state.restoreTarget = await window.camera.resolveSnapshot(selected);
    state.restoreJob = null; state.restoreFailure = false;
    state.restoreStarted = state.restoreFinished = state.norStarted = state.norFinished = state.norWriterStarted = null; state.planned = null;
    showRestorePhase(null);
    for (const id of ['restoreTimer', 'norTimer', 'restoreFragments', 'restoreResult']) setText(id, '');
    setText('startRestore', '开始恢复');
    setText('restorePath', state.restoreTarget);
  }
});
bind('startRestore', startRestore);
bind('exportLogs', async () => {
  const output = await window.camera.exportLogs(allJobs());
  if (output) status(`日志已导出：${output}`, 'success');
});

window.camera.paths().then(paths => {
  state.taskRoot = paths.taskRoot;
  byId('appVersion').textContent = paths.version;
  if (paths.platform !== 'win32') setText('platformNotice', '当前平台仅可预览界面；设备操作需要 Windows。');
  refreshLogs();
});
page('board');
setInterval(refreshLogs, 4000);

function elapsed(start, end = Date.now()) { return new Date(Math.max(0, end - start)).toISOString().slice(11, 19); }
function updateTimers() {
  updateBackupTimers();
  if (state.restoreStarted) setText('restoreTimer', `恢复已进行 ${elapsed(state.restoreStarted, state.restoreFinished || Date.now())}`);
  if (state.norStarted) {
    const estimate = state.norWriterStarted && !state.norFinished ?
      '启动数据重建预计约 3 分钟；请保持供电和连接。\n' : '';
    setText('norTimer', estimate + `启动数据阶段已进行 ${elapsed(state.norStarted, state.norFinished || Date.now())}`);
  }
  if (state.planned != null && state.restorePhase === 'write') {
    const processed = state.completed || 0;
    byId('restoreWriteBar').value = state.planned ? Math.min(1, processed / state.planned) : 0;
    setText('restoreFragments', `已处理 ${processed}/${state.planned} · 已验证 ${state.verified || 0} · 未通过 ${state.failed || 0}`);
  }
  if (state.stockStarted) setText('stockTimer', `请等待相机更新，预计约 7 分钟。已等待 ${elapsed(state.stockStarted)}`);
}
setInterval(updateTimers, 1000);
bind('cameraReady', () => page('connection'));
const sdModels = [['ILCE-7M4','A7 IV'],['ILCE-7RM5','A7R V'],['ILCE-7SM3','A7S III'],['ILCE-1','A1'],['ILCE-1M2','A1 II'],['ILCE-9M3','A9 III'],['ILME-FX2','FX2'],['ILME-FX3','FX3'],['ILME-FX3A','FX3A'],['ILME-FX6','FX6'],['ILME-FX30','FX30']];
for (const [id, name] of sdModels) byId('sdModel').add(new Option(`${name} (${id})`, id));
function officialInstructions() {
  setText('officialText', byId('sdModel').value === 'ILME-FX6' ?
    '请安全推出卡片。连接原装 AC 电源，将存有 BODYDATA.DAT 的 SDXC 或 CFexpress Type A 卡插入卡槽 B。打开 Maintenance（维护）→ Version（版本）→ Version Up（版本更新），核对版本后按屏幕提示执行。' :
    '将卡插入卡槽 1，打开菜单 → 设置 → 设置选项 → 版本。如果有“机身”，先选“机身”，再选“软件更新”。请按相机提示核对目标版本并完成更新。');
}
function setSDStep(step) {
  state.sdStep = step;
  byId('sdEntry').hidden = step !== 'entry';
  byId('sdHelper').hidden = !['source', 'target'].includes(step);
  byId('sdSource').hidden = step !== 'source';
  byId('sdTarget').hidden = step !== 'target';
  byId('sdSlot').hidden = step !== 'slot';
  byId('officialInstructions').hidden = step !== 'officialUpdate';
  byId('stockReady').hidden = step !== 'ready';
  byId('sdBack').hidden = step === 'entry' || state.waitingStock;
  byId('cameraCard').hidden = state.official;
  byId('chooseCard').disabled = byId('cameraCard').disabled = !state.dat;
  byId('cameraCardConfirmation').hidden = !state.cameraRoute;
  setText('targetDAT', '所选文件：' + (state.dat || ''));
  setText('confirmSlot', state.cameraRoute ? '确认卡仍在槽 1，继续' : '已插入卡槽 1，继续');
  setText('stockAdapterState', state.controlVerified ? 'Multi 转接头已验证' : '请先检查 Multi 转接头');
}
function sdHelper(official) {
  state.official = official; state.fromHelper = true; state.cameraRoute = false;
  state.dat = state.card = null; state.stockJob = state.stockPlan = null;
  setText('datPath', '尚未选择 DAT。'); setText('sdResult', '');
  officialInstructions(); setSDStep('source');
}
function connectionCameraModel() {
  return byId('sdModel').value === 'ILCE-7RM5' ? 'ILCE-7RM5' : 'ILCE-7M4';
}
byId('sdModel').addEventListener('change', () => {
  const r5 = byId('sdModel').value === 'ILCE-7RM5';
  const supported = ['ILCE-7M4', 'ILCE-7RM5'].includes(byId('sdModel').value);
  setText('adapterRecoveryTitle', r5 ? 'A7R V 引导恢复' : 'A7 IV 引导恢复');
  setText('adapterRecoveryDescription', r5 ?
    '无法进入菜单时，A7R V 可使用 Multi 转接头启动恢复。' :
    '无法进入菜单时，A7 IV 可使用 Multi 转接头启动恢复。');
  setText('stockModelLabel', r5 ? '我确认相机是 A7R V（ILCE-7RM5）' : '我确认相机是 A7M4（ILCE-7M4）');
  byId('stockModel').checked = false;
  byId('stockCard').checked = false;
  byId('adapterRecovery').hidden = !supported;
  byId('moreFirmwareDownloads').hidden = !supported;
  byId('sdUnsupported').hidden = supported;
  state.stockJob = state.stockPlan = null;
  setSDStep('entry');
});
bind('officialPrepare', () => sdHelper(true));
bind('helpCard', () => sdHelper(false));
bind('officialReady', () => { state.official = true; state.fromHelper = false; officialInstructions(); setSDStep('officialUpdate'); });
bind('preparedCard', () => { state.fromHelper = false; byId('stockCard').checked = true; setSDStep('ready'); });
bind('confirmSlot', () => { byId('stockCard').checked = true; setSDStep('ready'); });
bind('sdBack', () => {
  if (state.waitingStock || state.busy) return;
  setSDStep({source:'entry',target:'source',slot:'target',officialUpdate:state.fromHelper?'target':'entry',ready:state.fromHelper?'slot':'entry'}[state.sdStep] || 'entry');
});
bind('officialRequirements', () => window.camera.openSupport(byId('sdModel').value));
bind('downloadFirmware', () => window.camera.openSupport(byId('sdModel').value));
bind('moreFirmwareDownloads', () => window.camera.openFirmwareArchive(byId('sdModel').value));

for (const id of ['stockPower','backupPower','restorePower']) byId(id).addEventListener('change', () => {
  for (const other of ['stockPower','backupPower','restorePower']) byId(other).checked = byId(id).checked;
});
byId('followLogs').addEventListener('change', () => { if (byId('followLogs').checked) byId('logText').scrollTop = byId('logText').scrollHeight; });
