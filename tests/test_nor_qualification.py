"""Purpose checks for offline NOR entry and snapshot-referenced cleanup."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest

from recovery_tool import nor_qualification as q
from recovery_tool.stock_fat import Fat
from recovery_tool.test_stock_sd import settings_fixture


def large_fat():
    data=bytearray(settings_fixture())
    data.extend(bytes(128*512))
    struct.pack_into('<H',data,19,256)
    return bytes(data)+b'PHYSICAL-TAIL'.ljust(4096,b'T')


class QualificationTests(unittest.TestCase):
    def runner_fixture(self, root, fault=''):
        """Run our shell control flow with all hardware commands replaced by mocks."""
        fake=root/'fakebin';fake.mkdir()
        source=root/'source';source.mkdir()
        for name in ('DGU.bin','MIS.bin','loader_spi.bin','cpapp-b.bin','pcie_phy_fw.bin','Backup.bin'):
            (source/name).write_bytes(('SYNTHETIC-'+name).encode())
        nq=root/'norq';(nq/'cp').mkdir(parents=True)
        for n in ('tomco.txt','udtrcp.bin'):(nq/'cp'/n).write_bytes(b'SYNTHETIC')
        (nq/'job.conf').write_text('JOB_ID='+('a'*32)+'\n')
        with tarfile.open(nq/'loader_nor.tar','w') as tf:
            tf.add(source/'DGU.bin',arcname='loader_nor.bin')
        (root/'device-mailbox').write_bytes(q.initial_mailbox('a'*32))
        (root/'commands').touch()
        for name in ('tmp_updater','cp','setting'):(root/name).mkdir()
        # Match the actual Sony SquashFS topology and canonical /proc/mounts.
        (root/'ramdsk').symlink_to('tmp_updater',target_is_directory=True)
        (root/'dev').mkdir()
        for n in ('mem','pcidma','nflasha1','nflasha2','nflasha8'):(root/'dev'/n).touch()
        mountpoint = 'tmp' if fault == 'missing_tmpfs' else 'tmp_updater'
        (root/'mounts').write_text(f'tmpfs {root}/{mountpoint} tmpfs rw 0 0\n')
        rows=[]
        for n in ('DGU.bin','MIS.bin','loader_spi.bin','cpapp-b.bin','pcie_phy_fw.bin','Backup.bin'):
            dest=root/('setting' if n=='Backup.bin' else 'cp')/n
            rows.append(hashlib.md5((source/n).read_bytes()).hexdigest()+'  '+str(dest))
        (nq/'source-files.md5').write_text('\n'.join(rows)+'\n')
        mock=f'''#!{sys.executable}
import os,sys,pathlib,shutil,hashlib
r=pathlib.Path(os.environ['MOCK_ROOT']); n=pathlib.Path(sys.argv[0]).name; a=sys.argv[1:]
with (r/'commands').open('a') as s:s.write(n+' '+' '.join(a)+'\\n')
if n=='mount':
 if '--bind' in a:
  sys.exit(97)  # No bind may obscure the canonical tmpfs or its children.
 elif a[-2].endswith('nflasha1'):
  os.link(r/'device-mailbox',pathlib.Path(a[-1])/'NORQ.DAT')
 else:
  for p in (r/'source').iterdir():shutil.copyfile(p,pathlib.Path(a[-1])/p.name)
elif n=='md5sum':
 if a[0]=='-c':
  for line in pathlib.Path(a[1]).read_text().splitlines():
   h,p=line.split('  ',1)
   if hashlib.md5(pathlib.Path(p).read_bytes()).hexdigest()!=h:sys.exit(1)
 else:print(hashlib.md5(pathlib.Path(a[0]).read_bytes()).hexdigest()+'  '+a[0])
elif n=='bksb.elf':pathlib.Path(a[-1]).write_bytes(b'SYNTHETIC-BACKUP')
elif n=='ud_nor_pformat.elf':pathlib.Path('nor_ldr.img').write_bytes(b'EXBL-SYNTHETIC')
elif n=='ud_read_lsi_type.elf':sys.exit(4 if os.environ.get('MOCK_FAULT')=='lsi' else 3)
elif n=='ud_get_boot_ext.elf':sys.exit(2 if os.environ.get('MOCK_FAULT')=='boot_ext' else 0)
elif n=='ud_get_boot_device.elf':sys.exit(2 if os.environ.get('MOCK_FAULT')=='boot_device' else 1)
elif n=='ud_cp_firmup.elf':
 if a[0] in ('-e','-f','-y'):sys.exit(98)
 if os.environ.get('MOCK_FAULT')=='flash' and a[0]=='-i':sys.exit(255)
 if os.environ.get('MOCK_FAULT')=='bootstrap' and a[0]=='-u':sys.exit(9)
 if a[0]=='-r':
  size=4095 if os.environ.get('MOCK_FAULT')=='short' else 4096
  (r/'tmp_updater/nor.bin').write_bytes(bytes([int(a[2])//4096%256])*size)
'''
        # Sony's actual BusyBox dd has FEATURE_DD_IBS_OBS disabled. Host dd
        # otherwise hides unsupported conv=notrunc in the first receipt write.
        dd=fake/'dd';dd.write_text(f'''#!{sys.executable}
import os,sys
allowed={{'bs','count','seek','skip','if','of','status'}}
if any(a.split('=',1)[0] not in allowed for a in sys.argv[1:]):sys.exit(1)
os.execv('/bin/dd',['dd']+sys.argv[1:])
''');dd.chmod(0o755)
        driver=fake/'driver';driver.write_text(mock);driver.chmod(0o755)
        for name in ('mount','umount','insmod','sync','md5sum','bksb.elf','ud_nor_pformat.elf',
                     'ud_read_lsi_type.elf','ud_get_boot_ext.elf','ud_get_boot_device.elf',
                     'ud_spicom_boot.elf','ud_init_spi_port_setting.elf','spicom.elf',
                     'ud_cp_firmup.elf','uc_portctl.elf'):
            (fake/name).symlink_to(driver)
        text=Path(q.__file__).with_name('nor_qualification_runner.sh.in').read_text()
        # Namespace all camera paths. No generated firmware executable is used.
        text=re.sub(r'(?<![A-Za-z0-9_])/(norq_p1|tmp_updater|ramdsk|setting|norq|cp|usr/kmod)(?=/|\b)',
                    lambda match: str(root/match[1].replace('/','_')),text)
        text=text.replace('/dev/nflasha',str(root/'dev/nflasha')).replace('/dev/mem',str(root/'dev/mem'))
        text=text.replace('/dev/pcidma',str(root/'dev/pcidma')).replace('/proc/mounts',str(root/'mounts'))
        text=text.replace('test -c ', 'test -e ')
        text=text.replace('park() { while true; do sleep 60; done; }','park() { exit 0; }')
        lines=text.splitlines();lines=[f'PATH={fake}:/usr/bin:/bin:/usr/sbin:/sbin' if l.startswith('PATH=') else l for l in lines]
        script=root/'mocked-runner.sh';script.write_text('\n'.join(lines)+'\n')
        env={**os.environ,'MOCK_ROOT':str(root),'MOCK_FAULT':fault}
        root.chmod(0o555)  # Emulate the immutable SquashFS root; RAM child remains writable.
        try:
            subprocess.run(['sh',str(script)],env=env,check=True,capture_output=True,timeout=20)
        finally:
            root.chmod(0o755)
        return (root/'device-mailbox').read_bytes(),(root/'commands').read_text(),script,env

    def test_shell_runner_success_and_one_shot_latch_with_mocked_hardware(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);box,commands,script,env=self.runner_fixture(root)
            self.assertIn(b'state=READ_COMPLETE\n',box[:4096])
            self.assertIn(b'reads=5\n',box[:4096])
            self.assertEqual(len(box),q.BOX_SIZE)
            # Header rewrites/log writes must preserve every completed sample.
            for i,address in enumerate(q.READ_ADDRESSES):
                self.assertEqual(box[(i+1)*4096:(i+2)*4096],bytes([address//4096%256])*4096)
            self.assertEqual(commands.count('ud_cp_firmup.elf -r '),5)
            self.assertTrue((root/'ramdsk').is_symlink())
            self.assertEqual((root/'ramdsk').resolve(),(root/'tmp_updater').resolve())
            self.assertNotIn('mount --bind ',commands)
            # Mock mount becomes an idempotent no-op for the second entry.
            (root/'tmp_updater/norq_p1/NORQ.DAT').unlink()
            subprocess.run(['sh',str(script)],env=env,check=True,capture_output=True,timeout=20)
            later=(root/'commands').read_text()[len(commands):]
            self.assertNotIn('ud_cp_firmup.elf',later)
            self.assertEqual((root/'device-mailbox').read_bytes(),box)

    def test_shell_runner_refuses_missing_canonical_tmpfs_before_native_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            box,commands,_,_=self.runner_fixture(Path(tmp),'missing_tmpfs')
            self.assertIn(b'error=missing_ramdsk_tmpfs\n',box[:4096])
            self.assertIn(b'failed_from=ENTERED\n',box[:4096])
            self.assertNotIn('ud_cp_firmup.elf',commands)
            self.assertNotIn('ud_nor_pformat.elf',commands)

    def test_shell_runner_stops_on_bootstrap_error_and_short_read(self):
        for fault,expected in [('bootstrap',b'native_command_failed:ud_cp_firmup.elf'),('short',b'short_nor_read')]:
            with tempfile.TemporaryDirectory() as tmp:
                box,commands,_,_=self.runner_fixture(Path(tmp),fault)
                self.assertIn(b'state=FAILED\n',box[:4096])
                self.assertIn(expected,box[:4096])
                self.assertNotIn(b'state=READ_COMPLETE',box[:4096])
                if fault=='bootstrap':
                    self.assertNotIn('ud_cp_firmup.elf -r ',commands)
                    self.assertIn(b'native_exit_status=9\n',box[:4096])
                    self.assertIn(b'failed_from=CP_BOOT_STARTED\n',box[:4096])

    def test_query_failures_preserve_actual_command_and_data_status(self):
        for fault,command,status in [('lsi','ud_read_lsi_type.elf',4),
                                     ('boot_ext','ud_get_boot_ext.elf',2),
                                     ('boot_device','ud_get_boot_device.elf',2),
                                     ('flash','ud_cp_firmup.elf -i',255)]:
            with tempfile.TemporaryDirectory() as tmp:
                box,_,_,_=self.runner_fixture(Path(tmp),fault)
                head=box[:4096].split(b'\0',1)[0]
                self.assertIn(b'state=FAILED\n',head)
                self.assertIn(f'native_command={command}\n'.encode(),head)
                self.assertIn(f'native_exit_status={status}\n'.encode(),head)
    def test_fat_extension_preserves_neighbors_tail_and_all_fat_copies(self):
        before=large_fat();fs=Fat(before)
        original_settings=fs.read('updater/dat4')
        q._put_file(fs,'Backup.bin',b'N'*3000)
        q._put_file(fs,'NORQ.DAT',bytes(q.BOX_SIZE),new_root=True)
        after=Fat(fs.bytes())
        self.assertEqual(after.read('Backup.bin'),b'N'*3000)
        self.assertEqual(after.read('updater/dat4'),original_settings)
        self.assertEqual(after.read('NORQ.DAT'),bytes(q.BOX_SIZE))
        self.assertEqual(after.bytes()[after.total:],before[after.total:])
        self.assertEqual(after.data[after.fat_start:after.fat_start+after.fat_size],
                         after.data[after.fat_start+after.fat_size:after.fat_start+2*after.fat_size])

    def test_fat_exhaustion_refuses_without_partial_mutation(self):
        fs=Fat(settings_fixture());before=fs.bytes()
        with self.assertRaisesRegex(q.QualificationError,'insufficient FAT'):
            q._put_file(fs,'NORQ.DAT',bytes(q.BOX_SIZE),new_root=True)
        self.assertEqual(fs.bytes(),before)

    def test_production_trigger_preserves_version_and_backup(self):
        original=settings_fixture(b'\x04\x04')
        changed=q.p2_candidate(original);fs=Fat(changed)
        self.assertEqual(fs.read('updater/mode'),b'')
        self.assertEqual(fs.read('updater/mode1'),b'')
        self.assertEqual(fs.read('updater/dat4'),b'\x04\x04')
        self.assertEqual(fs.read('Backup.bin'),Fat(original).read('Backup.bin'))
        with self.assertRaisesRegex(q.QualificationError,'conflict'):
            q.p2_candidate(changed)

    def fixture(self, root):
        jobid='a'*32;original=large_fat();fs=Fat(original)
        q._put_file(fs,'NORQ.DAT',q.initial_mailbox(jobid),new_root=True)
        files={}
        for name,data in [('A-p1',original),('A-p2',settings_fixture()),('A-p8',b'p8'),
                          ('candidate-p1',fs.bytes()),('candidate-p2',q.p2_candidate(settings_fixture())),
                          ('manifest',b'{}')]:
            p=root/name;p.write_bytes(data);files[name]=p
        job={'schema':q.SCHEMA,'job_id':jobid,'nor_array_write_supported':False,
             'source_partitions':{f'nflasha{s}':q.file_pin(files[f'A-p{s}']) for s in (1,2,8)},
             'source_manifests':[q.file_pin(files['manifest'])],
             'candidates':{f'nflasha{s}':q.file_pin(files[f'candidate-p{s}']) for s in (1,2)}}
        p=root/'job.json';q.save(p,job)
        return p,job,files

    def completed(self, files, jobid):
        fs=Fat(files['candidate-p1'].read_bytes());box=bytearray(q.BOX_SIZE)
        fields=[f'schema={q.SCHEMA}',f'job_id={jobid}','state=READ_COMPLETE','error=none',
                'flash_type=0','nor_array_write_started=false','reads=5']
        for i in range(5):
            data=bytes([i+1])*4096;box[(i+1)*4096:(i+2)*4096]=data
            fields.append(f'sample_{i}_md5={hashlib.md5(data).hexdigest()}')
        header=('\n'.join(fields)+'\n').encode();box[:len(header)]=header
        fs.replace('NORQ.DAT',box)
        live=files['candidate-p1'].with_name('captured-p1');live.write_bytes(fs.bytes())
        return live

    def test_collect_completed_samples_then_snapshot_cleanup_and_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);jp,job,files=self.fixture(root);live=self.completed(files,job['job_id'])
            nor=root/'fresh-nor.bin'
            with nor.open('wb') as stream:stream.truncate(64*1024*1024)
            with nor.open('r+b') as stream:
                for i,off in enumerate(q.READ_ADDRESSES):stream.seek(off);stream.write(bytes([i+1])*4096)
            result=q.collect(jp,live,files['candidate-p2'],root/'collected',fastboot_nor=nor)
            self.assertTrue(result['qualification_read_complete'])
            self.assertTrue(result['cross_reader_equal'])
            self.assertFalse(result['nor_array_write_completed'])
            plan=q.cleanup_plan(jp,live,files['candidate-p2']);pp=root/'cleanup.json';q.save(pp,plan)
            self.assertEqual(plan['restore']['nflasha1']['path'],str(files['A-p1'].resolve()))
            readbacks=[]
            for s in (1,2):
                rb=root/f'synthetic-readback-p{s}';rb.write_bytes(files[f'A-p{s}'].read_bytes());readbacks.append(rb)
            proof=q.verify_cleanup(jp,pp,*readbacks)
            self.assertTrue(proof['cleanup_completed'])
            self.assertFalse(proof['nfa8_restored'])
            self.assertEqual(set(root.glob('*rollback*')),set())

    def test_non_job_mutation_and_source_drift_refuse_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);jp,job,files=self.fixture(root);live=self.completed(files,job['job_id'])
            data=bytearray(live.read_bytes());data[-1]^=1;live.write_bytes(data)
            with self.assertRaisesRegex(q.QualificationError,'non-job p1'):
                q.cleanup_plan(jp,live,files['candidate-p2'])
            files['A-p2'].write_bytes(b'changed')
            with self.assertRaisesRegex(q.QualificationError,'pinned input changed'):
                q.load_job(jp)

    def test_corrupt_short_foreign_and_incomplete_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);jp,job,files=self.fixture(root)
            for kind in ('foreign','corrupt','missing-read','short'):
                live=self.completed(files,job['job_id']);fs=Fat(live.read_bytes());box=bytearray(fs.read('NORQ.DAT'))
                if kind=='foreign':box[:4096]=bytes(box[:4096]).replace(b'a'*32,b'b'*32)
                elif kind=='corrupt':box[4096]^=1
                elif kind=='missing-read':box[:4096]=bytes(box[:4096]).replace(b'reads=5',b'reads=4')
                else:box=box[:100]
                fs.replace('NORQ.DAT',box);live.write_bytes(fs.bytes())
                with self.subTest(kind=kind), self.assertRaises((q.QualificationError,ValueError)):
                    q.validate_temporary(job,live,files['candidate-p2'])

    def test_timestamp_only_allowed_but_cluster_field_not_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,job,files=self.fixture(root);live=self.completed(files,job['job_id'])
            fs=Fat(live.read_bytes());entry=fs.locate('NORQ.DAT')[0]
            fs.data[entry+13]=100  # Linux FAT ctime subsecond stamp.
            fs.data[entry+22:entry+26]=b'\x21\x00\x21\x00';live.write_bytes(fs.bytes())
            q.validate_temporary(job,live,files['candidate-p2'])
            fs.data[entry+12]=1;live.write_bytes(fs.bytes())
            with self.assertRaisesRegex(q.QualificationError,'non-job p1'):
                q.validate_temporary(job,live,files['candidate-p2'])
            fs.data[entry+12]=0
            fs.data[entry+20]=1;live.write_bytes(fs.bytes())
            with self.assertRaisesRegex(q.QualificationError,'non-job p1'):
                q.validate_temporary(job,live,files['candidate-p2'])

    def test_fat_dirty_bit_allowed_but_other_boot_state_bits_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);jp,job,files=self.fixture(root)
            live=self.completed(files,job['job_id'])
            original=live.read_bytes()
            dirty=bytearray(original);dirty[37]^=1;live.write_bytes(dirty)
            plan=q.cleanup_plan(jp,live,files['candidate-p2'])
            self.assertTrue(plan['fat_volume_state']['dirty_bit_changed'])
            for offset,mask in ((37,2),(37,128),(36,1),(38,1)):
                changed=bytearray(dirty);changed[offset]^=mask;live.write_bytes(changed)
                with self.subTest(offset=offset,mask=mask), self.assertRaisesRegex(q.QualificationError,'non-job p1'):
                    q.validate_temporary(job,live,files['candidate-p2'])

    def test_readback_mismatch_cannot_mark_cleanup_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);jp,job,files=self.fixture(root);live=self.completed(files,job['job_id'])
            pp=root/'cleanup.json';q.save(pp,q.cleanup_plan(jp,live,files['candidate-p2']))
            with self.assertRaisesRegex(q.QualificationError,'readback mismatch'):
                q.verify_cleanup(jp,pp,files['candidate-p1'],files['A-p2'])
            with self.assertRaisesRegex(q.QualificationError,'independent cleanup readback'):
                q.verify_cleanup(jp,pp,files['A-p1'],files['A-p2'])

    def test_lfn_checksum_and_sequence_are_required(self):
        fs=Fat(large_fat());entry=fs.root_start
        # Insert a one-slot VFAT name for existing BACKUP.BIN without altering its allocation.
        fs.data[entry+64:entry+96]=fs.data[entry+32:entry+64]
        fs.data[entry+32:entry+64]=fs.data[entry:entry+32]
        short=bytes(fs.data[entry+32:entry+43]);checksum=0
        for byte in short:checksum=(((checksum&1)<<7)+(checksum>>1)+byte)&255
        text='long_name.bin'.encode('utf-16le')
        row=bytearray(32);row[0]=0x41;row[11]=0x0f;row[13]=checksum
        row[1:11],row[14:26],row[28:32]=text[:10],text[10:22],text[22:26]
        fs.data[entry:entry+32]=row
        self.assertEqual(q.read_root_file(fs,'long_name.bin'),fs.read('BACKUP.BIN'))
        fs.data[entry+13]^=1
        with self.assertRaisesRegex(q.QualificationError,'LFN checksum'):
            q.read_root_file(fs,'long_name.bin')

    def test_fat16_extension_preserves_the_second_fat(self):
        data=bytearray(8192*512)
        struct.pack_into('<HBHBHHBHH',data,11,512,1,1,2,64,8192,0xf8,32,32)
        data[510:512]=b'\x55\xaa'
        for off in (512,512+32*512):data[off:off+4]=b'\xf8\xff\xff\xff'
        fs=Fat(bytes(data));self.assertEqual(fs.bits,16)
        q._put_file(fs,'NORQ.DAT',bytes(q.BOX_SIZE),new_root=True)
        self.assertEqual(Fat(fs.bytes()).read('NORQ.DAT'),bytes(q.BOX_SIZE))

    def test_explicit_capture_sources_require_complete_pins(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            files={}
            for name in ('nflasha1','nflasha2','nflasha8'):
                path=root/(name+'.bin');path.write_bytes(name.encode())
                files[name]=q.file_pin(path)
            receipt=root/'capture.json'
            data={'schema':'nor-qualification-input-read-v1','status':'READ_COMPLETE',
                  'persistent_write_commands_issued':False,'files':files}
            receipt.write_text(json.dumps(data))
            with patch.object(q,'CAPTURE_SIZES',{n:p['bytes'] for n,p in files.items()}):
                sources,manifests=q.capture_pins(receipt)
                self.assertEqual(sources,files)
                self.assertEqual(manifests,[q.file_pin(receipt)])
                data['status']='READ_INCOMPLETE';receipt.write_text(json.dumps(data))
                with self.assertRaisesRegex(q.QualificationError,'completed read-only'):
                    q.capture_pins(receipt)
                data['status']='READ_COMPLETE';data['files']['nflasha2']['sha256']='0'*64
                receipt.write_text(json.dumps(data))
                with self.assertRaises(q.QualificationError):q.capture_pins(receipt)

    def test_device_prewrite_guard_refuses_source_drift(self):
        from recovery_tool.nor_qualification_device import verify_live_sources
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);pins={}
            for name in ('nflasha1','nflasha2'):
                path=root/(name+'.original');path.write_bytes(name.encode());pins[name]=q.file_pin(path)
            class Reader:
                def region_size(self,name):return pins[name]['bytes']
                def read_region(self,name,path,**kw):
                    Path(path).write_bytes(b'X'*pins[name]['bytes'])
            with self.assertRaisesRegex(q.QualificationError,'differs from pinned source'):
                verify_live_sources(Reader(),root,pins)

    def test_capture_entry_and_cleanup_use_same_transport_and_restore_current(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from recovery_tool import nor_qualification_device as device, write_transaction as tx
        from recovery_tool.updater_restore import Fragment, replay
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);jp,job,files=self.fixture(root)
            job['source_kind']='capture';jp.write_text(json.dumps(job))
            identity=[]
            store={f'nflasha{s}':files[f'A-p{s}'].read_bytes() for s in (1,2,8)}
            class Transport:
                def __init__(self):self.reboots=0;self.writes=[]
                def region_size(self,name):return len(store[name])
                def read_region(self,name,path,*,offset=0,length=None,**kw):
                    data=store[name][offset:None if length is None else offset+length]
                    Path(path).write_bytes(data);return len(data)
                def _send(self,command):
                    assert command=='reboot';self.reboots+=1
                def _reply(self):return b'OKAY',b''
            transport=Transport()
            def write(t,name,payload,sectors,**kw):
                self.assertIs(t,transport);self.assertIn(name,('nflasha1','nflasha2'))
                store[name]=replay(store[name],[Fragment(0,sectors,payload)])
                t.writes.append(name)
            with patch.object(device,'_capture_identity',return_value=(identity,files['manifest'])), \
                 patch.object(device,'_exclusive_transport',side_effect=lambda:nullcontext(transport)), \
                 patch.object(tx,'_write_response',side_effect=write):
                device.prepare_install(jp)
                result=device.apply(jp)
                self.assertEqual(result['status'],'REBOOT_ACKNOWLEDGED')
                self.assertEqual(transport.reboots,1)
                with self.assertRaisesRegex(q.QualificationError,'already consumed'):device.apply(jp)
                self.assertEqual(transport.reboots,1)
                # Simulate the parked qualification receipt, then capture it.
                live=self.completed(files,job['job_id']);store['nflasha1']=live.read_bytes()
                p2=root/'captured-p2';p2.write_bytes(store['nflasha2'])
                plan=root/'cleanup.json';q.save(plan,q.cleanup_plan(jp,live,p2))
                device.prepare_cleanup(jp,plan)
                result=device.apply(jp,cleanup=True)
                self.assertEqual(result['status'],'VERIFIED')
                self.assertEqual(transport.reboots,1)
                for slot in (1,2,8):self.assertEqual(store[f'nflasha{slot}'],files[f'A-p{slot}'].read_bytes())

    def test_entry_and_runner_never_fall_through_or_program_array(self):
        entry=q.patch_init(b'power-init\n#execute main procedure\nUdtrMain.sh &\n')
        self.assertLess(entry.index(b'exec /norq/runner.sh'),entry.index(b'UdtrMain.sh &'))
        self.assertIn(b'exec /norq/runner.sh\nwhile true',entry)
        script=Path(q.__file__).with_name('nor_qualification_runner.sh.in')
        subprocess.run(['sh','-n',str(script)],check=True)
        text=script.read_text()
        for line in text.splitlines():
            if 'ud_cp_firmup.elf' in line:
                self.assertNotRegex(line,r' -(e|f|y)( |$)')
        self.assertNotIn('write_nor_loader_img',text)
        self.assertNotIn('UdtrMain.sh',text)
        self.assertNotIn('reboot', '\n'.join(l for l in text.splitlines() if not l.startswith('#')))
        self.assertIn("state=PREPARED",text)
        self.assertIn('STATE=CP_BOOT_STARTED; record',text)
        self.assertIn('WORK=/tmp_updater',text)
        self.assertIn('P1=$WORK/norq_p1',text)
        self.assertNotIn('mount --bind',text)
        self.assertNotIn('P1=/norq_p1',text)


if __name__=='__main__':
    unittest.main()
