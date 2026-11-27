"""Native-order, bounded evidence and failure tests. All hardware is mocked."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from recovery_tool import nor_rebuild as w


def fields(data):
    return dict(line.split('=',1) for line in data.split(b'\0',1)[0].decode().splitlines() if line)


class WriterTests(unittest.TestCase):
    def run_runner(self, root, *, fault='', fail_at=0):
        fake=root/'fake';fake.mkdir();nq=root/'norq';(nq/'cp').mkdir(parents=True)
        source=root/'source';source.mkdir();(root/'commands').touch()
        for name in ('tmp_updater','cp','setting','dev'):(root/name).mkdir()
        (root/'ramdsk').symlink_to('tmp_updater')
        for name in ('mem','pcidma','nflasha1','nflasha2','nflasha8'):(root/'dev'/name).touch()
        (root/'dev/nflasha2').write_bytes(b'P'*512+(b'X'+b'T'*511 if fault=='exit_preimage' else b'T'*512))
        (nq/'p2-exit-1.bin').write_bytes(b'O'*512)
        (nq/'exit-trigger.sh').write_text('restore_trigger 1 '+str(nq/'p2-exit-1.bin')+' '+hashlib.md5(b'T'*512).hexdigest()+' '+hashlib.md5(b'O'*512).hexdigest()+'\n')
        (root/'mounts').write_text(f'tmpfs {root}/tmp_updater tmpfs rw 0 0\n')
        sizes={'DGU.bin':16,'MIS.bin':119296,'loader_spi.bin':32,'pcie_phy_fw.bin':16384,
               'cpapp-b.bin':65536,'Backup.bin':32,'nor_ldr.img':417792}
        for name,size in sizes.items():(source/name).write_bytes(bytes([len(name)])*size)
        for name in ('tomco.txt','udtrcp.bin'):(nq/'cp'/name).write_bytes(b'SYNTHETIC')
        (root/'mailbox').write_bytes(w.initial_mailbox('a'*32))
        (nq/'job.conf').write_text(f'''JOB_ID={'a'*32}
DGU_BYTES=16
MIS_BYTES=119296
LOADER_BYTES=16
PHY_BYTES=16384
CPAPP_BYTES=65536
CPAPP_BLOCK_COUNT=2
TRAIN_OFFSET=512
TRAIN_BYTES=1536
COMPANION_BYTES=417792
''')
        with (nq/'job.conf').open('a') as stream:
            for key,name in [('DGU','DGU.bin'),('MIS','MIS.bin'),('PHY','pcie_phy_fw.bin'),
                             ('CPAPP','cpapp-b.bin')]:
                stream.write(key+'_MD5='+hashlib.md5((source/name).read_bytes()).hexdigest()+'\n')
            stream.write('LOADER_MD5='+hashlib.md5(b'L'*16).hexdigest()+'\n')
        rows=[]
        for name in sizes:
            dest=root/('setting' if name=='Backup.bin' else 'cp')/name
            rows.append(hashlib.md5((source/name).read_bytes()).hexdigest()+'  '+str(dest))
        (nq/'source-files.md5').write_text('\n'.join(rows)+'\n')
        archive=root/'archive';archive.mkdir()
        for name,data in [('loader_nor.bin',b'L'*16),('LdrBkupList.txt',b'ID'),('pformat_config.txt',b'LAYOUT'),('loader_nor.md5',b'SUM')]:
            (archive/name).write_bytes(data)
        staged_rows=[]
        for name in sizes:
            if name!='nor_ldr.img':
                staged_rows.append(hashlib.md5((source/name).read_bytes()).hexdigest()+'  '+str(root/'tmp_updater/nor_loader'/name))
        for path in archive.iterdir():
            staged_rows.append(hashlib.md5(path.read_bytes()).hexdigest()+'  '+str(root/'tmp_updater/nor_loader'/path.name))
        (nq/'staged-files.md5').write_text('\n'.join(staged_rows)+'\n')
        with tarfile.open(nq/'loader_nor.tar','w') as tf:
            for path in archive.iterdir():tf.add(path,arcname=path.name)
        driver=fake/'driver';driver.write_text(f'''#!{sys.executable}
import os,sys,pathlib,shutil,hashlib,subprocess
r=pathlib.Path(os.environ['MOCK_ROOT']);n=pathlib.Path(sys.argv[0]).name;a=sys.argv[1:]
with (r/'commands').open('a') as f:f.write(n+' '+' '.join(a)+'\\n')
fault=os.environ.get('MOCK_FAULT','')
if fault=='bootstrap' and n=='ud_spicom_boot.elf':sys.exit(255)
if n.startswith('ud_') or n in ('spicom.elf','bksb.elf','insmod','uc_portctl.elf'):
 p=r/'native-count';i=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(i))
 if i==int(os.environ.get('MOCK_FAIL_AT','0')):sys.exit(255)
if n=='mount':
 if '--bind' in a:sys.exit(97)
 if a[-2].endswith('nflasha1'):os.link(r/'mailbox',pathlib.Path(a[-1])/'NORW.DAT')
 elif 'rw,sync,noatime,nodiratime' not in a and not (pathlib.Path(a[-1])/'nor_ldr.img').exists():
  for p in (r/'source').iterdir():shutil.copyfile(p,pathlib.Path(a[-1])/p.name)
elif n=='cp':
 subprocess.run(['/bin/cp']+a,check=True)
 if fault=='staged' and a[-1].endswith('/nor_loader/'):
  p=pathlib.Path(a[-1])/'DGU.bin';p.write_bytes(b'X'+p.read_bytes()[1:])
 if fault=='companion' and a[-1].endswith('/cp/nor_ldr.img'):
  p=pathlib.Path(a[-1]);p.write_bytes(b'X'+p.read_bytes()[1:])
elif n=='md5sum':
 if a[0]=='-c':
  for line in pathlib.Path(a[1]).read_text().splitlines():
   h,p=line.split('  ',1)
   if hashlib.md5(pathlib.Path(p).read_bytes()).hexdigest()!=h:sys.exit(1)
 else:print(hashlib.md5(pathlib.Path(a[0]).read_bytes()).hexdigest()+'  '+a[0])
elif n=='bksb.elf':pathlib.Path(a[-1]).write_bytes(b'B'*512)
elif n=='ud_nor_pformat.elf':
 p=pathlib.Path('nor_ldr.img')
 if not a:p.write_bytes(b'G'*417792)
 elif a[0]=='-f' and fault=='late_staged':
  p=pathlib.Path('cpapp-b.bin');p.write_bytes(b'X'+p.read_bytes()[1:])
 elif a[0]=='-m':
  b=bytearray(p.read_bytes());d=pathlib.Path(a[1]).read_bytes();s=int(a[2]);b[s:s+len(d)]=d;p.write_bytes(b)
elif n=='ud_read_lsi_type.elf':sys.exit(3)
elif n=='ud_get_boot_device.elf':sys.exit(1)
elif n=='ud_get_data_from_mis.elf':print('0x00000200' if a[-1]=='t_add' else '0x00000600')
elif n=='ud_cp_firmup.elf':
 if a[0]=='-i':sys.exit(1)
 if a[0]=='-t':pathlib.Path(a[1]).write_bytes(b'T'*1536)
 if fault=='verify' and a[0]=='-y':
  print('MP_CMD_NOR_WRITE_VERIFY NG. result[7]');sys.exit(255)
''');driver.chmod(0o755)
        for name in ('mount','umount','cp','md5sum','bksb.elf','ud_nor_pformat.elf',
                     'ud_read_lsi_type.elf','ud_get_boot_ext.elf','ud_get_boot_device.elf',
                     'ud_get_data_from_mis.elf','ud_spicom_boot.elf','ud_init_spi_port_setting.elf',
                     'spicom.elf','ud_cp_firmup.elf','insmod','ud_pd_firmup.elf','ud_reboot.elf'):(fake/name).symlink_to(driver)
        sync = fake / 'sync'
        sync.write_text('#!/bin/sh\nprintf "sync \\n" >> "$MOCK_ROOT/commands"\n')
        sync.chmod(0o755)
        dd = fake / 'dd'
        dd.write_text("""#!/bin/sh
for argument do
    case "${argument%%=*}" in
        bs|count|seek|skip|if|of|status) ;;
        *) exit 1 ;;
    esac
done
exec /bin/dd "$@"
""")
        dd.chmod(0o755)
        text=Path(w.__file__).with_name('nor_rebuild_runner.sh.in').read_text()
        text=re.sub(r'(?<![A-Za-z0-9_])/(tmp_updater|ramdsk|setting|norq|cp|usr/kmod)(?=/|\b)',
                    lambda m:str(root/m[1]),text)
        for name in ('nflasha','mem','pcidma'):text=text.replace('/dev/'+name,str(root/'dev'/name))
        text=text.replace('/proc/mounts',str(root/'mounts')).replace('test -c ','test -e ')
        # Keep evidence commands in their real camera namespace, not the much
        # longer temporary host namespace used to emulate filesystem effects.
        text=text.replace('LAST_COMMAND="$*"', 'LAST_COMMAND=$(echo "$*" | /usr/bin/sed "s#$MOCK_ROOT##g")')
        text=text.replace('park() { while true; do sleep 60; done; }','park() { exit 0; }')
        text='\n'.join(f'PATH={fake}:/usr/bin:/bin:/usr/sbin:/sbin' if x.startswith('PATH=') else x for x in text.splitlines())
        script=root/'runner.sh';script.write_text(text+'\n')
        env={**os.environ,'MOCK_ROOT':str(root),'MOCK_FAULT':fault,'MOCK_FAIL_AT':str(fail_at)}
        subprocess.run(['sh',str(script)],env=env,capture_output=True,check=True,timeout=120)
        box=(root/'mailbox').read_bytes();return box,(root/'commands').read_text(),script,env

    def test_original_native_write_order_verify_and_companion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);box,commands,script,env=self.run_runner(root)
            head=fields(box[:4096])
            self.assertEqual(head['state'],'NATIVE_REBOOT_INTENT')
            self.assertEqual(head['exit_trigger_verified'],'true')
            self.assertEqual((root/'dev/nflasha2').read_bytes(),b'P'*512+b'O'*512)
            self.assertEqual(head['array_verified'],'true');self.assertEqual(head['companion_verified'],'true')
            self.assertEqual(len(box),w.BOX_SIZE)
            destructive=[line.split('ud_cp_firmup.elf ',1)[1] for line in commands.splitlines()
                         if line.startswith('ud_cp_firmup.elf -e ') or line.startswith(('ud_cp_firmup.elf -f ','ud_cp_firmup.elf -y '))]
            self.assertEqual([x for x in destructive if x.startswith('-e ')],
                             ['-e 2 -a 0 -s 9','-e 2 -a 9 -s 26','-e 2 -a 64 -s 33',
                              '-e 2 -a 96 -s 5','-e 3 -a 32 -s 2'])
            self.assertEqual(len(destructive),13)
            self.assertEqual([x.split()[0] for x in destructive if not x.startswith('-e ')],
                             ['-f','-y','-y','-y','-f','-f','-f','-f'])
            self.assertNotIn('uc_portctl.elf',commands)
            self.assertTrue(commands.rstrip().endswith('ud_reboot.elf'))
            self.assertIn('ud_pd_firmup.elf b',commands)
            self.assertNotIn('mount --bind',commands)
            self.assertEqual(hashlib.md5(box[135168:135168+417792]).hexdigest(),head['companion_md5'])
            # Consumed job may mount/read its mailbox, but cannot rerun native work.
            (root/'tmp_updater/norw_p1/NORW.DAT').unlink()
            subprocess.run(['sh',str(script)],env=env,capture_output=True,check=True,timeout=10)
            later=(root/'commands').read_text()[len(commands):]
            self.assertNotIn('ud_cp_firmup.elf',later)
            self.assertNotIn('uc_portctl.elf',later)

    def test_bootstrap_failure_preserves_cause_and_never_starts_an_erase(self):
        with tempfile.TemporaryDirectory() as tmp:
            box,commands,_,_=self.run_runner(Path(tmp),fault='bootstrap')
            head=fields(box[:4096])
            self.assertEqual(head['state'],'FAILED_BEFORE_ARRAY_WRITE')
            self.assertEqual(head['native_exit_status'],'255')
            self.assertIn('ud_spicom_boot.elf',head['native_command'])
            self.assertNotIn('ud_cp_firmup.elf -e ',commands)

    def test_exit_preimage_mismatch_cannot_overwrite_unowned_bytes_or_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);box,commands,_,_=self.run_runner(root,fault='exit_preimage')
            head=fields(box[:4096])
            self.assertEqual(head['state'],'EXIT_FAILED')
            self.assertEqual(head['array_verified'],'true')
            self.assertEqual(head['companion_verified'],'true')
            self.assertEqual(head['exit_trigger_verified'],'false')
            self.assertIn('exit_trigger_preimage_mismatch',head['error'])
            self.assertEqual((root/'dev/nflasha2').read_bytes(),b'P'*512+b'X'+b'T'*511)
            self.assertNotIn('ud_reboot.elf',commands)
            self.assertNotIn('ud_pd_firmup.elf',commands)

    def test_native_verify_failure_cannot_signal_or_claim_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            box,commands,_,_=self.run_runner(Path(tmp),fault='verify')
            head=fields(box[:4096]);self.assertEqual(head['state'],'ARRAY_WRITE_OUTCOME_UNKNOWN')
            self.assertEqual(head['native_exit_status'],'255');self.assertEqual(head['array_verified'],'false')
            self.assertNotIn('uc_portctl.elf -c 1 ',commands)
            self.assertNotIn('/cp/nor_ldr.img',commands)

    def test_same_length_staged_corruption_stops_before_first_erase(self):
        for fault in ('staged','late_staged'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                box,commands,_,_=self.run_runner(Path(tmp),fault=fault)
                head=fields(box[:4096])
                self.assertEqual(head['state'],'FAILED_BEFORE_ARRAY_WRITE')
                self.assertEqual(head['array_started'],'false')
                self.assertIn('md5sum',head['native_command'])
                self.assertNotIn('ud_cp_firmup.elf -e ',commands)
                self.assertNotIn('ud_cp_firmup.elf -f ',commands)
                self.assertNotIn('ud_cp_firmup.elf -y ',commands)

    def test_companion_mismatch_is_separate_from_verified_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            box,commands,_,_=self.run_runner(Path(tmp),fault='companion')
            head=fields(box[:4096]);self.assertEqual(head['state'],'COMPANION_FAILED')
            self.assertEqual(head['array_verified'],'true');self.assertEqual(head['companion_verified'],'false')
            self.assertNotIn('uc_portctl.elf -c 1 ',commands)

    def test_sony_extra_erase_unit_and_array_boundaries(self):
        self.assertEqual(w.erase_extent(0x200000,65535,65536)['count'],1)
        self.assertEqual(w.erase_extent(0x200000,65536,65536)['count'],2)
        self.assertEqual(w.erase_extent(0x200000,65537,65536)['count'],2)
        with self.assertRaises(ValueError):w.erase_extent(0x3ff0000,65536,65536)
        with self.assertRaises(ValueError):w.erase_extent(1,4096,4096)

if __name__=='__main__':unittest.main()
