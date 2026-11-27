const assert = require('node:assert/strict');
const {test} = require('node:test');
const {removeFastboot, windowsExecutor, openDriverSetup} = require('../attach_profile/driver-maintenance');
const path = require('node:path');

const eligible = {code: 'eligible', inf: 'oem123.inf', digest: 'a'.repeat(64)};
const call = (inspect, response, remove = async () => assert.fail('unexpected removal')) => removeFastboot({
  driver: {inspect, remove}, language: 'en', log: () => {},
  showDialog: async options => {
    assert.notEqual(response, undefined, 'unexpected confirmation prompt');
    assert.equal(options.defaultId, 0);
    assert.match(options.detail, /ILCE7M4-fastboot\noem123\.inf$/);
    return {response};
  }
});
test('refusals never request confirmation or removal', async () => {
  for (const code of ['target', 'query', 'package', 'present', 'shared']) {
    assert.equal((await call(async () => ({code}))).code, code);
  }
  assert.equal((await call(async () => { throw Error('access denied'); })).code, 'query');
  assert.equal((await call(async () => ({...eligible, inf: 'winusb.inf'}))).code, 'query');
});
test('application cancellation prevents elevation; confirmation uses only the attested package', async () => {
  assert.equal((await call(async () => eligible, 0)).code, 'canceled');
  const result = await call(async () => eligible, 1, async binding => {
    assert.deepEqual(binding, eligible);
    return 'removed';
  });
  assert.equal(result.code, 'removed');
});
test('Windows elevation and exit statuses are mapped without executing any command', async () => {
  for (const [code, expected] of [[0, 'removed'], [21, 'changed'], [22, 'failed'], [23, 'canceled'], [24, 'failed']]) {
    const executor = windowsExecutor(async (executable, args) => {
      assert.match(executable, /\\System32\\WindowsPowerShell\\v1\.0\\powershell\.exe$/i);
      assert.equal(args[3], '-Command');
      assert.match(args[4], /-Verb RunAs -Wait -PassThru/);
      const payload = args[4].match(/'-EncodedCommand','([^']+)'/)[1];
      const script = Buffer.from(payload, 'base64').toString('utf16le');
      assert.match(script, /Remove-CheckedPackage 'oem123\.inf' '[a-f0-9]{64}'/);
      assert.match(script, /\/delete-driver \$inf \/uninstall/);
      assert.doesNotMatch(script, /\/force|\/reboot|\/add-driver/);
      if (code) throw Object.assign(Error('mock error'), {code});
    });
    assert.equal(await executor.remove(eligible), expected);
  }
  const executor = windowsExecutor(async () => assert.fail('unsafe INF reached command execution'));
  assert.equal(await executor.remove({...eligible, inf: 'winusb.inf'}), 'query');
});

test('Zadig setup needs Code 28 at confirmation and launch, and verifies its assets', async () => {
  const directory = process.env.CAMERA_DRIVER_TEST_ASSETS ||
    path.resolve(__dirname, '../../build/windows-driver');
  const missing = {mode: 'driver', installable: true};
  const noLaunch = () => assert.fail('unexpected Zadig launch');
  const options = {directory, probe: async () => missing, confirm: async () => true,
    launch: noLaunch, showPreset: noLaunch};
  for (const initial of [{mode: 'msc'}, {mode: 'unknown'}, {mode: 'driver'}, {mode: 'fastboot'}]) {
    assert.equal((await openDriverSetup({
      ...options, probe: async () => initial, confirm: noLaunch
    })).code, 'connection');
  }
  assert.equal((await openDriverSetup({...options, confirm: async () => false})).code, 'canceled');
  await assert.rejects(openDriverSetup({...options, read: async () => Buffer.from('changed')}), /verified release assets/);
  let calls = 0;
  assert.equal((await openDriverSetup({
    ...options, probe: async () => ++calls === 1 ? missing : {mode: 'msc'}
  })).code, 'connection');
  const dispatched = [];
  assert.equal((await openDriverSetup({...options,
    showPreset: filename => dispatched.push(path.basename(filename)),
    launch: async filename => { dispatched.push(path.basename(filename)); return ''; }
  })).code, 'opened');
  assert.deepEqual(dispatched, ['A7M4-Fastboot.cfg', 'zadig.exe']);
});
