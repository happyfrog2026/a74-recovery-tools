"""Fast, synthetic host-evidence tests; no runner, transport, or device access."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from recovery_tool import nor_qualification as q
from recovery_tool import nor_rebuild as w
from recovery_tool.stock_fat import Fat
from recovery_tool.test_stock_sd import settings_fixture


JOB_ID = 'a' * 32


def md5(data):
    return hashlib.md5(data).hexdigest()


def empty_fat():
    """Two MiB FAT12 with enough allocation for the fixed one MiB mailbox."""
    image = bytearray(4096 * 512)
    image[:3] = b'\xeb\x3c\x90'
    struct.pack_into('<HBHBHHBHH', image, 11, 512, 1, 1, 2, 64, 4096, 0xf8, 12, 32)
    image[510:512] = b'\x55\xaa'
    for start in (512, 13 * 512):
        image[start:start+3] = b'\xf8\xff\xff'
    return bytes(image) + b'PHYSICAL-TAIL'.ljust(4096, b'T')


def record(row):
    """Match the runner's bounded newline payload, digest, then commit marker."""
    row = {k: str(v) for k, v in row.items() if k not in ('payload_md5', 'commit')}
    payload = ''.join(f'{key}={value}\n' for key, value in row.items()).encode('ascii')
    data = payload + f'payload_md5={md5(payload)}\ncommit={JOB_ID}\n'.encode('ascii')
    if len(data) > 512:
        raise AssertionError('synthetic record exceeds production slot')
    return data.ljust(512, b'\0')


def mailbox(head, rows):
    result = bytearray(w.initial_mailbox(JOB_ID))
    for name, data in (("companion", b'C' * 417792), ("header", b'H' * 2048),
                       ("loader_backup", b'B' * 512), ("training", b'T' * 1536)):
        start, _ = w.SLOTS[name]
        result[start:start+len(data)] = data
    text = ''.join(f'{key}={value}\n' for key, value in head.items()).encode('ascii')
    result[:4096] = text.ljust(4096, b'\0')
    start, _ = w.SLOTS['ledger']
    for index, row in enumerate(rows):
        result[start+index*512:start+(index+1)*512] = record(row)
    return bytes(result)


class EvidenceFixture:
    def __init__(self, root):
        self.root = root
        self.components = {
            name: {'bytes': len(data), 'md5': md5(data), 'sha256': q.sha(data)}
            for name, data in [('DGU.bin', b'D' * 16), ('MIS.bin', b'M' * 119296),
                               ('pcie_phy_fw.bin', b'P' * 16384), ('cpapp-b.bin', b'A' * 65536)]}
        source = Fat(empty_fat())
        q._put_file(source, 'KEEP.TXT', b'unrelated preserved file', new_root=True)
        self.p1_source = self.write('source-p1.bin', source.bytes())
        candidate = Fat(source.bytes())
        q._put_file(candidate, w.BOX_NAME, w.initial_mailbox(JOB_ID), new_root=True)
        self.p1_candidate = self.write('candidate-p1.bin', candidate.bytes())
        self.p2_source = self.write('source-p2.bin', settings_fixture())
        self.p2_candidate = self.write('candidate-p2.bin', q.p2_candidate(settings_fixture()))
        original_p2, temporary_p2 = self.p2_source.read_bytes(), self.p2_candidate.read_bytes()
        owned = sorted({index//512 for index, (before, after) in enumerate(zip(original_p2, temporary_p2))
                        if before != after})
        self.exit_sectors = [
            {'sector': sector, 'bytes': 512, 'source_name': f'/norq/p2-exit-{sector}.bin',
             'original_md5': md5(original_p2[sector*512:(sector+1)*512]),
             'candidate_md5': md5(temporary_p2[sector*512:(sector+1)*512])}
            for sector in owned]
        p8 = Fat(empty_fat())
        entry = q._put_file(p8, 'NORLDR.IMG', b'O' * 417792, new_root=True)
        p8.data[entry:entry+8] = b'NOR_LDR '
        q._put_file(p8, 'KEEP.TXT', b'keep companion neighbor', new_root=True)
        self.p8_source = self.write('source-p8.bin', p8.bytes())
        tool = self.write('synthetic-tool.bin', b'synthetic pinned tool')
        manifest = self.write('source-manifest.json', b'{}\n')
        self.job = {
            'schema': w.SCHEMA, 'profile': w.PROFILE, 'job_id': JOB_ID,
            'source_partitions': {f'nflasha{n}': q.file_pin(path) for n, path in
                                  ((1, self.p1_source), (2, self.p2_source), (8, self.p8_source))},
            'source_manifests': [q.file_pin(manifest)], 'tool_inputs': [q.file_pin(tool)],
            'candidates': {'nflasha1': q.file_pin(self.p1_candidate),
                           'nflasha2': q.file_pin(self.p2_candidate)},
            'components': self.components,
            'loader': {'bytes': 16, 'md5': md5(b'L' * 16), 'sha256': q.sha(b'L' * 16)},
            'training': {'offset': 512, 'bytes': 1536},
            'operations': w.operation_manifest(self.components, {'loader_nor.bin': {'size': 200}}, 512, 1536, 16),
            'cleanup_targets': ['nflasha1', 'nflasha2'],
            'exit_sectors': self.exit_sectors,
            'completion_route': 'verified_trigger_restore_then_sony_native_reboot',
            'companion_update': {'region': 'nflasha8', 'file': 'nor_ldr.img', 'retained': True},
            'mailbox': {'name': w.BOX_NAME, 'bytes': w.BOX_SIZE, 'slots': w.SLOTS},
        }
        self.job_path = self.write('job.json', json.dumps(self.job).encode())
        self.head = {
            'schema': w.SCHEMA, 'job_id': JOB_ID, 'state': 'NATIVE_REBOOT_INTENT',
            'error': 'none', 'array_started': 'true', 'array_verified': 'true',
            'flash_type': '1',
            'companion_verified': 'true', 'inflight': 'false',
            'exit_trigger_verified': 'true',
            'header_md5': md5(b'H' * 2048), 'loader_backup_md5': md5(b'B' * 512),
            'loader_backup_bytes': '512', 'training_md5': md5(b'T' * 1536), 'training_bytes': '1536',
            'companion_md5': md5(b'C' * 417792), 'companion_bytes': '417792',
        }
        for sector in self.exit_sectors:
            self.head[f"exit_{sector['sector']}_pre_md5"] = sector['candidate_md5']
            self.head[f"exit_{sector['sector']}_post_md5"] = sector['original_md5']
        self.rows = self.native_rows()
        self.array_end = len(self.rows)
        for command in ('mount -t vfat -o rw,sync,noatime,nodiratime /dev/nflasha8 /cp',
                        'cp ./nor_ldr.img /cp/nor_ldr.img', 'sync', 'umount /cp',
                        'mount -t vfat -o ro,noatime,nodiratime /dev/nflasha8 /cp',
                        'md5sum /cp/nor_ldr.img', 'umount /cp'):
            self.add_pair(self.rows, kind='companion', component='companion', command=command,
                          input_bytes=417792, input_md5=self.head['companion_md5'], address=0, bus_mode=1,
                          erase_type=0, erase_start=0, erase_count=0)
        self.companion_end = len(self.rows)
        for sector in self.exit_sectors:
            number = sector['sector']
            for command in (f'dd if=/dev/nflasha2 of=/tmp_updater/p2-pre-{number}.bin bs=512 skip={number} count=1',
                            f'write_trigger /norq/p2-exit-{number}.bin {number}', 'sync',
                            f'dd if=/dev/nflasha2 of=/tmp_updater/p2-post-{number}.bin bs=512 skip={number} count=1'):
                self.add_pair(self.rows, kind='exit', component='exit_trigger', command=command,
                              input_bytes=512, input_md5=sector['original_md5'], address=number*512,
                              bus_mode=1, erase_type=0, erase_start=0, erase_count=0)
        self.exit_end = len(self.rows)
        for command in ('ud_pd_firmup.elf b', 'ud_reboot.elf'):
            self.add_pair(self.rows, kind='exit', component='normal_reboot', command=command,
                          input_bytes=0, input_md5='none', address=0, bus_mode=1,
                          erase_type=0, erase_start=0, erase_count=0)
        self.rows.pop()  # Native reboot may terminate without a result record.
        self.head.update(operation=str((len(self.rows)+1)//2), ledger_records=str(len(self.rows)))
        self.p1_capture = self.capture(self.head, self.rows)
        self.p2_capture = self.write('capture-p2.bin', self.p2_source.read_bytes())

    def write(self, name, data):
        path = self.root/name
        path.write_bytes(data)
        return path

    @staticmethod
    def add_pair(rows, **values):
        base = {'job_id': JOB_ID, 'operation': str(len(rows)//2 + 1), **{k: str(v) for k, v in values.items()}}
        rows.extend([{**base, 'event': 'intent', 'status': 'pending'},
                     {**base, 'event': 'result', 'status': '0'}])

    def native_rows(self):
        """Spell out the runner's CP prelude and bus changes independently."""
        rows = []
        for command in ('ud_cp_firmup.elf -l', 'ud_cp_firmup.elf -c',
                        'ud_cp_firmup.elf -u /tmp_updater/updater/bodyfs/cp/tomco.txt -a 0x100000',
                        'ud_cp_firmup.elf -u /tmp_updater/updater/bodyfs/cp/udtrcp.bin -a 0x108000',
                        'ud_cp_firmup.elf -u /tmp_updater/nor_loader/Backup.bin -a 0x4000000',
                        'ud_cp_firmup.elf -j',
                        'ud_cp_firmup.elf -t /tmp_updater/ddrtraining.dat -a 0x3e210fc -s 1536'):
            self.add_control(rows, command)
        self.add_control(rows, 'ud_cp_firmup.elf -i', kind='query', status='1')
        self.add_control(rows, 'ud_cp_firmup.elf -m 0')
        digests = {'header': self.head['header_md5'], 'loader_backup': self.head['loader_backup_md5'],
                   'loader': self.job['loader']['md5'], 'training': self.head['training_md5'],
                   **{key: self.components[name]['md5'] for key, name in
                      (('dgu', 'DGU.bin'), ('mis', 'MIS.bin'), ('phy', 'pcie_phy_fw.bin'), ('cpapp', 'cpapp-b.bin'))}}
        for op in self.job['operations']:
            common = dict(kind='array', component=op['component'], input_md5=digests[op['component']],
                          input_bytes=512 if op['bytes'] is None else op['bytes'], address=op['address'], bus_mode=op['bus_mode'])
            if op['component'] in ('mis', 'phy', 'cpapp'):
                self.add_control(rows, 'ud_cp_firmup.elf -i', kind='query', status='1')
                self.add_control(rows, 'ud_cp_firmup.elf -m 1')
                self.add_control(rows, 'ud_cp_firmup.elf -d 0')
            if op['erase']:
                e = op['erase']
                self.add_pair(rows, **common, command=f"ud_cp_firmup.elf -e {e['type']} -a {e['start']} -s {e['count']}",
                              erase_type=e['type'], erase_start=e['start'], erase_count=e['count'])
            source = op['source_name'] if op['source_name'].startswith('/') else './'+op['source_name']
            self.add_pair(rows, **common, command=f"ud_cp_firmup.elf {op['program_option']} {source} -a {op['address']}",
                          erase_type=0, erase_start=0, erase_count=0)
        return rows

    def add_control(self, rows, command, *, kind='ordinary', status='0'):
        self.add_pair(rows, kind=kind, component='none', command=command,
                      input_bytes=0, input_md5='none', address=0, bus_mode=0,
                      erase_type=0, erase_start=0, erase_count=0)
        rows[-1]['status'] = status

    @staticmethod
    def renumber(rows):
        """Keep parser sequencing valid while testing native command sequencing."""
        for index, row in enumerate(rows):
            row['operation'] = str(index//2 + 1)
        return rows

    @staticmethod
    def row_index(rows, command, *, event='intent', occurrence=0):
        return [index for index, row in enumerate(rows)
                if row['command'] == command and row['event'] == event][occurrence]

    def capture(self, head, rows, name='capture-p1.bin'):
        fs = Fat(self.p1_candidate.read_bytes())
        fs.replace(w.BOX_NAME, mailbox(head, rows))
        return self.write(name, fs.bytes())

    def collect(self, *, p1=None, p2=None, p8=None, name='collection'):
        return w.collect(self.job_path, p1 or self.p1_capture, p2 or self.p2_capture,
                         self.root/name, p8_path=p8)

    def receipt_head(self, rows, **updates):
        return {**self.head, 'operation': str((len(rows)+1)//2), 'ledger_records': str(len(rows)), **updates}


class WriterEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fx = EvidenceFixture(Path(self.tmp.name))

    def test_committed_md5_records_parse_in_order(self):
        rows = w.parse_ledger(mailbox(self.fx.head, self.fx.rows), JOB_ID)
        self.assertEqual(len(rows), 87)
        self.assertEqual(rows[0]['event'], 'intent')
        self.assertEqual(rows[-1]['operation'], '44')
        self.assertEqual(rows[-1]['command'], 'ud_reboot.elf')
        self.assertEqual(rows[-1]['status'], 'pending')

    def test_torn_or_uncommitted_record_is_refused(self):
        original = mailbox(self.fx.head, self.fx.rows)
        start, _ = w.SLOTS['ledger']
        for marker in (b'input_md5=', b'commit='):
            with self.subTest(marker=marker):
                box = bytearray(original)
                index = box.index(marker, start, start+512) + len(marker)
                box[index] = ord('b') if box[index] != ord('b') else ord('c')
                with self.assertRaises(q.QualificationError):
                    w.parse_ledger(bytes(box), JOB_ID)

    def test_holes_and_out_of_order_results_are_refused(self):
        original = mailbox(self.fx.head, self.fx.rows)
        start, _ = w.SLOTS['ledger']
        box = bytearray(original)
        box[start+512:start+1024] = bytes(512)
        with self.assertRaisesRegex(q.QualificationError, 'hole'):
            w.parse_ledger(bytes(box), JOB_ID)
        rows = copy.deepcopy(self.fx.rows)
        rows[1], rows[3] = rows[3], rows[1]
        with self.assertRaisesRegex(q.QualificationError, 'order'):
            w.parse_ledger(mailbox(self.fx.head, rows), JOB_ID)

    def test_invalid_kind_and_intent_result_status_are_refused(self):
        for index, key, value in ((0, 'kind', 'unrecognized'), (0, 'status', '0'),
                                  (1, 'status', 'pending'), (1, 'status', '-1'),
                                  (1, 'status', '256')):
            with self.subTest(index=index, key=key, value=value):
                rows = copy.deepcopy(self.fx.rows)
                rows[index][key] = value
                with self.assertRaises(q.QualificationError):
                    w.parse_ledger(mailbox(self.fx.head, rows), JOB_ID)

    def test_native_result_must_match_its_intent(self):
        rows = copy.deepcopy(self.fx.rows)
        rows[1]['input_md5'] = 'f' * 32
        with self.assertRaisesRegex(q.QualificationError, 'intent'):
            w.parse_ledger(mailbox(self.fx.head, rows), JOB_ID)

    def test_native_result_cannot_omit_intent_fields_even_with_fresh_digest(self):
        for key in ('command', 'input_md5', 'input_bytes', 'component'):
            with self.subTest(key=key):
                rows = copy.deepcopy(self.fx.rows)
                del rows[1][key]
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(self.fx.head, rows), name=f'missing-result-{key}')

    def test_full_eight_program_five_erase_prefix_and_source_lengths(self):
        rows = w.parse_ledger(mailbox(self.fx.head, self.fx.rows), JOB_ID)
        intents = [r for r in rows if r['kind'] == 'array' and r['event'] == 'intent']
        self.assertEqual(sum(' -e ' in r['command'] for r in intents), 5)
        self.assertEqual(len(intents), 13)
        self.assertEqual(sum(r['event'] == 'intent' and r['command'].startswith('ud_cp_firmup.elf ')
                             for r in rows), 31)
        self.assertTrue(w._validate_array_prefix(self.fx.job, rows, self.fx.head))
        for command, key, value in (('ud_cp_firmup.elf -e 2 -a 0 -s 9', 'input_md5', 'f' * 32),
                                    ('ud_cp_firmup.elf -e 2 -a 0 -s 9', 'input_bytes', '513'),
                                    ('ud_cp_firmup.elf -e 2 -a 0 -s 9', 'erase_count', '8'),
                                    ('ud_cp_firmup.elf -e 2 -a 0 -s 9', 'bus_mode', '1'),
                                    ('ud_cp_firmup.elf -y ./DGU.bin -a 2048', 'input_md5', 'f' * 32),
                                    ('ud_cp_firmup.elf -y ./DGU.bin -a 2048', 'input_bytes', '17'),
                                    ('ud_cp_firmup.elf -y ./loader_nor.bin -a 36864', 'input_md5', 'f' * 32)):
            with self.subTest(command=command, key=key):
                modified = copy.deepcopy(rows)
                index = self.fx.row_index(modified, command)
                modified[index][key] = value
                with self.assertRaises(q.QualificationError):
                    w._validate_array_prefix(self.fx.job, modified, self.fx.head)

    def test_missing_result_and_failed_result_never_verify_array(self):
        prefix = self.fx.rows[:self.fx.array_end-1]
        for rows in (prefix, [*prefix, {**self.fx.rows[self.fx.array_end-1], 'status': '7'}]):
            with self.subTest(records=len(rows)):
                parsed = w.parse_ledger(mailbox(self.fx.head, rows), JOB_ID)
                self.assertFalse(w._validate_array_prefix(self.fx.job, parsed, self.fx.head))
                head = {**self.fx.head, 'state': 'ARRAY_WRITE_OUTCOME_UNKNOWN',
                        'array_verified': 'false', 'companion_verified': 'false',
                        'exit_trigger_verified': 'false',
                        'ledger_records': str(len(rows)), 'inflight': 'true'}
                path = self.fx.capture(head, rows)
                result = self.fx.collect(p1=path, p2=self.fx.p2_candidate, name=f'partial-{len(rows)}')
                self.assertFalse(result['array_write_verified'])

    def test_forged_verified_header_without_complete_receipts_is_refused(self):
        for rows in ([], self.fx.rows[:self.fx.array_end-1]):
            with self.subTest(records=len(rows)):
                path = self.fx.capture(self.fx.head, rows)
                with self.assertRaisesRegex(q.QualificationError, 'receipts'):
                    self.fx.collect(p1=path, name=f'forged-{len(rows)}')

    def test_collect_complete_native_evidence_and_retained_companion(self):
        p8 = Fat(self.fx.p8_source.read_bytes())
        p8.replace('nor_ldr.img', b'C' * 417792)
        readback = self.fx.write('p8-readback.bin', p8.bytes())
        result = self.fx.collect(p8=readback)
        self.assertTrue(result['array_write_verified'])
        self.assertTrue(result['companion_verified'])
        self.assertTrue(result['host_p8_companion_and_other_files_verified'])
        self.assertFalse(result['cleanup_completed'])
        self.assertFalse(result['can_execute'])
        self.assertEqual(Path(result['evidence']['companion']['path']).read_bytes(), b'C' * 417792)
        self.assertEqual(Fat(self.fx.p8_source.read_bytes()).read('NOR_LDR.IMG'), b'O' * 417792)
        self.assertEqual(Fat(readback.read_bytes()).read('NOR_LDR.IMG'), b'C' * 417792)

    def test_pending_native_reboot_proves_exit_without_proving_normal_usb(self):
        result = self.fx.collect()
        self.assertTrue(result['array_write_verified'])
        self.assertTrue(result['companion_verified'])
        self.assertTrue(result['exit_trigger_verified'])
        self.assertTrue(result['native_reboot_requested'])
        self.assertFalse(result['normal_usb_observed'])
        self.assertFalse(result['cleanup_completed'])
        self.assertEqual(result['stage']['state'], 'NATIVE_REBOOT_INTENT')
        self.assertEqual(result['ledger'][-1]['command'], 'ud_reboot.elf')
        self.assertEqual(result['ledger'][-1]['event'], 'intent')
        self.assertEqual(result['ledger'][-1]['status'], 'pending')
        self.assertEqual(Fat(self.fx.p2_capture.read_bytes()).entries(3).keys(), {'DAT4'})

    def test_exit_sector_preimage_and_readback_commands_are_mandatory(self):
        for offset in (0, 6):
            for damage in ('missing', 'wrong-sector', 'wrong-file'):
                with self.subTest(offset=offset, damage=damage):
                    rows = copy.deepcopy(self.fx.rows)
                    index = self.fx.companion_end + offset
                    if damage == 'missing':
                        del rows[index:index+2]
                        self.fx.renumber(rows)
                    else:
                        command = rows[index]['command']
                        if damage == 'wrong-sector':
                            number = self.fx.exit_sectors[0]['sector']
                            command = command.replace(f'skip={number}', f'skip={number+1}')
                        else:
                            command = command.replace('/tmp_updater/p2-', '/tmp_updater/forged-p2-')
                        rows[index]['command'] = rows[index+1]['command'] = command
                    head = self.fx.receipt_head(rows)
                    with self.assertRaises(q.QualificationError):
                        self.fx.collect(p1=self.fx.capture(head, rows), name=f'exit-{offset}-{damage}')

    def test_exit_write_is_confined_to_owned_sector_and_original_digest(self):
        for damage in ('sector', 'digest', 'bytes', 'address', 'erase', 'bus'):
            with self.subTest(damage=damage):
                rows = copy.deepcopy(self.fx.rows)
                index = self.fx.companion_end + 2
                for row in rows[index:index+2]:
                    number = self.fx.exit_sectors[0]['sector']
                    if damage == 'sector':
                        row['command'] = f'write_trigger /norq/p2-exit-{number+1}.bin {number+1}'
                        row['address'] = str((number+1)*512)
                    elif damage == 'digest':
                        row['input_md5'] = 'f' * 32
                    elif damage == 'bytes':
                        row['input_bytes'] = '1024'
                    elif damage == 'address':
                        row['address'] = '0'
                    elif damage == 'erase':
                        row['erase_count'] = '1'
                    else:
                        row['bus_mode'] = '0'
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(self.fx.head, rows), name=f'exit-write-{damage}')

    def test_exit_sector_observed_pre_and_post_digests_must_match(self):
        for sector in self.fx.exit_sectors:
            for phase in ('pre', 'post'):
                with self.subTest(sector=sector['sector'], phase=phase):
                    key = f"exit_{sector['sector']}_{phase}_md5"
                    head = {**self.fx.head, key: 'f' * 32}
                    with self.assertRaises(q.QualificationError):
                        self.fx.collect(p1=self.fx.capture(head, self.fx.rows), name=f'exit-digest-{phase}')

    def test_exit_manifest_digest_and_footprint_must_match_pinned_partitions(self):
        for damage in ('original', 'candidate', 'non-owned'):
            with self.subTest(damage=damage):
                job = copy.deepcopy(self.fx.job)
                sector = job['exit_sectors'][0]
                if damage == 'original':
                    sector['original_md5'] = 'f' * 32
                elif damage == 'candidate':
                    sector['candidate_md5'] = 'f' * 32
                else:
                    number = sector['sector'] + 1
                    sector.update(sector=number, source_name=f'/norq/p2-exit-{number}.bin')
                    sector['original_md5'] = md5(self.fx.p2_source.read_bytes()[number*512:(number+1)*512])
                    sector['candidate_md5'] = md5(self.fx.p2_candidate.read_bytes()[number*512:(number+1)*512])
                self.fx.job_path.write_text(json.dumps(job))
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(name=f'forged-exit-manifest-{damage}')
        self.fx.job_path.write_text(json.dumps(self.fx.job))

    def test_native_reboot_requires_every_trigger_restore_and_power_result(self):
        indexes = [index for index in range(self.fx.companion_end, len(self.fx.rows))
                   if self.fx.rows[index]['event'] == 'result']
        self.assertEqual(len(indexes), 5)
        for index in indexes:
            with self.subTest(result=index):
                rows = copy.deepcopy(self.fx.rows)
                rows[index]['status'] = '255'
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(self.fx.head, rows), name=f'failed-exit-{index}')

    def test_companion_verified_header_requires_complete_persistence_receipts(self):
        for completed_pairs in range(7):
            count = self.fx.array_end + completed_pairs*2
            with self.subTest(records=count):
                head = {**self.fx.head, 'ledger_records': str(count), 'operation': str(count//2)}
                head.update(state='COMPANION_VERIFIED', exit_trigger_verified='false')
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(head, self.fx.rows[:count]), p2=self.fx.p2_candidate,
                                    name=f'forged-companion-{count}')
        rows = copy.deepcopy(self.fx.rows[:self.fx.companion_end])
        rows[-1]['status'] = '1'
        head = self.fx.receipt_head(rows, state='COMPANION_VERIFIED', exit_trigger_verified='false')
        with self.assertRaises(q.QualificationError):
            self.fx.collect(p1=self.fx.capture(head, rows), p2=self.fx.p2_candidate, name='failed-companion-result')

    def test_terminal_flags_cannot_hide_missing_torn_or_pending_result(self):
        rows = self.fx.rows[:-1]
        head = self.fx.receipt_head(rows)
        with self.assertRaises(q.QualificationError):
            self.fx.collect(p1=self.fx.capture(head, rows), name='missing-terminal-result')
        fs = Fat(self.fx.p1_capture.read_bytes())
        box = bytearray(fs.read(w.BOX_NAME))
        offset = w.SLOTS['ledger'][0] + (len(self.fx.rows)-1)*512
        box[offset+10] ^= 1
        fs.replace(w.BOX_NAME, bytes(box))
        with self.assertRaises(q.QualificationError):
            self.fx.collect(p1=self.fx.write('torn-terminal.bin', fs.bytes()), name='torn-terminal-result')
        for key, value in (('inflight', 'true'), ('ledger_records', '0'), ('operation','0'),
                           ('error','native_command_failed'), ('flash_type','255')):
            with self.subTest(key=key):
                head = {**self.fx.head, key: value}
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(head, self.fx.rows), name=f'forged-terminal-{key}')

    def test_native_query_success_statuses_are_distinct_from_native_failures(self):
        for command, accepted, rejected in (('ud_read_lsi_type.elf', '3', '0'),
                                             ('ud_get_boot_device.elf', '1', '0'),
                                             ('ud_cp_firmup.elf -i', '1', '2')):
            with self.subTest(command=command):
                rows = []
                self.fx.add_pair(rows, kind='query', component='none', command=command,
                                 input_bytes=0, input_md5='none', address=0, bus_mode=1,
                                 erase_type=0, erase_start=0, erase_count=0)
                rows[-1]['status'] = accepted
                parsed = w.parse_ledger(mailbox(self.fx.head, rows), JOB_ID)
                self.assertTrue(w._all_native_results_confirmed(parsed))
                rows[-1]['status'] = rejected
                parsed = w.parse_ledger(mailbox(self.fx.head, rows), JOB_ID)
                self.assertFalse(w._all_native_results_confirmed(parsed))

    def test_no_command_is_allowed_after_native_reboot(self):
        for command, kind, status in (('ud_read_lsi_type.elf', 'query', '3'),
                                       ('ud_get_boot_device.elf', 'query', '1'),
                                       ('ud_cp_firmup.elf -i', 'query', '1'),
                                       ('sync', 'ordinary', '0')):
            with self.subTest(command=command):
                rows = copy.deepcopy(self.fx.rows)
                rows.append({**rows[-1], 'event': 'result', 'status': '0'})
                self.fx.add_control(rows, command, kind=kind, status=status)
                head = self.fx.receipt_head(rows)
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(head, rows), name=f'postterminal-{len(command)}')

    def test_disguised_destructive_operation_is_rejected_before_and_after_companion(self):
        for kind in ('ordinary', 'query'):
            for position in ('before', 'after'):
                with self.subTest(kind=kind, position=position):
                    rows = copy.deepcopy(self.fx.rows)
                    extra = []
                    self.fx.add_control(extra, 'ud_cp_firmup.elf -e 3 -a 0 -s 1024', kind=kind)
                    index = self.fx.array_end if position == 'before' else self.fx.companion_end
                    rows[index:index] = extra
                    self.fx.renumber(rows)
                    head = self.fx.receipt_head(rows)
                    with self.assertRaises(q.QualificationError):
                        self.fx.collect(p1=self.fx.capture(head, rows), name=f'disguised-{kind}-{position}')

    def test_cp_transcript_includes_exact_operation_kind(self):
        for command, kind in (('ud_cp_firmup.elf -l', 'query'),
                                ('ud_cp_firmup.elf -e 2 -a 0 -s 9', 'ordinary')):
            with self.subTest(command=command):
                rows = copy.deepcopy(self.fx.rows)
                index = self.fx.row_index(rows, command)
                rows[index]['kind'] = rows[index+1]['kind'] = kind
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(self.fx.head, rows), name=f'wrong-kind-{kind}')

    def test_array_bus_metadata_cannot_replace_missing_native_mode_commands(self):
        for command, occurrence in (('ud_cp_firmup.elf -i', 1),
                                     ('ud_cp_firmup.elf -m 1', 0),
                                     ('ud_cp_firmup.elf -d 0', 0)):
            with self.subTest(command=command):
                rows = copy.deepcopy(self.fx.rows)
                index = self.fx.row_index(rows, command, occurrence=occurrence)
                del rows[index:index+2]
                self.fx.renumber(rows)
                self.assertTrue(all(row['bus_mode'] == '1' for row in rows
                                    if row['kind'] == 'array' and row['component'] in ('mis', 'training', 'phy', 'cpapp')))
                head = self.fx.receipt_head(rows)
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(head, rows), name=f'missing-mode-{command[-3:]}')

    def test_native_mode_command_order_and_arguments_are_exact(self):
        for damage in ('reverse', 'wrong-bus', 'wrong-disable', 'wrong-query-kind'):
            with self.subTest(damage=damage):
                rows = copy.deepcopy(self.fx.rows)
                index = self.fx.row_index(rows, 'ud_cp_firmup.elf -i', occurrence=1)
                if damage == 'reverse':
                    rows[index+2:index+6] = rows[index+4:index+6] + rows[index+2:index+4]
                    self.fx.renumber(rows)
                elif damage == 'wrong-bus':
                    rows[index+2]['command'] = rows[index+3]['command'] = 'ud_cp_firmup.elf -m 0'
                elif damage == 'wrong-disable':
                    rows[index+4]['command'] = rows[index+5]['command'] = 'ud_cp_firmup.elf -d 1'
                else:
                    rows[index]['kind'] = rows[index+1]['kind'] = 'ordinary'
                    rows[index+1]['status'] = '0'
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(self.fx.head, rows), name=f'native-mode-{damage}')

    def test_every_flash_query_matches_header_flash_type(self):
        indexes = [index for index, row in enumerate(self.fx.rows)
                   if row['event'] == 'result' and row['command'] == 'ud_cp_firmup.elf -i']
        self.assertEqual(len(indexes), 4)
        for index in indexes:
            with self.subTest(query=index):
                rows = copy.deepcopy(self.fx.rows)
                rows[index]['status'] = '0'
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(self.fx.head, rows), name=f'flash-mismatch-{index}')

    def test_all_cp_bootstrap_and_mode_results_must_be_confirmed(self):
        for command in ('ud_cp_firmup.elf -l',
                        'ud_cp_firmup.elf -u /tmp_updater/nor_loader/Backup.bin -a 0x4000000',
                        'ud_cp_firmup.elf -t /tmp_updater/ddrtraining.dat -a 0x3e210fc -s 1536',
                        'ud_cp_firmup.elf -m 0', 'ud_cp_firmup.elf -d 0'):
            with self.subTest(command=command):
                rows = copy.deepcopy(self.fx.rows)
                index = self.fx.row_index(rows, command, event='result')
                rows[index]['status'] = '255'
                parsed = w.parse_ledger(mailbox(self.fx.head, rows), JOB_ID)
                self.assertFalse(w._validate_array_prefix(self.fx.job, parsed, self.fx.head))
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p1=self.fx.capture(self.fx.head, rows), name=f'failed-cp-result-{index}')

    def test_companion_failure_preserves_separate_verified_array_result(self):
        rows = copy.deepcopy(self.fx.rows[:self.fx.array_end+4])
        rows[-1]['status'] = '1'
        head = {**self.fx.head, 'state': 'COMPANION_FAILED', 'companion_verified': 'false',
                'exit_trigger_verified': 'false',
                'ledger_records': str(len(rows)), 'operation': str(len(rows)//2)}
        result = self.fx.collect(p1=self.fx.capture(head, rows), p2=self.fx.p2_candidate)
        self.assertTrue(result['array_write_verified'])
        self.assertFalse(result['companion_verified'])
        self.assertFalse(result['host_p8_companion_and_other_files_verified'])

    def test_companion_readback_rejects_unrelated_file_and_physical_tail_changes(self):
        for damage in ('neighbor', 'tail', 'companion'):
            with self.subTest(damage=damage):
                p8 = Fat(self.fx.p8_source.read_bytes())
                p8.replace('nor_ldr.img', b'C' * 417792)
                if damage == 'neighbor':
                    p8.replace('KEEP.TXT', b'altered companion neighbor')
                if damage == 'companion':
                    p8.replace('nor_ldr.img', b'X' * 417792)
                data = bytearray(p8.bytes())
                if damage == 'tail':
                    data[-1] ^= 1
                readback = self.fx.write(f'p8-{damage}.bin', bytes(data))
                with self.assertRaises(q.QualificationError):
                    self.fx.collect(p8=readback, name=f'bad-{damage}')

    def test_generated_evidence_digest_must_match_embedded_bytes(self):
        fs = Fat(self.fx.p1_capture.read_bytes())
        box = bytearray(fs.read(w.BOX_NAME))
        box[w.SLOTS['companion'][0]] ^= 1
        fs.replace(w.BOX_NAME, bytes(box))
        with self.assertRaisesRegex(q.QualificationError, 'checksum'):
            self.fx.collect(p1=self.fx.write('corrupt-evidence.bin', fs.bytes()))

    def test_mailbox_timestamp_and_vfat_dirty_byte_are_the_only_metadata_exceptions(self):
        data = bytearray(self.fx.p1_capture.read_bytes())
        entry = Fat(data).locate(w.BOX_NAME)[0]
        data[entry+13:entry+20] = b'\x01' * 7
        data[entry+22:entry+26] = b'\x01' * 4
        data[37] = 1
        path = self.fx.write('allowed-metadata.bin', bytes(data))
        head, box = w.validate_temporary(self.fx.job, path, self.fx.p2_capture)
        self.assertEqual(head['state'], 'NATIVE_REBOOT_INTENT')
        self.assertEqual(len(box), w.BOX_SIZE)
        for offset in (entry+11, 38, len(data)-1):
            with self.subTest(offset=offset):
                changed = bytearray(data)
                changed[offset] ^= 1
                path = self.fx.write('foreign-metadata.bin', bytes(changed))
                with self.assertRaises(q.QualificationError):
                    w.validate_temporary(self.fx.job, path, self.fx.p2_capture)

    def test_nonmailbox_p1_file_and_p2_size_changes_are_refused(self):
        fs = Fat(self.fx.p1_capture.read_bytes())
        fs.replace('KEEP.TXT', b'changed preserved file')
        with self.assertRaises(q.QualificationError):
            w.validate_temporary(self.fx.job, self.fx.write('bad-p1.bin', fs.bytes()), self.fx.p2_capture)
        p2 = self.fx.p2_capture.read_bytes()[:-1]
        with self.assertRaises(q.QualificationError):
            w.validate_temporary(self.fx.job, self.fx.p1_capture, self.fx.write('bad-p2.bin', p2))

    def test_cleanup_plan_restores_only_p1_and_preserves_current_p2(self):
        plan = w.cleanup_plan(self.fx.job_path, self.fx.p1_capture, self.fx.p2_capture)
        self.assertEqual(set(plan['restore']), {'nflasha1'})
        self.assertEqual(plan['preserve_p2'], q.file_pin(self.fx.p2_capture))
        self.assertFalse(plan['nfa8_restore'])
        self.assertFalse(plan['cleanup_completed'])
        plan_path = self.fx.write('cleanup-plan.json', json.dumps(plan).encode())
        p1 = self.fx.write('p1-clean-readback.bin', self.fx.p1_source.read_bytes())
        p2 = self.fx.write('p2-clean-readback.bin', self.fx.p2_source.read_bytes())
        proof = w.verify_cleanup(self.fx.job_path, plan_path, p1, p2)
        self.assertTrue(proof['cleanup_completed'])
        self.assertFalse(proof['nfa8_restored'])
        self.assertTrue(proof['array_write_completion_is_separate'])
        self.assertEqual(set(proof['readbacks']), {'nflasha1', 'nflasha2'})
        self.assertEqual(Fat(self.fx.p8_source.read_bytes()).read('NOR_LDR.IMG'), b'O' * 417792)

    def test_failure_before_trigger_exit_still_plans_explicit_p1_p2_restore(self):
        rows = self.fx.rows[:self.fx.array_end-1]
        head = self.fx.receipt_head(rows, state='ARRAY_WRITE_OUTCOME_UNKNOWN', array_verified='false',
                                    companion_verified='false', exit_trigger_verified='false', inflight='true')
        p1 = self.fx.capture(head, rows, name='failed-before-exit-p1.bin')
        plan = w.cleanup_plan(self.fx.job_path, p1, self.fx.p2_candidate)
        self.assertEqual(set(plan['restore']), {'nflasha1', 'nflasha2'})
        self.assertFalse(plan['cleanup_completed'])
        self.assertFalse(plan['can_execute'])

    def test_postnormal_backup_changes_are_preserved_during_p1_cleanup(self):
        p2 = Fat(self.fx.p2_capture.read_bytes())
        p2.replace('Backup.bin', b'N' * 128)
        current = self.fx.write('postnormal-p2.bin', p2.bytes())
        result = self.fx.collect(p2=current)
        self.assertTrue(result['exit_trigger_verified'])
        plan = w.cleanup_plan(self.fx.job_path, self.fx.p1_capture, current)
        self.assertEqual(set(plan['restore']), {'nflasha1'})
        plan_path = self.fx.write('cleanup-plan.json', json.dumps(plan).encode())
        p1 = self.fx.write('p1-clean-readback.bin', self.fx.p1_source.read_bytes())
        independent = self.fx.write('p2-preserved-readback.bin', current.read_bytes())
        proof = w.verify_cleanup(self.fx.job_path, plan_path, p1, independent)
        self.assertTrue(proof['cleanup_completed'])
        self.assertEqual(proof['readbacks']['nflasha2']['sha256'], q.sha(current.read_bytes()))
        self.assertEqual(Fat(independent.read_bytes()).read('Backup.bin'), b'N' * 128)

    def test_p2_source_and_collected_image_are_not_independent_preservation_readbacks(self):
        plan = w.cleanup_plan(self.fx.job_path, self.fx.p1_capture, self.fx.p2_capture)
        plan_path = self.fx.write('cleanup-plan.json', json.dumps(plan).encode())
        p1 = self.fx.write('p1-clean-readback.bin', self.fx.p1_source.read_bytes())
        link = self.fx.root/'captured-p2-hardlink.bin'
        link.hardlink_to(self.fx.p2_capture)
        for p2 in (self.fx.p2_source, self.fx.p2_capture, link):
            with self.subTest(path=p2.name):
                with self.assertRaisesRegex(q.QualificationError, 'independent'):
                    w.verify_cleanup(self.fx.job_path, plan_path, p1, p2)

    def test_remaining_owned_mode_flags_refuse_cleanup(self):
        for keep in ('MODE', 'MODE1'):
            with self.subTest(flag=keep):
                fs = Fat(self.fx.p2_candidate.read_bytes())
                other = 'MODE1' if keep == 'MODE' else 'MODE'
                entry = fs.locate('updater/'+other)[0]
                fs.data[entry] = 0xe5
                path = self.fx.write(f'p2-retained-{keep}.bin', fs.bytes())
                with self.assertRaises(q.QualificationError):
                    w.cleanup_plan(self.fx.job_path, self.fx.p1_capture, path)

    def test_cleanup_source_and_hardlink_are_not_independent_readbacks(self):
        plan = w.cleanup_plan(self.fx.job_path, self.fx.p1_capture, self.fx.p2_capture)
        plan_path = self.fx.write('cleanup-plan.json', json.dumps(plan).encode())
        p2 = self.fx.write('p2-clean-readback.bin', self.fx.p2_source.read_bytes())
        link = self.fx.root/'source-p1-hardlink.bin'
        link.hardlink_to(self.fx.p1_source)
        for p1 in (self.fx.p1_source, link):
            with self.subTest(path=p1.name):
                with self.assertRaisesRegex(q.QualificationError, 'independent'):
                    w.verify_cleanup(self.fx.job_path, plan_path, p1, p2)

    def test_cleanup_modified_plan_and_incorrect_readback_are_refused(self):
        plan = w.cleanup_plan(self.fx.job_path, self.fx.p1_capture, self.fx.p2_capture)
        plan['nfa8_restore'] = True
        path = self.fx.write('altered-plan.json', json.dumps(plan).encode())
        p1 = self.fx.write('p1-clean-readback.bin', self.fx.p1_source.read_bytes())
        p2 = self.fx.write('p2-clean-readback.bin', self.fx.p2_source.read_bytes())
        with self.assertRaisesRegex(q.QualificationError, 'plan differs'):
            w.verify_cleanup(self.fx.job_path, path, p1, p2)
        plan['nfa8_restore'] = False
        path = self.fx.write('cleanup-plan.json', json.dumps(plan).encode())
        p1.write_bytes(self.fx.p1_candidate.read_bytes())
        with self.assertRaisesRegex(q.QualificationError, 'readback differs'):
            w.verify_cleanup(self.fx.job_path, path, p1, p2)


if __name__ == '__main__':
    unittest.main()
