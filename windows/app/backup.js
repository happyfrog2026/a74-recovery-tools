function verifiedCameraBackup(result) {
  const regions = ['nflasha', 'nflashaB0', 'nflashaB1', 'cp-nor'];
  return result?.status === 'VERIFIED' &&
    regions.every(region => result.verified_regions?.includes(region));
}

function updateBackupRecovery() {
  const recovery = state.backupRecovery;
  byId('backupRecovery').hidden = !recovery;
  byId('startBackup').hidden = Boolean(recovery);
  if (!recovery) return;
  byId('backupRecapture').hidden = Boolean(recovery.emmc);
  byId('backupArchive').hidden = !recovery.emmc || Boolean(recovery.archive);
  byId('backupArchive').disabled = recovery.missing.some(region => !recovery.supplements.has(region));
  byId('backupArchiveVerify').hidden = !recovery.archive;
  const missing = recovery.missing.filter(region => !recovery.supplements.has(region));
  byId('backupSupplement').hidden = missing.length === 0;
  const select = byId('backupMissingRegion');
  select.replaceChildren(...missing.map(region => new Option(region, region)));
}

function showBackupDrift(result) {
  if (!state.attachOnly || result?.backup_anchor_mismatch?.region !== 'nflasha') return false;
  state.backupRecovery = {
    prior: state.backupJob + '\\snapshot', emmc: null, archive: null,
    missing: [], supplements: new Map()
  };
  setText('backupResult', '备份暂停。重连后读取的 eMMC 内容与原备份不同。');
  setText('backupRecoveryResult', '');
  updateBackupRecovery();
  return true;
}

async function recaptureBackupRegion(region) {
  const recovery = state.backupRecovery;
  if (!recovery || !region || state.busy) return;
  if (state.attachOnly && state.connection?.mode !== 'fastboot') return;
  if (!byId('backupPower').checked || !byId('backupSpace').checked)
    return setText('backupRecoveryResult', '请确认供电和保存空间。');
  if (!await confirmAction('确认同一台相机',
    '请确认仍是原备份的同一台机身，并且 Fastboot 已在线。新数据将双读并保存到独立目录。')) return;
  const output = newJob('backup-region-' + region, state.backupParent);
  const result = await run({ action: 'backup_recapture', prior_snapshot: recovery.prior,
    output_dir: output, region, same_camera_confirmed: true });
  if (result?.status === 'PARTIAL' && result.verified_regions?.includes(region)) {
    if (region === 'nflasha') recovery.emmc = output;
    else recovery.supplements.set(region, output);
    setText('backupRecoveryResult', '区域双读已验证：' + region + '\n' + output);
  } else {
    const detail = result?.summary || result?.error || '';
    const message = /Fastboot.*(?:unavailable|not online)/i.test(detail) ?
      'Fastboot 未在线。原备份已保留。' : '只读重采未完成，原备份已保留。';
    setText('backupRecoveryResult', message + '\n' + detail);
  }
  updateBackupRecovery();
}

async function archiveBackup() {
  const recovery = state.backupRecovery;
  if (!recovery?.emmc || state.busy || recovery.missing.some(region => !recovery.supplements.has(region))) return;
  if (!await confirmAction('确认同一台相机', '请确认归档中的所有区域都来自原备份的同一台相机。')) return;
  const output = newJob('backup-archive', state.backupParent);
  const result = await run({ action: 'backup_archive', prior_snapshot: recovery.prior,
    emmc_capture: recovery.emmc, supplements: [...recovery.supplements.values()],
    output_dir: output, same_camera_confirmed: true });
  if (result?.status === 'VERIFIED' && result.complete_region_set === true && result.restorable === false) {
    recovery.archive = output;
    recovery.missing = [];
    setText('backupRecoveryResult', '四区归档已校验。数据来自不同会话，恢复入口仅接受原有快照格式。' + '\n' + output);
  } else if (result?.status === 'INCOMPLETE' && result.missing_regions?.length) {
    recovery.missing = result.missing_regions;
    setText('backupRecoveryResult', '归档尚缺区域：' + recovery.missing.join(', '));
  } else {
    setText('backupRecoveryResult', '归档未完成，原备份已保留。' + '\n' + (result?.error || result?.summary || ''));
  }
  updateBackupRecovery();
}

async function verifyBackupArchive() {
  const recovery = state.backupRecovery;
  if (!recovery?.archive || state.busy) return;
  const result = await run({ action: 'backup_archive_verify', archive_dir: recovery.archive });
  const message = result?.status === 'VERIFIED' && result.complete_region_set === true && result.restorable === false ?
    '四区归档已离线复核。恢复入口仅接受原有快照格式。' :
    '归档复核未通过。' + '\n' + (result?.error || result?.summary || '');
  setText('backupRecoveryResult', message);
}

async function startBackup() {
  if (state.attachOnly && state.connection?.mode !== 'fastboot') return;
  if (!byId('backupPower').checked || !byId('backupSpace').checked)
    return setText('backupResult', '请确认供电和保存空间。');
  if (!state.attachOnly && !state.controlVerified) return setText('backupResult', '请先检查转接头。');
  if (!state.backupParent) return setText('backupResult', '请选择备份保存位置。');
  if (state.backupRecovery) return;
  if (state.backupJob) {
    const saved = await run({ action: 'backup_resume_status', job_dir: state.backupJob }, 'backupResult');
    if (showBackupDrift(saved) || saved?.status !== 'READY') return;
  }
  if (!state.backupJob) {
    const job = newJob('full_backup', state.backupParent);
    const prepared = await run({ action: 'prepare', kind: 'full_backup', inputs: {}, output_dir: job }, 'backupResult');
    if (!prepared || prepared.status !== 'READY') return;
    state.backupJob = job;
    setText('recentBackup', '最近任务目录：' + job);
  }
  const ready = await run({ action: 'reconcile', job_dir: state.backupJob }, 'backupResult');
  if (ready?.status !== 'READY') return;
  state.backupStarted = Date.now(); state.backupFinished = null; state.backupVerified = false;
  const captured = await run({ action: 'apply', job_dir: state.backupJob }, 'backupResult');
  if (captured?.status !== 'VERIFIED') {
    state.backupFinished = Date.now();
    showBackupDrift(captured);
    return;
  }
  const verified = await run({ action: 'verify', job_dir: state.backupJob }, 'backupResult');
  state.backupFinished = Date.now();
  if (verified?.status === 'VERIFIED') {
    if (state.attachOnly && !verifiedCameraBackup(verified)) {
      setText('backupResult', '机身备份未完成。eMMC、两个启动区和 CP NOR 均需通过双读校验。');
      status('机身备份未完成。eMMC、两个启动区和 CP NOR 均需通过双读校验。', 'warning');
      return;
    }
    state.backupVerified = true;
    setText('backupResult', state.attachOnly ?
      '机身备份已完成。eMMC、两个启动区和 CP NOR 均已通过双读校验。' :
      '备份文件和收据已验证。相机当前模式请单独查看日志。再次开始将创建新备份。');
    state.backupJob = null;
  }
}

bind('chooseBackup', async () => {
  const selected = await window.camera.choose('directory');
  if (!selected) return;
  const destination = await window.camera.backupDestination(selected);
  if (selected === state.backupParent && !destination.job && !state.backupRecovery) return;
  state.backupParent = destination.parent; state.backupJob = destination.job;
  state.backupRecovery = null; updateBackupRecovery();
  state.backupStarted = state.backupFinished = null; state.backupVerified = false;
  setText('backupTimer', ''); byId('backupProgress').hidden = true;
  setText('backupPath', destination.parent);
  setText('recentBackup', destination.job ? '最近任务目录：' + destination.job : '');
});
bind('chooseBackupJob', async () => {
  const selected = await choose('directory', 'backupPath');
  if (selected) {
    state.backupJob = selected; state.backupParent = parentOf(selected);
    state.backupRecovery = null; updateBackupRecovery();
  }
});
bind('startBackup', startBackup);
bind('backupRecapture', () => recaptureBackupRegion('nflasha'));
bind('backupCaptureMissing', () => recaptureBackupRegion(byId('backupMissingRegion').value));
bind('backupArchive', archiveBackup);
bind('backupArchiveVerify', verifyBackupArchive);

function updateBackupTimers() {
  if (state.backupStarted) {
    const fraction = state.backupVerified ? 1 : .95 * Math.min(1, Math.max(0, ((state.backupFinished || Date.now()) - state.backupStarted) / 972000));
    byId('backupProgress').hidden = false; byId('backupProgress').value = fraction;
    setText('backupTimer', `备份已进行 ${elapsed(state.backupStarted, state.backupFinished || Date.now())} · ${Math.floor(fraction * 100)}%`);
  }
}
