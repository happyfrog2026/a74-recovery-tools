"""Complete-tuple admission and offline stock transaction; no device access."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import stock_profiles, stock_sd, stock_trigger, stock_updater_source
from recovery_tool.stock_common import StockRecoveryError
from recovery_tool.stock_fat import Fat
from recovery_tool.test_stock_sd import settings_fixture
from recovery_tool.task_backend import Backend
from recovery_tool import write_transaction as tx
from recovery_tool.updater_restore import Fragment, replay


def boot_fixture(tag):
    image = bytearray(settings_fixture())
    image[1536:3584] = bytes(2048)
    image[3584:] = bytes(len(image) - 3584)
    fat = bytearray(512)
    fat[:3] = b'\xf8\xff\xff'
    for cluster in range(2, 9):
        off = cluster * 3 // 2
        value = int.from_bytes(fat[off:off+2], 'little')
        value = (value & 15) | 0xfff0 if cluster & 1 else (value & 0xf000) | 0xfff
        fat[off:off+2] = value.to_bytes(2, 'little')
    image[512:1024] = image[1024:1536] = fat
    def row(offset, name, cluster, content, attr=0x20):
        image[offset:offset+11] = name
        image[offset+11] = attr
        struct.pack_into('<HI', image, offset+26, cluster, len(content))
        image[3584 + (cluster-2)*512:3584+(cluster-2)*512+len(content)] = content
    row(1536, b'BOOT       ', 2, b'', 0x10)
    files = {'boot/initrd.img': b'initrd-'+tag, 'boot/vmlinux.bin': b'kernel-'+tag,
             'boot/cxd90057.dtb': b'dtb-'+tag, 'boot/kemco.txt': b'kemco',
             'boot/loadaddr.txt': b'loadaddr'}
    files['p1chksum'] = b''.join(hashlib.md5(files[name]).hexdigest().encode()+b'  ./'+name.encode()+b'\n'
                                for name in ('boot/vmlinux.bin', 'boot/initrd.img'))
    for index, (path, name) in enumerate(zip(stock_profiles.BOOT_FILES,
                  (b'INITRD  IMG', b'VMLINUX BIN', b'CXD90057DTB', b'KEMCO   TXT', b'LOADADDRTXT', b'P1CHKSUM   '))):
        row(1568 if index == 5 else 3584+index*32, name, index+3, files[path])
    return bytes(image).ljust(25_100_288, b'\0'), files


def profile(files, versions):
    return {'versions': versions, 'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                                           for name, data in files.items()}}


class ProfilesTest(unittest.TestCase):
    def test_r5_accepts_live_checksum_pair_without_m4_profile(self):
        image, _ = boot_fixture(b'r5')
        evidence = stock_profiles.inspect_environment(image, camera_model="ILCE-7RM5")
        self.assertEqual(evidence["camera_model"], "ILCE-7RM5")
        self.assertEqual(evidence["profile_id"], "checksum-verified-live-ILCE-7RM5")
        with self.assertRaises(StockRecoveryError):
            stock_profiles.inspect_environment(image)
        damaged = Fat(image)
        damaged.replace('p1chksum', b'x' * len(damaged.read('p1chksum')))
        with self.assertRaisesRegex(StockRecoveryError, 'MD5'):
            stock_profiles.inspect_environment(damaged.bytes(), camera_model="ILCE-7RM5")

    def test_r5_partition_mapping_accepts_body_specific_sector_bytes(self):
        from recovery_tool.updater_restore import SIZE, SETTINGS_SIZE, RestoreError
        sector = bytearray(512)
        sector[:4] = b'8246'
        struct.pack_into('<4I', sector, 32, 128, SIZE // 512, 5, 1)
        struct.pack_into('<4I', sector, 48, 49152, SETTINGS_SIZE // 512, 5, 1)
        sector[200:210] = b'R5specific'
        stock_trigger._verify_sdm2(bytes(sector), camera_model="ILCE-7RM5")
        struct.pack_into('<I', sector, 52, 123)
        with self.assertRaisesRegex(RestoreError, 'nflasha2 SDM2 mapping mismatch.*expected.*actual'):
            stock_trigger._verify_sdm2(bytes(sector), camera_model="ILCE-7RM5")

    def test_r5_model_is_saved_as_model_without_path_normalization(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder) / 'r5-job'
            Backend().prepare('stock_sd_recovery',
                              {'sd_ready': True, 'camera_model': 'ILCE-7RM5'}, job)
            task = json.loads((job / 'task.json').read_text())
            self.assertEqual(task['inputs']['camera_model'], 'ILCE-7RM5')

    def test_catalogue_covers_all_packages_without_inventing_110_p1(self):
        catalog = stock_profiles.CATALOG
        self.assertEqual(len(catalog['packages']), 14)
        self.assertEqual(len(catalog['profiles']), 5)
        packages = {p['version']: p for p in catalog['packages'].values()}
        self.assertIsNone(packages['1.10']['profile'])
        for version, package in packages.items():
            if version != '1.10':
                self.assertIn(version, catalog['profiles'][package['profile']]['versions'])
        self.assertEqual(packages['3.01']['profile'], packages['6.02']['profile'])

    def test_complete_tuple_passthrough_needs_no_assets_and_rejects_mix(self):
        first, one = boot_fixture(b'one')
        _, two = boot_fixture(b'two')
        catalogue = {'profiles': {'one': profile(one, ['1.05']), 'two': profile(two, ['3.00'])}}
        with patch.dict(stock_profiles.CATALOG, catalogue):
            with patch.object(stock_updater_source, 'ASSET_DIR', Path('/does-not-exist')):
                out, evidence = stock_updater_source.reconstruct(first)
            self.assertIs(out, first)
            self.assertEqual(evidence['changed_sectors'], [])
            self.assertEqual(evidence['environment']['profile_id'], 'one')
            mixed = Fat(first)
            mixed.replace('boot/initrd.img', two['boot/initrd.img'])
            # Even internally consistent checksum entries cannot authorize a cross-release pair.
            pair = {**one, 'boot/initrd.img': two['boot/initrd.img']}
            mixed.replace('p1chksum', b''.join(hashlib.md5(pair[n]).hexdigest().encode()+b'  ./'+n.encode()+b'\n'
                                            for n in ('boot/vmlinux.bin', 'boot/initrd.img')))
            with self.assertRaises(StockRecoveryError):
                stock_profiles.inspect_environment(mixed.bytes())
            with self.assertRaisesRegex(StockRecoveryError, 'Mixed or damaged official boot tuple'):
                stock_updater_source.reconstruct(mixed.bytes())
            damaged = Fat(first)
            damaged.replace('p1chksum', b'x' * len(one['p1chksum']))
            # This remains blocked even if the official initrd is the 6.02 repair target.
            with patch.object(stock_updater_source, 'STOCK_INITRD_SHA256', hashlib.sha256(one['boot/initrd.img']).hexdigest()):
                with self.assertRaisesRegex(StockRecoveryError, 'Mixed or damaged official boot tuple'):
                    stock_updater_source.reconstruct(damaged.bytes())

    def test_prepare_plan_run_pins_original_tuple_and_reboots_once(self):
        updater, files = boot_fixture(b'older')
        settings = settings_fixture(b'\0\x03')
        dat = b'official target differs from resident updater'
        digest = hashlib.sha256(dat).hexdigest()
        catalogue = {'profiles': {'older': profile(files, ['3.00'])},
                     'packages': {digest: {'version': '6.02', 'bytes': len(dat), 'profile': 'newer'}}}
        live = {'nflasha': b'M'*512, 'nflasha1': updater, 'nflasha2': settings}
        writes, reboots = [], []
        class Transport:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def close(self): pass
            def region_size(self, name): return len(live[name])
            def read_region(self, name, output, *, offset=0, length=None, **_):
                Path(output).write_bytes(live[name][offset:offset+length if length else None])
            def _send(self, command): reboots.append(command)
            def _reply(self): return b'OKAY', b''
        def flash(transport, name, sparse, sectors, **_):
            writes.append(name)
            live[name] = replay(live[name], [Fragment(0, sectors, sparse)])
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(stock_profiles.CATALOG, catalogue), \
             patch.object(stock_sd, 'SETTINGS_SIZE', len(settings)), \
             patch.object(stock_trigger, 'SETTINGS_SIZE', len(settings)), \
             patch('recovery_tool.task_backend.verify_stock_sdm2'), \
             patch('recovery_tool.task_backend.Backend._transport', return_value=Transport()), \
             patch.object(tx, '_write_response', side_effect=flash):
            root = Path(folder)
            source = root/'BODYDATA.DAT'
            source.write_bytes(dat)
            backend = Backend(transport_factory=Transport)
            prepared = backend.prepare('stock_sd_recovery', {'dat': str(source)}, root/'job')
            self.assertEqual(prepared['target_package_version'], '6.02')
            plan = backend.stock_plan(root/'job')
            self.assertEqual(plan['status'], 'READY', plan)
            self.assertEqual(plan['updater_environment']['official_versions'], ['3.00'])
            self.assertFalse((root/'job/updater-stage').exists())
            self.assertEqual(writes, [])
            self.assertEqual(reboots, [])
            result = backend.stock_run(root/'job', confirmed_plan_digest=plan['plan_digest'])
            self.assertTrue(result['stock_reboot_acknowledged'], result)
            self.assertEqual(result['updater_environment']['profile_id'], 'older')
            self.assertEqual(live['nflasha1'], updater)
            self.assertEqual(writes, ['nflasha2'])
            self.assertEqual(reboots, ['reboot'])
            backend.stock_run(root/'job')
            self.assertEqual(reboots, ['reboot'])
            self.assertEqual(backend.verify(root/'job')['updater_environment']['profile_id'], 'older')

    def test_r5_plan_run_unknown_m4_tuple_writes_trigger_and_reboots_once(self):
        updater, _ = boot_fixture(b'r5-unknown-to-m4')
        settings = settings_fixture(b'\x00\x03')
        sector0 = bytearray(512)
        sector0[:4] = b'8246'
        struct.pack_into('<4I', sector0, 32, 128, len(updater) // 512, 5, 1)
        struct.pack_into('<4I', sector0, 48, 49152, len(settings) // 512, 5, 1)
        sector0[200:210] = b'R5specific'
        live = {'nflasha': bytes(sector0), 'nflasha1': updater, 'nflasha2': settings}
        writes, reboots = [], []

        class Transport:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def close(self): pass
            def region_size(self, name): return len(live[name])
            def read_region(self, name, output, *, offset=0, length=None, **_):
                Path(output).write_bytes(live[name][offset:offset+length if length else None])
            def _send(self, command): reboots.append(command)
            def _reply(self): return b'OKAY', b''

        def flash(transport, name, sparse, sectors, **_):
            writes.append(name)
            live[name] = replay(live[name], [Fragment(0, sectors, sparse)])

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(stock_sd, 'SETTINGS_SIZE', len(settings)), \
             patch.object(stock_trigger, 'SETTINGS_SIZE', len(settings)), \
             patch.object(tx, '_write_response', side_effect=flash):
            root = Path(folder)
            backend = Backend(transport_factory=Transport)
            prepared = backend.prepare('stock_sd_recovery',
                {'sd_ready': True, 'camera_model': 'ILCE-7RM5'}, root/'job')
            self.assertEqual(prepared['status'], 'READY')
            plan = backend.stock_plan(root/'job')
            self.assertEqual(plan['status'], 'READY', plan)
            self.assertEqual(plan['updater_environment']['profile_id'],
                             'checksum-verified-live-ILCE-7RM5')
            self.assertFalse((root/'job/updater-stage').exists())
            self.assertEqual(writes, [])
            result = backend.stock_run(root/'job', confirmed_plan_digest=plan['plan_digest'])
            self.assertTrue(result['stock_reboot_acknowledged'], result)
            self.assertEqual(live['nflasha1'], updater)
            self.assertEqual(writes, ['nflasha2'])
            final = Fat(live['nflasha2'])
            self.assertEqual(final.read('updater/dat4'), b'\0\0')
            self.assertEqual(final.read('updater/mode'), b'')
            self.assertEqual(final.read('updater/mode5'), b'')
            self.assertEqual(reboots, ['reboot'])
            backend.stock_run(root/'job')
            self.assertEqual(reboots, ['reboot'])

    def test_receipt_only_target_and_receipt_pin_survive_planning(self):
        updater, files = boot_fixture(b'older')
        settings = settings_fixture()
        source_hash = 'a' * 64
        catalog = {'profiles': {'older': profile(files, ['3.00'])},
                   'packages': {source_hash: {'version': '1.10', 'bytes': 123, 'profile': None}}}
        live = {'nflasha': b'M'*512, 'nflasha1': updater, 'nflasha2': settings}
        class Transport:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read_region(self, name, output, *, offset=0, length=None, **_):
                Path(output).write_bytes(live[name][offset:offset+length if length else None])
        with tempfile.TemporaryDirectory() as folder, patch.dict(stock_profiles.CATALOG, catalog), \
             patch.object(stock_sd, 'SETTINGS_SIZE', len(settings)), \
             patch.object(stock_trigger, 'SETTINGS_SIZE', len(settings)), \
             patch('recovery_tool.task_backend.verify_stock_sdm2'), \
             patch('recovery_tool.task_backend.Backend._transport', return_value=Transport()):
            root = Path(folder)
            receipt = root/'sd-receipt.json'
            receipt.write_text(json.dumps({'schema': 'sd-card-prepare-v1', 'status': 'VERIFIED',
                'readback_verified': True, 'source_sha256': source_hash, 'target_sha256': source_hash,
                'source_bytes': 123, 'target_bytes': 123,
                'volume_identity': {'device_identifier': 'disk-test', 'mount_point': '/test/card'}}))
            backend = Backend(transport_factory=lambda: self.fail('write transport prohibited'))
            prepared = backend.prepare('stock_sd_recovery',
                {'sd_ready': True, 'sd_preparation_receipt': str(receipt)}, root/'job')
            self.assertEqual(prepared['target_package_version'], '1.10')
            self.assertIsNone(prepared['host_dat_sha256'])
            self.assertEqual(prepared['prepared_card_dat_sha256'], source_hash)
            plan = backend.stock_plan(root/'job')
            self.assertEqual(plan['target_package_version'], '1.10')
            self.assertEqual(plan['prepared_card_dat_sha256'], source_hash)
            manifest = json.loads((root/'job/manifest.json').read_text())
            self.assertIn(str(receipt.resolve()), [pin['path'] for pin in manifest['input_files']])
            receipt.write_text(receipt.read_text()+' ')
            result = backend.stock_run(root/'job', confirmed_plan_digest=plan['plan_digest'])
            self.assertEqual(result['status'], 'NO_WRITE')
            self.assertFalse((root/'job/stock-authorization.json').exists())
