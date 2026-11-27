"""Offline, separately scoped Sony NOR rebuild artifact; no device executor.

A successful build is not permission to program a camera. Actual native results
and the success-only ordinary Sony USB return govern collection; time never resets the device.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import tarfile
from uuid import uuid4

from . import nor_qualification as q, nor_resources
from .stock_fat import Fat
from .stock_sd import all_files

SCHEMA = 'sony-nor-rebuild-v1'
BOX_NAME = 'NORW.DAT'
BOX_SIZE = 1048576
SLOTS = {'ledger': (4096, 131072), 'companion': (135168, 524288),
         'header': (659456, 4096), 'loader_backup': (663552, 4096),
         'training': (667648, 4096), 'native_log': (671744, 262144)}
TERMINAL_STATES = ('FAILED_BEFORE_ARRAY_WRITE', 'ARRAY_WRITE_OUTCOME_UNKNOWN',
                   'ARRAY_WRITE_FAILED', 'ARRAY_WRITE_VERIFIED',
                   'COMPANION_VERIFIED', 'COMPANION_FAILED', 'EXIT_FAILED', 'NATIVE_REBOOT_INTENT')
PROFILE = nor_resources.PROFILE


def parse_layout(data):
    """Admit only the matched Sony layout, never arbitrary shell input."""
    text = data.decode('ascii')
    result = {}
    for block in re.findall(r'file_param\s*\{([^}]+)\}', text):
        fields = dict(re.findall(r'(\w+)\s*=\s*("[^"]+"|0x[0-9a-fA-F]+|[0-9]+)', block))
        name = fields.pop('path').strip('"')
        q.need(name not in result, 'duplicate layout component')
        result[name] = {k: int(v, 0) for k, v in fields.items()}
    expected = {'DGU.bin': (4, 64, True, False), 'LdrBkup.bin': (68, 4, True, False),
                'loader_nor.bin': (72, 200, True, False), 'MIS.bin': (512, 256, False, True),
                'pcie_phy_fw.bin': (768, 0, False, True), 'cpapp-b.bin': (4096, 0, False, True)}
    q.need(set(result) == set(expected), 'unexpected NOR layout components')
    for name, (start, size, encrypted, ddr) in expected.items():
        row = result[name]
        q.need(row['start'] == start and row['size'] == size and
               bool(row.get('enc')) == encrypted and bool(row.get('ddr')) == ddr and
               row.get('update') == 1, 'unsupported NOR layout: ' + name)
    return result


def erase_extent(address, size, unit):
    q.need(unit in (4096, 65536) and address >= 0 and address % unit == 0 and size > 0,
           'invalid native erase geometry')
    count = size // unit + 1  # Preserve Sony's deliberate extra unit.
    q.need(address + count * unit <= 67108864, 'native erase exceeds NOR')
    return {'type': 2 if unit == 4096 else 3, 'start': address // unit, 'count': count,
            'byte_start': address, 'byte_end': address + count * unit}


def operation_manifest(components, layout, training_offset, training_length, loader_length):
    q.need(training_offset == 512 and training_length == 1536, 'unsupported MIS training fields')
    q.need(loader_length > 0 and loader_length <= layout['loader_nor.bin']['size'] * 512,
           'loader exceeds matched NOR allocation')
    expected_sizes = {'DGU.bin': 32768, 'MIS.bin': 131072, 'pcie_phy_fw.bin': 16384,
                      'cpapp-b.bin': 24 * 1024 * 1024}
    for name, limit in expected_sizes.items():
        q.need(0 < components[name]['bytes'] <= limit, 'component exceeds matched allocation: ' + name)
    rows = [
        ('header', 'infomisc.dat', 0, 2048, '-f', 0, erase_extent(0, 71*512, 4096)),
        ('dgu', 'DGU.bin', 0x800, components['DGU.bin']['bytes'], '-y', 0, None),
        ('loader_backup', 'LdrBkup.bin', 0x8800, None, '-y', 0, None),
        ('loader', 'loader_nor.bin', 0x9000, loader_length, '-y', 0, erase_extent(0x9000, 200*512, 4096)),
        ('mis', 'MIS.bin', 0x40000, components['MIS.bin']['bytes'], '-f', 1, erase_extent(0x40000, 256*512, 4096)),
        ('training', '/tmp_updater/ddrtraining.dat', 0x40000+training_offset, training_length, '-f', 1, None),
        ('phy', 'pcie_phy_fw.bin', 0x60000, components['pcie_phy_fw.bin']['bytes'], '-f', 1,
         erase_extent(0x60000, components['pcie_phy_fw.bin']['bytes'], 4096)),
        ('cpapp', 'cpapp-b.bin', 0x200000, components['cpapp-b.bin']['bytes'], '-f', 1,
         erase_extent(0x200000, components['cpapp-b.bin']['bytes'], 65536)),
    ]
    return [{'id': i+1, 'component': name, 'source_name': source, 'address': address,
             'bytes': size, 'program_option': option, 'bus_mode': mode, 'erase': erase,
             'native_program_includes_verify': True}
            for i, (name, source, address, size, option, mode, erase) in enumerate(rows)]


def initial_mailbox(job_id):
    return (f'schema={SCHEMA}\njob_id={job_id}\nstate=PREPARED\nerror=none\n'
            'array_started=false\narray_verified=false\ncompanion_verified=false\n'
            'operation=0\nledger_records=0\ninflight=false\n').encode().ljust(BOX_SIZE, b'\0')


def patch_init(data):
    anchor = b'#execute main procedure\nUdtrMain.sh &'
    q.need(data.count(anchor) == 1, 'unknown writer init dispatch')
    return data.replace(anchor, b'# Separate standalone Sony NOR rebuild; never fall through.\n'
                        b'exec /norq/runner.sh\nwhile true; do sleep 60; done\n' + anchor)


def build(sources, manifests, sony_body, loader_tar, outdir, *, source_kind,
          mksquashfs='mksquashfs'):
    """Build only local files. No transport module is imported or called."""
    outdir = Path(outdir).resolve()
    q.need(not outdir.exists(), 'writer output already exists')
    p1, p2, p8 = (Path(sources[f'nflasha{s}']['path']).read_bytes() for s in (1, 2, 8))
    f1, f2, f8 = Fat(p1), Fat(p2), Fat(p8)
    updater = nor_resources.select_updater(f1)
    cp_files = nor_resources.cp_components(f8)
    components = {name: {'bytes': len(data), 'sha256': q.sha(data),
                         'md5': hashlib.md5(data).hexdigest()}
                  for name, data in cp_files.items()}
    training_offset, training_length = struct.unpack_from('<II', cp_files['MIS.bin'], 320)
    loader = Path(loader_tar).read_bytes()
    q.need(q.sha(loader) == q.LOADER_SHA, 'writer loader TAR differs from matched DAT')
    with tarfile.open(loader_tar) as tf:
        members = tf.getmembers()
        q.need(set(m.name for m in members) == {'loader_nor.bin', 'LdrBkupList.txt',
               'pformat_config.txt', 'loader_nor.md5'} and all(m.isfile() for m in members),
               'unsafe or unexpected loader archive')
        layout = parse_layout(tf.extractfile('pformat_config.txt').read())
        loader_length = tf.getmember('loader_nor.bin').size
        loader_data = tf.extractfile('loader_nor.bin').read()
        archive_files = {m.name: tf.extractfile(m).read() for m in members}
    operations = operation_manifest(components, layout, training_offset, training_length, loader_length)
    job_id = uuid4().hex
    additions = {}
    tool_pins = []
    for name, path, expected in nor_resources.tool_inputs(Path(sony_body), updater):
        data = path.read_bytes()
        q.need(q.sha(data) == expected, 'unmatched Sony writer tool: ' + name)
        additions['norq/'+name] = data; tool_pins.append(q.file_pin(path))
    additions['norq/loader_nor.tar'] = loader; tool_pins.append(q.file_pin(loader_tar))
    additions['norq/runner.sh'] = Path(__file__).with_name('nor_rebuild_runner.sh.in').read_bytes()
    cpapp_count = operations[-1]['erase']['count']
    # No paths or shell syntax supplied by callers are emitted as shell code.
    additions['norq/job.conf'] = (f'JOB_ID={job_id}\nCPAPP_BYTES={components["cpapp-b.bin"]["bytes"]}\n'
        f'CPAPP_BLOCK_COUNT={cpapp_count}\nLOADER_BYTES={loader_length}\n'
        f'DGU_BYTES={components["DGU.bin"]["bytes"]}\nMIS_BYTES={components["MIS.bin"]["bytes"]}\n'
        f'PHY_BYTES={components["pcie_phy_fw.bin"]["bytes"]}\nTRAIN_OFFSET={training_offset}\n'
        f'TRAIN_BYTES={training_length}\nCOMPANION_BYTES=417792\n').encode()
    md5rows = [(data, '/cp/'+name) for name, data in cp_files.items()]
    md5rows.append((f2.read('Backup.bin'), '/setting/Backup.bin'))
    additions['norq/source-files.md5'] = ''.join(hashlib.md5(data).hexdigest()+'  '+name+'\n'
                                               for data, name in md5rows).encode()
    staged = [(data, name) for name, data in cp_files.items() if name != 'nor_ldr.img']
    staged += [(f2.read('Backup.bin'),'Backup.bin'), *[(data,name) for name,data in archive_files.items()]]
    additions['norq/staged-files.md5'] = ''.join(hashlib.md5(data).hexdigest()+'  /tmp_updater/nor_loader/'+name+'\n'
                                               for data,name in staged).encode()
    additions['norq/job.conf'] += ''.join(f'{key}_MD5={digest}\n' for key,digest in (
        ('DGU',components['DGU.bin']['md5']),('MIS',components['MIS.bin']['md5']),
        ('PHY',components['pcie_phy_fw.bin']['md5']),('CPAPP',components['cpapp-b.bin']['md5']),
        ('LOADER',hashlib.md5(loader_data).hexdigest()))).encode()
    trigger = q.p2_candidate(p2)
    changed = sorted({i//512 for i,(a,b) in enumerate(zip(p2,trigger)) if a!=b})
    q.need(0 < len(changed) <= 2, 'unexpected trigger exit footprint')
    exit_sectors=[]; exit_commands=[]
    for sector in changed:
        original=p2[sector*512:(sector+1)*512]; temporary=trigger[sector*512:(sector+1)*512]
        source='/norq/p2-exit-'+str(sector)+'.bin'
        additions[source.lstrip('/')]=original
        row={'sector':sector,'bytes':512,'source_name':source,
             'original_md5':hashlib.md5(original).hexdigest(),
             'candidate_md5':hashlib.md5(temporary).hexdigest()}
        exit_sectors.append(row)
        exit_commands.append(f"restore_trigger {sector} {source} {row['candidate_md5']} {row['original_md5']}")
    additions['norq/exit-trigger.sh']=('\n'.join(exit_commands)+'\n').encode()
    initrd = q.rebuild_initrd(f1.read('boot/initrd.img'), additions, mksquashfs, init_transform=patch_init)
    original = all_files(f1); checksum = f1.read('p1chksum')
    old_md5 = hashlib.md5(original['BOOT/INITRD.IMG']).hexdigest().encode()
    q.need(checksum.count(old_md5) == 1, 'writer source p1 checksum mismatch')
    q._put_file(f1, 'boot/initrd.img', initrd)
    f1.replace('p1chksum', checksum.replace(old_md5, hashlib.md5(initrd).hexdigest().encode()))
    q._put_file(f1, BOX_NAME, initial_mailbox(job_id), new_root=True)
    candidate = f1.bytes(); after = all_files(Fat(candidate))
    q.need(set(after)-set(original) == {BOX_NAME} and all(after[n] == v for n, v in original.items()
           if n not in ('BOOT/INITRD.IMG', 'P1CHKSUM')), 'writer changed unrelated p1 file')
    q.need(candidate[f1.total:] == p1[f1.total:], 'writer changed physical p1 tail')
    for pin in [*sources.values(), *manifests, *tool_pins]: q.check_pin(pin)
    outdir.mkdir(mode=0o700)
    for name, data in [('p1.candidate.bin', candidate), ('p2.candidate.bin', trigger)]:
        path = outdir/name; path.write_bytes(data); path.chmod(0o600)
    job = {'schema': SCHEMA, 'job_id': job_id, 'profile': PROFILE, 'status': 'OFFLINE_BUILT',
           'camera_io': False, 'can_execute': False, 'source_kind': source_kind,
           'source_partitions': sources, 'source_manifests': manifests, 'tool_inputs': tool_pins,
           'updater_profile': updater,
           'candidates': {f'nflasha{s}': q.file_pin(outdir/f'p{s}.candidate.bin') for s in (1,2)},
           'runner_sha256': q.sha(additions['norq/runner.sh']), 'initrd_sha256': q.sha(initrd),
           'components': components, 'training': {'offset': training_offset, 'bytes': training_length},
           'loader': {'bytes': loader_length, 'md5': hashlib.md5(loader_data).hexdigest(),
                      'sha256': q.sha(loader_data)},
           'operations': operations, 'mailbox': {'name': BOX_NAME, 'bytes': BOX_SIZE, 'slots': SLOTS},
           'native_program_verification': 'Sony plain/encrypted program commands include readback/compare',
           'companion_update': {'region': 'nflasha8', 'file': 'nor_ldr.img', 'retained': True},
           'cleanup_targets': ['nflasha1','nflasha2'], 'extra_rollback_copies': False,
           'exit_sectors':exit_sectors,'completion_route':'verified_trigger_restore_then_sony_native_reboot',
           'terminal_notification': {'kind':'ordinary_sony_usb_after_success_only_native_reboot',
               'timer_reset_permitted':False,'led_required':False},
           'required_next_action':'independent_review_before_single_live_writer_install'}
    q.save(outdir/'job.json', job)
    return job


def load_job(path):
    job = json.loads(Path(path).read_text())
    q.need(job.get('schema') == SCHEMA and job.get('profile') == PROFILE and
           re.fullmatch('[0-9a-f]{32}', job.get('job_id','')), 'invalid writer job')
    q.need(job.get('cleanup_targets') == ['nflasha1','nflasha2'] and
           job.get('companion_update') == {'region':'nflasha8','file':'nor_ldr.img','retained':True},
           'writer cleanup/companion scope differs')
    for pin in [*job['source_partitions'].values(), *job['source_manifests'],
                *job['candidates'].values(), *job['tool_inputs']]: q.check_pin(pin)
    return job


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path); p.add_argument('sony_body', type=Path)
    p.add_argument('loader_tar', type=Path); p.add_argument('output', type=Path)
    p.add_argument('--capture', action='store_true', help='Explicit existing capture; not proof of a completed full restore')
    p.add_argument('--mksquashfs', default='mksquashfs')
    a = p.parse_args(argv)
    sources, manifests = q.capture_pins(a.source) if a.capture else q.snapshot_pins(a.source)
    job = build(sources, manifests, a.sony_body, a.loader_tar, a.output,
                source_kind='capture' if a.capture else 'snapshot', mksquashfs=a.mksquashfs)
    print(json.dumps(job, indent=2))


# These validators do not attach to a camera or authorize reset/cleanup writes.
def _fields(data):
    text = data.split(b'\0', 1)[0].decode('ascii')
    rows = [line.split('=', 1) for line in text.splitlines() if line]
    q.need(all(len(x) == 2 for x in rows) and len({x[0] for x in rows}) == len(rows),
           'malformed or duplicate writer receipt fields')
    return dict(rows)


def validate_temporary(job, p1_path, p2_path):
    expected = Path(job['candidates']['nflasha1']['path']).read_bytes()
    actual = Path(p1_path).read_bytes(); q.need(len(actual) == len(expected), 'writer p1 size changed')
    fs = Fat(expected); entry, first, size, _ = fs.locate(BOX_NAME)
    q.need(size == BOX_SIZE, 'writer mailbox size differs')
    normalized = bytearray(actual)
    if fs.bits in (12,16) and expected[37] == 0 and actual[37] == 1: normalized[37] = 0
    for start, length in [(fs.offset(c), fs.cluster_size) for c in fs.chain(first)] + [(entry+13,7),(entry+22,4)]:
        normalized[start:start+length] = expected[start:start+length]
    q.need(bytes(normalized) == expected, 'unexpected non-job writer p1 change')
    box = Fat(actual).read(BOX_NAME); head = _fields(box[:4096])
    q.need(head.get('schema') == SCHEMA and head.get('job_id') == job['job_id'], 'foreign writer mailbox')
    p2 = Path(p2_path).read_bytes()
    if head.get('exit_trigger_verified') == 'true':
        q.need(_exit_proof(job,parse_ledger(box,job['job_id']),head)[0], 'exit flag lacks exact native receipts')
        source=Path(job['source_partitions']['nflasha2']['path']).read_bytes()
        q.need(len(p2)==len(source), 'normal-boot p2 size differs')
        _owned_flags_restored(source,p2)
    else:
        q.need(p2 == Path(job['candidates']['nflasha2']['path']).read_bytes(),
               'unexpected non-job writer p2 change')
    return head, box


def parse_ledger(box, job_id):
    offset, length = SLOTS['ledger']; result = []; ended = False
    for start in range(offset, offset+length, 512):
        data = box[start:start+512]
        if data == bytes(512): ended = True; continue
        q.need(not ended, 'writer ledger contains a hole')
        row = _fields(data)
        q.need(set(row) == {'job_id','operation','event','kind','command','status','component',
                           'input_bytes','input_md5','address','bus_mode','erase_type','erase_start',
                           'erase_count','payload_md5','commit'}, 'incomplete writer ledger fields')
        q.need(row.get('job_id') == job_id and row.get('commit') == job_id, 'uncommitted writer ledger record')
        payload = data.split(b'payload_md5=',1)[0]
        q.need(row.get('payload_md5') == hashlib.md5(payload).hexdigest(), 'torn writer ledger record')
        op = int(row.get('operation','-1'))
        q.need(op == len(result)//2+1 and row.get('event') == ('intent' if len(result)%2 == 0 else 'result'),
               'writer ledger order differs')
        q.need(row.get('kind') in ('ordinary','query','array','companion','exit'), 'unknown native operation kind')
        if row['event'] == 'intent':
            q.need(row.get('status') == 'pending', 'native intent already contains a result')
        else:
            q.need(re.fullmatch(r'[0-9]{1,3}', row.get('status','')) and int(row['status']) <= 255,
                   'invalid native exit status')
        if row['event'] == 'result':
            keys = set(row)-{'event','status','payload_md5'}
            q.need({k:row[k] for k in keys} == {k:result[-1][k] for k in keys},
                   'writer native result differs from its intent')
        result.append(row)
    return result


def _all_native_results_confirmed(rows):
    if len(rows)%2: return False
    for row in rows[1::2]:
        accepted = {'0'}
        if row['kind'] == 'query':
            accepted = {'ud_read_lsi_type.elf': {'3'},
                        'ud_get_boot_device.elf': {'1'},
                        'ud_cp_firmup.elf -i': {'0','1'}}.get(row['command'], {'0'})
        if row['status'] not in accepted: return False
    return True


def _owned_flags_restored(source,actual):
    for data in (source,actual):
        fs=Fat(data); parent=fs.entries().get('UPDATER')
        q.need(parent is not None and parent[3]&16, 'missing p2 updater directory')
        entries=fs.entries(parent[1])
        q.need(not any(name in entries for name in ('MODE','MODE1')), 'owned MODE/MODE1 trigger still present')


def _exit_expected(job):
    before=Path(job['source_partitions']['nflasha2']['path']).read_bytes()
    after=Path(job['candidates']['nflasha2']['path']).read_bytes()
    changed=sorted({i//512 for i,(a,b) in enumerate(zip(before,after)) if a!=b})
    specs=job.get('exit_sectors',[])
    q.need([r['sector'] for r in specs]==changed and 0<len(changed)<=2, 'exit sector footprint differs')
    expected=[]
    for row in specs:
        n=row['sector']; path=f'/norq/p2-exit-{n}.bin'
        q.need(row['bytes']==512 and row['source_name']==path and
               row['original_md5']==hashlib.md5(before[n*512:(n+1)*512]).hexdigest() and
               row['candidate_md5']==hashlib.md5(after[n*512:(n+1)*512]).hexdigest(), 'exit sector pins differ')
        meta=('exit_trigger',512,row['original_md5'],n*512)
        for cmd in (f'dd if=/dev/nflasha2 of=/tmp_updater/p2-pre-{n}.bin bs=512 skip={n} count=1',
                    f'write_trigger {path} {n}','sync',
                    f'dd if=/dev/nflasha2 of=/tmp_updater/p2-post-{n}.bin bs=512 skip={n} count=1'):
            expected.append((cmd,meta))
    expected += [('ud_pd_firmup.elf b',('normal_reboot',0,'none',0)),
                 ('ud_reboot.elf',('normal_reboot',0,'none',0))]
    return expected


def _exit_proof(job,rows,head):
    expected=_exit_expected(job); exits=[r for r in rows if r['kind']=='exit']
    intents=exits[::2]
    q.need(len(intents)<=len(expected), 'extra native exit operation')
    for row,(cmd,(component,size,digest,address)) in zip(intents,expected):
        q.need(row['event']=='intent' and row['command']==cmd and row['component']==component and
               int(row['input_bytes'])==size and row['input_md5']==digest and int(row['address'])==address and
               int(row['bus_mode'])==1 and
               all(int(row['erase_'+k])==0 for k in ('type','start','count')), 'native exit intent differs')
    # No interspersed command after companion unmount: only this exact suffix.
    if exits: q.need(rows[-len(exits):]==exits, 'interspersed command after terminal companion unmount')
    trigger_records=(len(expected)-2)*2
    restored=(len(exits)>=trigger_records and _all_native_results_confirmed(exits[:trigger_records]) and
              head.get('exit_trigger_verified')=='true')
    if restored:
        q.need(all(head.get(f"exit_{r['sector']}_pre_md5")==r['candidate_md5'] and
                   head.get(f"exit_{r['sector']}_post_md5")==r['original_md5'] for r in job['exit_sectors']),
               'native exit preimage/readback digest evidence differs')
    reboot=(restored and len(exits)==2*len(expected)-1 and
            _all_native_results_confirmed(exits[:-1]) and exits[-1]['command']=='ud_reboot.elf' and
            exits[-1]['event']=='intent' and head.get('state')=='NATIVE_REBOOT_INTENT')
    return restored,reboot


def _validate_array_prefix(job, rows, head):
    # Commands are typed by their real operation, never by a caller's kind tag.
    # This also proves the native flash init and mode transitions, rather than
    # trusting the self-reported bus_mode field on each destructive call.
    cp_expected = [('ordinary','ud_cp_firmup.elf '+x) for x in (
        '-l','-c','-u /tmp_updater/updater/bodyfs/cp/tomco.txt -a 0x100000',
        '-u /tmp_updater/updater/bodyfs/cp/udtrcp.bin -a 0x108000',
        '-u /tmp_updater/nor_loader/Backup.bin -a 0x4000000','-j',
        f"-t /tmp_updater/ddrtraining.dat -a 0x3e210fc -s {job['training']['bytes']}")]
    expected = []
    for op in job['operations']:
        if op['component'] in ('header','mis','phy','cpapp'):
            cp_expected += [('query','ud_cp_firmup.elf -i'),
                            ('ordinary',f"ud_cp_firmup.elf -m {op['bus_mode']}")]
            if op['bus_mode'] == 1: cp_expected.append(('ordinary','ud_cp_firmup.elf -d 0'))
        erase = op['erase']
        if erase:
            command = f"ud_cp_firmup.elf -e {erase['type']} -a {erase['start']} -s {erase['count']}"
            expected.append((op, command, True)); cp_expected.append(('array',command))
        source = op['source_name'] if op['source_name'].startswith('/') else './'+op['source_name']
        command = f"ud_cp_firmup.elf {op['program_option']} {source} -a {op['address']}"
        expected.append((op, command, False)); cp_expected.append(('array',command))
    cp_rows = [r for r in rows if Path(r['command'].split()[0]).name == 'ud_cp_firmup.elf']
    cp_intents = [r for r in cp_rows if r['event']=='intent']
    q.need(len(cp_intents) <= len(cp_expected) and
           [(r['kind'],r['command']) for r in cp_intents] == cp_expected[:len(cp_intents)],
           'native CP command/kind or bus transition differs from matched sequence')
    intents = [r for r in rows if r['kind'] == 'array' and r['event'] == 'intent']
    q.need(len(intents) <= len(expected), 'unexpected extra destructive operation')
    digests = {'header':head.get('header_md5'), 'loader_backup':head.get('loader_backup_md5'),
               'training':head.get('training_md5'), 'loader':job['loader']['md5']}
    names = {'dgu':'DGU.bin','mis':'MIS.bin','phy':'pcie_phy_fw.bin','cpapp':'cpapp-b.bin'}
    digests.update({k:job['components'][n]['md5'] for k,n in names.items()})
    for row, (op, command, erased) in zip(intents, expected):
        q.need(row['component'] == op['component'] and row['command'] == command and
               int(row['address']) == op['address'] and int(row['bus_mode']) == op['bus_mode'] and
               row['input_md5'] == digests[op['component']], 'native NOR operation differs from matched manifest')
        size = int(head['loader_backup_bytes']) if op['bytes'] is None else op['bytes']
        q.need(int(row['input_bytes']) == size, 'native NOR input length differs')
        e = op['erase'] if erased else {'type':0,'start':0,'count':0}
        q.need(all(int(row['erase_'+k]) == e[k] for k in ('type','start','count')), 'native erase footprint differs')
    array_results = [r for r in rows if r['kind'] == 'array' and r['event'] == 'result']
    return (len(intents) == len(expected) and len(array_results) == len(expected) and
            all(r['status']=='0' for r in array_results) and len(cp_rows)==2*len(cp_expected) and
            _all_native_results_confirmed(cp_rows) and
            all(r['status']==head.get('flash_type') for r in cp_rows
                if r['event']=='result' and r['command']=='ud_cp_firmup.elf -i'))


def _validate_receipts(job,head,box):
    ledger = parse_ledger(box, job['job_id'])
    array_proven = _validate_array_prefix(job,ledger,head)
    if head.get('array_verified') == 'true':
        q.need(array_proven and head.get('array_started') == 'true' and head.get('inflight') == 'false',
               'array verified flag lacks complete native receipts')
    q.need(not head.get('companion_verified') == 'true' or head.get('array_verified') == 'true',
           'companion success without verified NOR array')
    if head.get('state') == 'COMPANION_VERIFIED':
        q.need(head.get('array_verified') == head.get('companion_verified') == 'true' and
               head.get('inflight') == 'false' and len(ledger)%2 == 0 and
               head.get('error') == 'none' and head.get('flash_type') in ('0','1') and
               int(head['ledger_records']) == len(ledger) and int(head['operation']) == len(ledger)//2 and
               _all_native_results_confirmed(ledger),
               'incomplete writer terminal receipt')
    if head.get('companion_verified') == 'true':
        expected_companion = ['mount -t vfat -o rw,sync,noatime,nodiratime /dev/nflasha8 /cp',
                              'cp ./nor_ldr.img /cp/nor_ldr.img', 'sync', 'umount /cp',
                              'mount -t vfat -o ro,noatime,nodiratime /dev/nflasha8 /cp',
                              'md5sum /cp/nor_ldr.img', 'umount /cp']
        companion = [r for r in ledger if r['kind']=='companion' and r['event']=='intent']
        q.need(head.get('state') in ('COMPANION_VERIFIED','EXIT_TRIGGER_RESTORE','EXIT_TRIGGER_VERIFIED','NATIVE_REBOOT_INTENT','EXIT_FAILED') and
               [r['command'] for r in companion] == expected_companion and
               all(r['component']=='companion' and r['input_md5']==head.get('companion_md5') and
                   int(r['input_bytes'])==417792 for r in companion),
               'companion verified flag lacks native persistence receipts')
        first=next(i for i,r in enumerate(ledger) if r['kind']=='companion')
        q.need(all(r['kind']=='companion' for r in ledger[first:first+14]) and
               [r['command'] for r in ledger[first:first+14:2]]==expected_companion and
               _all_native_results_confirmed(ledger[first:first+14]), 'incomplete native companion persistence')
        suffix=ledger[first+14:]
        q.need(all(r['kind']=='exit' for r in suffix), 'native command after terminal companion unmount')
    exit_verified,reboot_requested=_exit_proof(job,ledger,head)
    if head.get('state')=='NATIVE_REBOOT_INTENT':
        q.need(array_proven and head.get('array_verified')==head.get('companion_verified')=='true' and
               exit_verified and reboot_requested and int(head['ledger_records'])==len(ledger) and
               int(head['operation'])==(len(ledger)+1)//2 and head.get('error')=='none' and
               _all_native_results_confirmed(ledger[:-1]), 'incomplete success-only native reboot receipt')
    return ledger,array_proven,exit_verified,reboot_requested


def collect(job_path, p1_path, p2_path, output, *, p8_path=None):
    job = load_job(job_path); head, box = validate_temporary(job,p1_path,p2_path)
    out = Path(output).resolve(); q.need(not out.exists(), 'writer collection already exists')
    ledger,array_proven,exit_verified,reboot_requested=_validate_receipts(job,head,box)
    out.mkdir(mode=0o700)
    evidence = {}
    for name, length_key, md5_key, fixed in (
        ('companion','companion_bytes','companion_md5',417792),
        ('header',None,'header_md5',2048),
        ('loader_backup','loader_backup_bytes','loader_backup_md5',None),
        ('training','training_bytes','training_md5',1536)):
        digest = head.get(md5_key,'none')
        if digest == 'none': continue
        length = int(head[length_key]) if length_key else fixed
        offset, cap = SLOTS[name]
        q.need(0 < length <= cap and (fixed is None or length == fixed), 'invalid generated evidence length')
        if name == 'loader_backup': q.need(length <= 2048, 'loader Backup exceeds NOR allocation')
        data = box[offset:offset+length]
        q.need(hashlib.md5(data).hexdigest() == digest, 'generated writer evidence checksum differs: '+name)
        path = out/(name+'.bin'); path.write_bytes(data); path.chmod(0o600); evidence[name] = q.file_pin(path)
    start, count = SLOTS['native_log']; path = out/'native.log'
    path.write_bytes(box[start:start+count].rstrip(b'\0')); path.chmod(0o600)
    p8_proven = False
    if p8_path is not None:
        q.need(head.get('companion_verified') == 'true' and 'companion' in evidence, 'no verified companion to compare')
        original = Path(job['source_partitions']['nflasha8']['path']).read_bytes()
        actual = Path(p8_path).read_bytes(); q.need(len(actual) == len(original), 'companion region size differs')
        before, after = all_files(Fat(original)), all_files(Fat(actual))
        q.need(set(before) == set(after) and all(after[k] == v for k,v in before.items()
               if k.upper() != 'NOR_LDR.IMG'), 'unrelated nfa8 file changed')
        q.need(Fat(actual).read('nor_ldr.img') == Path(evidence['companion']['path']).read_bytes(),
               'persisted nfa8 companion differs')
        q.need(actual[Fat(original).total:] == original[Fat(original).total:], 'nfa8 physical tail changed')
        p8_proven = True
    result = {'schema':SCHEMA,'job_id':job['job_id'],'stage':head,'ledger':ledger,
              'array_write_verified':array_proven and head.get('array_verified')=='true',
              'companion_verified':head.get('companion_verified')=='true',
              'host_p8_companion_and_other_files_verified':p8_proven,
              'cleanup_completed':False,'evidence':evidence,'job_manifest':q.file_pin(job_path),
              'exit_trigger_verified':exit_verified,'native_reboot_requested':reboot_requested,
              'normal_usb_observed':False,
              'captured':{'nflasha1':q.file_pin(p1_path),'nflasha2':q.file_pin(p2_path)},
              'historical_nor_equality_is_required':False,'can_execute':False}
    q.save(out/'collection.json',result)
    return result


def cleanup_plan(job_path,p1_path,p2_path):
    job=load_job(job_path);head,box=validate_temporary(job,p1_path,p2_path)
    rows,array_proven,exit_verified,reboot_requested=_validate_receipts(job,head,box)
    if exit_verified:
        q.need(array_proven and
               head.get('array_verified')==head.get('companion_verified')=='true',
               'p2 exit cleanup requires confirmed array and companion results')
    targets=['nflasha1'] if exit_verified else job['cleanup_targets']
    return {'schema':SCHEMA,'job_id':job['job_id'],'job_manifest':q.file_pin(job_path),
            'status':'OFFLINE_CLEANUP_PLAN','can_execute':False,'cleanup_completed':False,
            'observed_stage':head.get('state'),'restore':{n:job['source_partitions'][n] for n in targets},
            'captured':{'nflasha1':q.file_pin(p1_path),'nflasha2':q.file_pin(p2_path)},
            'exit_trigger_verified':exit_verified,'native_reboot_requested':reboot_requested,
            'preserve_p2':q.file_pin(p2_path) if exit_verified else None,
            'nfa8_restore':False,'requires_confirmed_terminal_and_fresh_preimages':True,
            'normal_reboot_requires_array_and_companion_verified':True}


def verify_cleanup(job_path,plan_path,p1_readback,p2_readback):
    job=load_job(job_path);plan=json.loads(Path(plan_path).read_text())
    q.need(plan == cleanup_plan(job_path,plan['captured']['nflasha1']['path'],
                               plan['captured']['nflasha2']['path']), 'writer cleanup plan differs')
    proof={}
    for name,path in [('nflasha1',p1_readback),('nflasha2',p2_readback)]:
        source=plan['preserve_p2'] if name=='nflasha2' and plan['preserve_p2'] is not None else job['source_partitions'][name]
        q.need(not Path(path).samefile(source['path']) and
               not Path(path).samefile(job['source_partitions'][name]['path']),
               'cleanup source is not independent readback')
        pin=q.file_pin(path)
        q.need(all(pin[k]==source[k] for k in ('bytes','sha256')), 'writer cleanup readback differs: '+name)
        proof[name]=pin
    return {'schema':SCHEMA,'job_id':job['job_id'],'cleanup_completed':True,
            'status':'CLEANUP_READBACK_VERIFIED','nfa8_restored':False,'readbacks':proof,
            'p2_preserved_after_native_trigger_cleanup':plan['exit_trigger_verified'],
            'job_manifest':q.file_pin(job_path),'cleanup_plan':q.file_pin(plan_path),
            'array_write_completion_is_separate':True,'evidence_scope':'caller_supplied_files_not_live_transport_attestation'}


if __name__ == '__main__': main()
