const { execFile } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { promisify } = require('node:util');
const {createHash} = require('node:crypto');

const execute = promisify(execFile);
const inspectScript = fs.readFileSync(path.join(__dirname, 'driver-inspect.ps1'), 'utf8');
const instance = 'USB\\VID_18D1&PID_4EE0\\ILCE7M4-fastboot';
const labels = {
  title: ['Remove Fastboot WinUSB', '移除 Fastboot WinUSB'],
  busy: ['Wait for the current task to finish.', '请等待当前任务结束。'],
  target: ['Windows did not identify the exact camera Fastboot instance.', 'Windows 未找到对应的相机 Fastboot 实例。'],
  present: ['Disconnect the camera DATA USB, then try again.', '请断开相机的 DATA USB 后重试。'],
  package: ['The bound driver is not a recognized dedicated Zadig WinUSB package.', '当前绑定的驱动包未能确认为 Zadig 专用 WinUSB。'],
  query: ['Windows could not verify the Fastboot driver binding.', 'Windows 无法核实 Fastboot 驱动绑定。'],
  shared: ['Windows could not prove that only this Fastboot instance uses the package.', 'Windows 无法确认该驱动包仅由此 Fastboot 实例使用。'],
  changed: ['The binding or package changed after confirmation. Try again.', '确认后驱动绑定或驱动包发生变化，请重试。'],
  canceled: ['Driver removal was canceled.', '已取消驱动移除。'],
  failed: ['Windows did not report successful driver removal. See the activity log.', 'Windows 未报告驱动移除成功，请查看运行日志。'],
  removed: ['The dedicated Fastboot WinUSB package was removed.', '已移除 Fastboot 专用 WinUSB 驱动包。']
};
const encoded = script => Buffer.from(script, 'utf16le').toString('base64');

function windowsExecutor(run = execute) {
  const powershell = path.win32.join(process.env.SystemRoot || 'C:\\Windows',
    'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const start = async (args, options) => run(powershell,
    ['-NoLogo', '-NoProfile', '-NonInteractive', ...args],
    {windowsHide: true, maxBuffer: 32768, ...options});
  return {
    inspect: async () => {
      const { stdout } = await start(['-EncodedCommand', encoded(inspectScript +
        '\ntry { Inspect-Target | ConvertTo-Json -Compress } catch { @{code="query"} | ConvertTo-Json -Compress }\n')],
      {timeout: 30000});
      return JSON.parse(stdout);
    },
    remove: async ({inf, digest}) => {
      if (!/^oem[0-9]+\.inf$/i.test(inf) || !/^[a-f0-9]{64}$/i.test(digest)) return 'query';
      const payload = encoded(inspectScript +
        "\ntry { exit (Remove-CheckedPackage '" + inf + "' '" + digest + "') } catch { exit 21 }\n");
      // The outer invocation uses a single encoding layer to stay below
      // CreateProcess's command line limit. Only base64 enters the command.
      const starter = [
        "$ErrorActionPreference='Stop'",
        "try { $p=Start-Process -FilePath (Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\\v1.0\\powershell.exe')",
        "-ArgumentList @('-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand','" + payload + "')",
        "-Verb RunAs -Wait -PassThru; exit $p.ExitCode } catch {",
        '$e=$_.Exception; while ($e.InnerException) { $e=$e.InnerException }',
        'if ($e.NativeErrorCode -eq 1223) { exit 23 }; exit 24 }'
      ];
      try {
        await start(['-Command', starter[0] + '; ' + starter.slice(1).join(' ')]);
        return 'removed';
      } catch (error) {
        if (error.code === 21) return 'changed';
        if (error.code === 23) return 'canceled';
        return 'failed';
      }
    }
  };
}

async function removeFastboot({driver, showDialog, log, language}) {
  const lang = language === 'en' ? 0 : 1;
  const label = code => (labels[code] || labels.query)[lang];
  const report = (code, detail = '') => {
    log('Fastboot WinUSB: ' + code + (detail ? ' (' + detail + ')' : ''));
    return {code, message: label(code)};
  };
  let binding;
  try {
    binding = await driver.inspect();
  } catch (error) {
    return report('query');
  }
  if (binding?.code !== 'eligible') return report(binding?.code || 'query');
  if (!/^oem[0-9]+\.inf$/i.test(binding.inf) || !/^[a-f0-9]{64}$/i.test(binding.digest)) return report('query');
  const {response} = await showDialog({
    type: 'warning', title: label('title'), defaultId: 0, cancelId: 0,
    buttons: lang === 0 ? ['Cancel', 'Remove driver'] : ['取消', '移除驱动'],
    message: lang === 0 ? 'Remove the Zadig WinUSB driver for this camera Fastboot interface?' :
      '移除用于此相机 Fastboot 接口的 Zadig WinUSB 驱动？',
    detail: instance + '\n' + binding.inf
  });
  if (response !== 1) return report('canceled', binding.inf);
  return report(await driver.remove(binding), binding.inf);
}

const driverAssets = {
  'zadig.exe': '4ecaa95df3da3621486a043aef8b3050b8bafe7c901402871e816229ef82039b',
  'A7M4-Fastboot.cfg': '5b9fea8decd8eae09e3babc5ebaf44c0b35986211ccd943b90f70b4b0a2e4488',
  'zadig.ini': '7768c5640b48cb6c4ae14c57e9c06082a46c24db952a8d7c3dab56f19f601768'
};

async function openDriverSetup({probe, confirm, showPreset, launch, directory,
  read = fs.promises.readFile}) {
  const eligible = result => result.mode === 'driver' && result.installable === true;
  const initial = await probe();
  if (!eligible(initial)) return {code: 'connection', connection: initial};
  if (!await confirm()) return {code: 'canceled'};
  for (const [name, expected] of Object.entries(driverAssets)) {
    const filename = path.join(directory, name);
    const hash = createHash('sha256').update(await read(filename)).digest('hex');
    if (hash !== expected) throw new Error('Installed Zadig files do not match the verified release assets.');
  }
  const current = await probe();
  if (!eligible(current)) return {code: 'connection', connection: current};
  showPreset(path.join(directory, 'A7M4-Fastboot.cfg'));
  const error = await launch(path.join(directory, 'zadig.exe'));
  if (error) throw new Error(error);
  return {code: 'opened'};
}

module.exports = {removeFastboot, windowsExecutor, openDriverSetup};
