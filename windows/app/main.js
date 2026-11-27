const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { fastbootDriverStatus } = require('./fastboot-driver');
const { removeFastboot, windowsExecutor } = require('../attach_profile/driver-maintenance');

let mainWindow;
let workerBusy = false;
let smokeSameCameraRequests = 0;

function userRoot() {
  if (!process.env.LOCALAPPDATA) throw new Error('LOCALAPPDATA is unavailable');
  return path.join(process.env.LOCALAPPDATA, 'Camera Recovery');
}

function log(message) {
  try {
    const root = userRoot();
    fs.mkdirSync(root, { recursive: true });
    fs.appendFileSync(path.join(root, 'native-session.log'),
      `${new Date().toISOString()}  ${String(message).replace(/[\r\n]+/g, ' ')}\n`);
  } catch (error) {
    process.stderr.write(`Session log unavailable: ${error.message}\n`);
  }
}

function workerCommand() {
  if (process.env.CAMERA_RECOVERY_SMOKE === '1' && process.platform !== 'win32') {
    return { executable: process.env.CAMERA_RECOVERY_TEST_PYTHON || '/usr/bin/python3',
      args: [path.resolve(__dirname, '../tests/fake_worker.py')] };
  }
  if (process.platform !== 'win32') {
    throw new Error('Windows worker actions require Windows; this host is for source review only');
  }
  if (!app.isPackaged && process.env.CAMERA_RECOVERY_PYTHON) {
    return { executable: process.env.CAMERA_RECOVERY_PYTHON,
      args: [path.resolve(__dirname, '../worker_entry.py')] };
  }
  const override = process.env.CAMERA_RECOVERY_WORKER;
  if (override) return { executable: override, args: [] };
  const bundled = path.join(process.resourcesPath, 'worker', 'CameraRecoveryWorker.exe');
  const development = path.resolve(__dirname, '../../build/windows-worker-dist/CameraRecoveryWorker/CameraRecoveryWorker.exe');
  const executable = fs.existsSync(bundled) ? bundled : development;
  if (!fs.existsSync(executable)) {
    for (const root of [path.dirname(bundled), path.dirname(development)]) {
      const python = path.join(root, 'python.exe');
      const entry = path.join(root, 'worker_entry.py');
      if (fs.existsSync(python) && fs.existsSync(entry)) return { executable: python, args: [entry] };
    }
    throw new Error('Windows Python worker is not packaged');
  }
  return { executable, args: [] };
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1120, height: 760, minWidth: 900, minHeight: 620,
    backgroundColor: '#f5f7fb', show: process.env.CAMERA_RECOVERY_SMOKE !== '1',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, nodeIntegration: false, sandbox: true
    }
  });
  mainWindow.setMenuBarVisibility(false);
  mainWindow.loadFile(path.join(__dirname, 'index.html'));
  if (process.env.CAMERA_RECOVERY_SMOKE === '1') mainWindow.webContents.once('did-finish-load', async () => {
    try {
      const result = await mainWindow.webContents.executeJavaScript(`(async () => {
        document.getElementById('selectEnglish').click();
        document.getElementById('checkBoard').click();
        for (let i = 0; i < 100; i++) {
          if (document.getElementById('boardResult').textContent.includes('ALREADY_INSTALLED')) break;
          await new Promise(resolve => setTimeout(resolve, 50));
        }
        if (document.getElementById('appVersion').textContent !== ${JSON.stringify(app.getVersion())}) throw new Error('Header release version does not match the installed app');
        document.getElementById('languageButton').click();
        document.getElementById('selectChinese').click();
        const chineseResult = document.getElementById('boardResult').textContent;
        const bootEnglish = 'Disconnect USB, hold BOOT while reconnecting, then release BOOT and check again';
        const bootResult = {status:'NEEDS_USER_ACTION', summary:'RP2040 runtime port did not answer known bridge PING',
                            next_action:bootEnglish};
        const bootMessage = taskText(bootResult);
        setText('boardResult', bootMessage);
        if (!document.getElementById('boardResult').textContent.includes('按住 RP2040 上的 BOOT 键') ||
            document.getElementById('boardResult').textContent.includes(bootEnglish) ||
            !window.localization.translate('Review the saved native NOR receipt before another camera operation').includes('恢复日志'))
          throw new Error('Chinese worker instructions were not localized');
        document.getElementById('languageButton').click();
        document.getElementById('selectEnglish').click();
        if (!document.getElementById('boardResult').textContent.includes(bootEnglish))
          throw new Error('English worker instructions did not return on language switch');
        state.boardJob = 'fixture-board-needs-boot';
        await checkBoard();
        const bootSteps = document.getElementById('bootSteps');
        const bootStyle = getComputedStyle(bootSteps);
        if (bootSteps.hidden || Number(bootStyle.fontWeight) < 700 ||
            bootStyle.color !== 'rgb(185, 28, 28)' ||
            !bootSteps.textContent.includes('release BOOT'))
          throw new Error('BOOTSEL instructions must be visible, bold red, and include releasing BOOT');
        state.boardJob = 'fixture-board-boot-ready';
        await checkBoard();
        if (!bootSteps.hidden) throw new Error('BOOTSEL instructions still shown after entering BOOTSEL');
        state.boardReadyToInstall = false;
        state.boardJob = null;
        setText('boardResult', taskText({status:'ALREADY_INSTALLED', summary:'Fake adapter checked'}));
        if (!taskText({status:'PARTIAL',success_with_skips:true,skipped_regions:[{name:'cp-nor'}]}).includes('cp-nor')) throw new Error('Partial skipped regions missing');
        if (!taskText({status:'VERIFIED',success_with_skips:true}).includes('详情缺失')) throw new Error('Missing skip details warning missing');
        if (taskText({status:'INCOMPLETE',success_with_skips:true,skipped_regions:[{name:'cp-nor'}]}).includes('跳过区域')) throw new Error('Incomplete result incorrectly shown as skip success');
        const pages = [...document.querySelectorAll('.nav')].map(item => item.dataset.page);
        document.querySelector('[data-page="prepareCamera"]').click();
        document.getElementById('cameraReady').click();
        if (!document.getElementById('connection').classList.contains('active')) throw new Error('Camera tutorial does not continue');
        page('stock');
        const models = document.getElementById('sdModel');
        const tips = document.getElementById('sdUsageTips');
        const downloads = document.getElementById('moreFirmwareDownloads');
        if (tips.open || downloads.hidden || downloads.textContent !== 'More download links 🔗')
          throw new Error('A7 IV downloads or collapsed usage tips missing');
        tips.querySelector('summary').click();
        if (!tips.open || tips.querySelectorAll('li').length !== 3)
          throw new Error('SD usage tips did not expand');
        tips.querySelector('summary').click();
        models.value = 'ILCE-7RM5'; models.dispatchEvent(new Event('change'));
        if (document.getElementById('adapterRecovery').hidden || downloads.hidden ||
            !document.getElementById('stockModelLabel').textContent.includes('ILCE-7RM5') ||
            !document.getElementById('adapterRecoveryTitle').textContent.includes('A7R V'))
          throw new Error('R5 guided recovery or model confirmation missing');
        window.localization.applyLanguage('zh');
        if (!document.getElementById('stockModelLabel').textContent.includes('ILCE-7RM5'))
          throw new Error('R5 confirmation reset when switching language');
        window.localization.applyLanguage('en');
        if (!document.getElementById('stockModelLabel').textContent.includes('ILCE-7RM5'))
          throw new Error('R5 English confirmation reset when switching language');
        document.getElementById('preparedCard').click();
        if (document.getElementById('stockReady').hidden) throw new Error('R5 prepared card route missing');
        state.controlVerified = true;
        document.getElementById('stockModel').checked = true;
        document.getElementById('stockPower').checked = true;
        await startStock();
        if (!state.stockJob || !document.getElementById('logText').textContent.includes('Fake R5 stock task prepared'))
          throw new Error('R5 stock task did not receive the selected model');
        document.getElementById('sdBack').click();
        models.value = 'ILME-FX6'; models.dispatchEvent(new Event('change'));
        if (!downloads.hidden || !document.getElementById('adapterRecovery').hidden)
          throw new Error('A7 IV downloads or guidance shown for FX6');
        document.getElementById('officialReady').click();
        if (!document.getElementById('officialText').textContent.includes('slot B')) throw new Error('FX6 card instructions missing');
        models.value = 'ILCE-7M4'; models.dispatchEvent(new Event('change'));
        document.getElementById('preparedCard').click();
        if (document.getElementById('stockReady').hidden || !document.getElementById('stockCard').checked) throw new Error('Prepared card route failed');
        document.getElementById('sdBack').click();
        if (state.sdStep !== 'entry' || document.getElementById('sdEntry').hidden) throw new Error('Prepared card back route failed');
        document.getElementById('helpCard').click();
        if (state.sdStep !== 'source' || !document.getElementById('sdEntry').hidden || !document.getElementById('chooseCard').disabled) throw new Error('SD source step mismatch');
        state.dat = 'BODYDATA.DAT'; state.card = 'E:/'; setSDStep('target');
        if (!document.getElementById('sdSource').hidden || document.getElementById('sdTarget').hidden) throw new Error('SD target step mismatch');
        setSDStep('slot'); document.getElementById('confirmSlot').click();
        if (state.sdStep !== 'ready') throw new Error('SD slot confirmation failed');
        document.getElementById('sdBack').click();
        if (state.sdStep !== 'slot') throw new Error('Helper ready back route failed');
        document.getElementById('sdBack').click();
        if (state.sdStep !== 'target') throw new Error('Slot back route failed');
        showStockOutcome('current'); state.stockStarted = Date.now() - 4000; updateTimers();
        if (!document.getElementById('stockChecks').hidden || !document.getElementById('sdBack').hidden || document.getElementById('stockWaitInstructions').hidden || !document.getElementById('stockTimer').textContent.includes('00:00:04')) throw new Error('Stock wait screen mismatch');
        state.waitingStock = false; state.stockStarted = null; document.getElementById('stockOutcome').hidden = true; document.getElementById('stockChecks').hidden = false; setText('stockTimer',''); setSDStep('entry');
        page('restore'); state.controlVerified = true;
        state.restoreTarget = state.taskRoot + '/snapshot';
        document.getElementById('restorePower').checked = true;
        state.restoreStarted = Date.now();
        const restoreVisible = id => !document.getElementById(id).hidden;
        handleProgress({stage:'restore_reconnect', retry:1, max_retries:3});
        if (!restoreVisible('restoreStage') || restoreVisible('restoreVerify') || restoreVisible('restoreWrite'))
          throw new Error('Restore reconnect spinner missing');
        handleProgress({stage:'restore_local_verify', bytes_done:512, bytes_total:2048});
        if (!restoreVisible('restoreVerify') || restoreVisible('restoreStage') ||
            document.getElementById('restoreVerifyBar').value !== .25 ||
            !document.getElementById('restoreBytes').textContent.includes('512 / 2,048 bytes'))
          throw new Error('Restore byte progress missing');
        handleProgress({stage:'fragment_write_verified', completed_fragments:4, planned_fragments:12,
                        verified_fragments:3, failed_fragments:1, fraction:1/3});
        if (!restoreVisible('restoreWrite') || restoreVisible('restoreVerify') ||
            !document.getElementById('restoreFragments').textContent.includes('Processed 4/12'))
          throw new Error('Restore fragment progress missing');
        handleProgress({stage:'restore_nor_wait', elapsed_seconds:3});
        if (!state.norStarted || !restoreVisible('restoreStage') || restoreVisible('restoreWrite') ||
            !document.getElementById('norTimer').textContent.includes('Startup data'))
          throw new Error('NOR stage did not replace fragment progress');
        await startRestore(); const firstRestore = state.restoreJob;
        if (!state.restoreFailure || !document.getElementById('startRestore').textContent.includes('Start fresh')) throw new Error('Failed restore restart missing');
        await startRestore();
        if (firstRestore === state.restoreJob) throw new Error('Restore reused failed job');
        state.restoreTarget = state.taskRoot + '/fixture-restore-success/snapshot';
        await startRestore();
        if (state.restoreFailure || state.verified !== 12 ||
            !document.getElementById('restoreResult').textContent.includes('startup data rebuild verified'))
          throw new Error('Full restore did not show the verified eMMC and NOR outcome');
        state.backupStarted = Date.now() - 2000000; state.backupFinished = null; state.backupVerified = false;
        updateTimers();
        if (document.getElementById('backupProgress').value !== .95) throw new Error('Backup progress must cap at 95%');
        state.backupVerified = true; updateTimers();
        if (document.getElementById('backupProgress').value !== 1) throw new Error('Verified backup must reach 100%');
        state.backupStarted = null;
        document.querySelector('[data-page="backup"]').click();
        const get = id => document.getElementById(id);
        const waitFor = async (predicate, message) => {
          for (let i = 0; i < 160; i++) {
            if (predicate()) return;
            await new Promise(resolve => setTimeout(resolve, 35));
          }
          throw new Error(message);
        };
        const selectBackupFixture = name => {
          state.backupParent = state.taskRoot;
          state.backupJob = state.taskRoot + '/fixture-backup-' + name;
          state.backupRecovery = null;
          updateBackupRecovery();
          get('backupPower').checked = get('backupSpace').checked = true;
          setText('backupResult', '');
        };
        selectBackupFixture('same-anchor');
        get('startBackup').click();
        await waitFor(() => state.backupVerified && !state.backupJob, 'Matching backup did not finish normally');
        if (!get('backupRecovery').hidden) throw new Error('Matching anchors showed a recapture');

        selectBackupFixture('changed-anchor');
        get('startBackup').click();
        await waitFor(() => state.backupVerified && !state.backupJob,
                      'Continued backup did not finish');
        if (!get('backupRecovery').hidden)
          throw new Error('Continued backup opened the regional recapture UI');
        page('connection');
        const remove = document.getElementById('removeFastbootWinUsb');
        if (remove.textContent !== 'Uninstall USB driver' ||
            document.getElementById('fastbootDriver').hidden !== true)
          throw new Error('Optional WinUSB removal must be separate from setup');
        remove.click();
        await waitFor(() => document.getElementById('fastbootRemovalResult').textContent.includes('Disconnect the camera DATA USB'),
                      'Driver removal did not report the read-only inspection result');
        state.backupStarted = null;
        page('board');
        return { pages, modelCount: models.options.length, freshRestore: firstRestore !== state.restoreJob, title: document.getElementById('pageTitle').textContent,
          result: document.getElementById('boardResult').textContent,
          chineseResult,
          language: document.documentElement.lang,
          log: document.getElementById('logText').textContent,
          untranslated: (document.querySelector('.shell').textContent.match(/[\u3400-\u9fff]+/g) || []) };
      })()`);
      if (result.pages.length !== 6 || result.modelCount !== 11 || !result.freshRestore || result.language !== 'en' || !result.title.includes('Multi adapter') ||
          !result.result.includes('ALREADY_INSTALLED') ||
          !result.chineseResult.includes('转接头已安装') || !result.log.includes('reconcile') ||
          result.untranslated.length)
        throw new Error(`Electron smoke failed: ${JSON.stringify(result)}`);
      process.stdout.write(`ELECTRON_UI_SMOKE_OK ${JSON.stringify(result)}\n`);
      app.exit(0);
    } catch (error) {
      process.stderr.write(`ELECTRON_UI_SMOKE_FAILED ${error.stack || error}\n`);
      app.exit(1);
    }
  });
}

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });

ipcMain.handle('open-firmware-archive', (_event, model) => {
  const urls = {
    'ILCE-7M4': 'https://bluepotato.shop/a74images.html',
    'ILCE-7RM5': 'https://bluepotato.shop/a7r5images.html'
  };
  if (!Object.hasOwn(urls, model)) throw new Error('No firmware archive for this camera model');
  return shell.openExternal(urls[model]);
});

ipcMain.handle('open-support', (_event, model) => {
  const models = ['ILCE-7M4','ILCE-7RM5','ILCE-7SM3','ILCE-1','ILCE-1M2','ILCE-9M3','ILME-FX2','ILME-FX3','ILME-FX3A','ILME-FX6','ILME-FX30'];
  if (!models.includes(model)) throw new Error('Unsupported camera model');
  const product = model === 'ILME-FX6' ? 'ilme-fx6v' : model.toLowerCase();
  return shell.openExternal(`https://www.sony.com/electronics/support/product/${product}/downloads`);
});

ipcMain.handle('fastboot-driver-status', async () => {
  if (process.platform !== 'win32') return 'unknown';
  try {
    const result = await fastbootDriverStatus();
    log(`Fastboot Windows PnP: ${result}${result === 'driver_missing' ? ' (Code 28)' : ''}`);
    return result;
  } catch (error) {
    log(`Fastboot Windows PnP query failed: ${error.message}`);
    return 'unknown';
  }
});

ipcMain.handle('open-fastboot-driver', async () => {
  if (process.platform !== 'win32' || workerBusy) throw new Error('Driver setup is not available now');
  workerBusy = true;
  try {
    if (await fastbootDriverStatus() !== 'driver_missing') throw new Error('A single Fastboot device with Code 28 is required');
    const directory = path.join(process.resourcesPath, 'fastboot-driver');
    const preset = path.join(directory, 'A7M4-Fastboot.cfg');
    const installer = path.join(directory, 'zadig.exe');
    if (!fs.existsSync(preset) || !fs.existsSync(installer)) throw new Error('Offline WinUSB files are missing');
    shell.showItemInFolder(preset);
    const error = await shell.openPath(installer);
    if (error) throw new Error(error);
  } finally {
    workerBusy = false;
  }
});

ipcMain.handle('remove-fastboot-winusb', async (_event, language) => {
  const smoke = !app.isPackaged && process.env.CAMERA_RECOVERY_SMOKE === '1' && process.platform !== 'win32';
  if (workerBusy || (!smoke && process.platform !== 'win32'))
    throw new Error('Driver removal is not available now');
  workerBusy = true;
  try {
    return await removeFastboot({
      driver: smoke ? { inspect: async () => ({ code: 'present' }) } : windowsExecutor(),
      showDialog: options => dialog.showMessageBox(mainWindow, options),
      language,
      log
    });
  } finally {
    workerBusy = false;
  }
});

ipcMain.handle('paths', () => ({ taskRoot: path.join(userRoot(), 'Tasks'), platform: process.platform, version: app.getVersion() }));

ipcMain.handle('choose', async (_event, kind) => {
  const properties = kind === 'dat' ? ['openFile'] : ['openDirectory'];
  const options = { properties, filters: kind === 'dat' ? [{ name: 'Sony DAT', extensions: ['dat'] }] : undefined };
  const selection = await dialog.showOpenDialog(mainWindow, options);
  return selection.canceled ? null : selection.filePaths[0];
});

ipcMain.handle('backup-destination', (_event, selected) => {
  if (typeof selected !== 'string') throw new Error('Choose a backup save location');
  const receipt = path.join(selected, 'task.json');
  if (fs.existsSync(receipt)) {
    const task = JSON.parse(fs.readFileSync(receipt, 'utf8'));
    if (task.kind !== 'full_backup') throw new Error('Choose a save location or an existing full backup task folder');
    return { parent: path.dirname(selected), job: selected };
  }
  if (path.basename(selected).startsWith('full_backup-')) throw new Error('The backup folder has no valid task record');
  return { parent: selected, job: null };
});

ipcMain.handle('resolve-snapshot', (_event, selected) => {
  if (typeof selected !== 'string') throw new Error('Choose a backup folder');
  const task = path.join(selected, 'task.json');
  if (path.basename(selected).startsWith('scoped_restore-') ||
      (fs.existsSync(task) && JSON.parse(fs.readFileSync(task, 'utf8')).kind === 'scoped_restore'))
    throw new Error('Old restore tasks cannot resume. Choose a verified snapshot or backup folder to create a fresh restore task.');
  const direct = path.join(selected, 'verified.json');
  const nested = path.join(selected, 'snapshot', 'verified.json');
  if (fs.existsSync(direct) && fs.existsSync(nested))
    throw new Error('Both selected folder and its snapshot child contain verified.json; choose the snapshot folder explicitly');
  if (fs.existsSync(direct)) return selected;
  if (fs.existsSync(nested)) return path.join(selected, 'snapshot');
  throw new Error('Selected folder has no verified snapshot receipt');
});

ipcMain.handle('read-logs', () => {
  const file = path.join(userRoot(), 'native-session.log');
  if (!fs.existsSync(file)) return '';
  const raw = fs.readFileSync(file, 'utf8');
  return raw;
});

ipcMain.handle('confirm', async (_event, title, detail, language) => {
  if (!app.isPackaged && process.platform !== 'win32' && process.env.CAMERA_RECOVERY_SMOKE === '1') {
    return title !== 'Confirm the same physical camera' || ++smokeSameCameraRequests > 1;
  }
  const response = await dialog.showMessageBox(mainWindow, {
    type: 'warning', buttons: language === 'en' ? ['Cancel', 'Continue'] : ['取消', '继续'],
    defaultId: 0, cancelId: 0,
    title: String(title), message: String(detail), noLink: true
  });
  return response.response === 1;
});

ipcMain.handle('worker', async (event, request) => {
  if (workerBusy) throw new Error('Another camera task is still running');
  if (!request || typeof request !== 'object' || typeof request.action !== 'string') {
    throw new Error('Invalid worker action');
  }
  const { executable, args } = workerCommand();
  workerBusy = true;
  try {
    log(`Started ${request.action}`);
    return await new Promise((resolve, reject) => {
      const child = spawn(executable, args, {
        windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }
      });
      let lineBuffer = '';
      let stderr = '';
      let finalResult;
      let finalError;
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', chunk => {
        lineBuffer += chunk;
        for (;;) {
          const newline = lineBuffer.indexOf('\n');
          if (newline < 0) break;
          const line = lineBuffer.slice(0, newline);
          lineBuffer = lineBuffer.slice(newline + 1);
          let message;
          try { message = JSON.parse(line); } catch { continue; }
          if (message.type === 'progress') {
            event.sender.send('worker-progress', message.event);
            log(`${message.event?.stage || 'progress'}: ${message.event?.message || ''}`);
          } else if (message.type === 'result') finalResult = message.result;
          else if (message.type === 'error') finalError = message;
        }
      });
      child.stderr.setEncoding('utf8');
      child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-8192); });
      child.on('error', reject);
      child.on('close', code => {
        if (finalError) {
          log(`Worker failed: ${finalError.error || 'unknown error'}; diagnostics=${JSON.stringify({
            action: request.action, error_type: finalError.error_type,
            errno: finalError.errno, winerror: finalError.winerror,
            filename: finalError.filename, filename2: finalError.filename2,
            output_dir: request.output_dir, job_dir: request.job_dir
          })}`);
          reject(Object.assign(new Error(finalError.error || 'Worker failed'), { detail: finalError }));
        } else if (finalResult) {
          log(`Result: ${finalResult.status || 'unknown'}; ${finalResult.summary || ''}`);
          resolve(finalResult);
        } else reject(new Error(`Worker returned no result (exit ${code}): ${stderr}`));
      });
      child.stdin.end(JSON.stringify(request) + '\n');
    });
  } finally { workerBusy = false; }
});

ipcMain.handle('export-logs', async (_event, taskDirs = []) => {
  const choice = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory'] });
  if (choice.canceled) return null;
  const output = path.join(choice.filePaths[0], `Camera-Recovery-logs-${Date.now()}`);
  fs.mkdirSync(output);
  const ownLog = path.join(userRoot(), 'native-session.log');
  if (fs.existsSync(ownLog)) fs.copyFileSync(ownLog, path.join(output, 'native-session.log'));
  const smallReceipts = ['task.json', 'manifest.json', 'journal.json', 'result.json',
    'capture-receipt.json', 'verified.json', 'stock-composite-plan.json',
    'camera-update-complete.json', 'camera-update-failed.json', 'restore-start.json',
    'stock-authorization.json', 'nor-flow.json', 'nor-admission.json', 'restore-result.json',
    'nor-rebuild/job.json'];
  for (const [index, directory] of taskDirs.slice(-12).entries()) {
    if (typeof directory !== 'string' || !fs.existsSync(directory)) continue;
    const destination = path.join(output, `task-${index + 1}`);
    fs.mkdirSync(destination);
    for (const name of smallReceipts) {
      const source = path.join(directory, name);
      if (fs.existsSync(source) && fs.statSync(source).isFile() && fs.statSync(source).size <= 2 * 1024 * 1024) {
        fs.mkdirSync(path.dirname(path.join(destination, name)), { recursive: true });
        fs.copyFileSync(source, path.join(destination, name));
      }
    }
  }
  return output;
});
