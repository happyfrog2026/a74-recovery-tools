"""Synthetic target-fragment plan and backend contracts; no device access."""
import hashlib
import json
from pathlib import Path
import tempfile
import struct
import unittest
from threading import Event
from unittest.mock import patch
from recovery_tool import scoped_restore_plan as plan
from recovery_tool import task_backend as backend
from recovery_tool import snapshot, write_transaction as tx
from recovery_tool.updater_restore import Fragment, replay
from test_scoped_restore_plan import p0_fixture
from test_write_transaction import FakeCamera

MODE = plan.FRAGMENT_VALIDATION_MODE

def replace_raw(folder, region, data):
    spec = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    for suffix in ('raw', 'repeat'):
        (folder / f'{region}.{suffix}.bin').write_bytes(data)
    p = folder / 'verified.json'
    verified = json.loads(p.read_text()); verified[region] = spec
    p.write_text(json.dumps(verified))
    p = folder / 'capture-receipt.json'
    receipt = json.loads(p.read_text())
    for kind, suffix in (('first','raw'),('second','repeat')):
        receipt['reads'][region][kind] = {'file': f'{region}.{suffix}.bin', **spec}
    p.write_text(json.dumps(receipt))
    p = folder / 'decrypted/manifest.json'
    manifest = json.loads(p.read_text()); manifest['source_snapshot'] = verified
    p.write_text(json.dumps(manifest))

class FragmentRestorePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.current = self.root/'current'; self.target = self.root/'target'
        p0_fixture(self.current, changed_slots=(8,15)); p0_fixture(self.target)

    def build(self):
        return plan.build_plan(self.current,self.target,profile='capability',validation_mode=MODE)

    def test_both_boot_copies_differ_and_only_planned_differences_are_included(self):
        for region in ('nflashaB0','nflashaB1'):
            data=bytearray((self.current/f'{region}.raw.bin').read_bytes()); data[512+3]=7
            replace_raw(self.current,region,data)
        result=self.build()
        self.assertEqual([row['region'] for row in result['partitions']],['nflasha8','nflasha15','nflashaB1','nflashaB0'])
        self.assertEqual([row['changed_sectors'] for row in result['partitions']],[1,1,1,1])
        self.assertEqual(plan.capability_identity(self.current,self.target,result),
                         [{'region':'nflasha','bytes':512,'sha256':result['sdm2_sector0_sha256']}])
        self.assertFalse(result['fully_matches_snapshot_after_write'])

    def test_header_difference_is_skipped_without_discarding_writable_partition_plan(self):
        data=bytearray((self.current/'nflasha.raw.bin').read_bytes()); data[28]=1
        replace_raw(self.current,'nflasha',data)
        result=self.build()
        self.assertEqual(len(result['partitions']),2)
        self.assertEqual(result['skipped_regions'][0]['name'],'nflasha')
        self.assertFalse(result['skipped_regions'][0]['header_sector_same'])

    def test_backend_pins_fragment_validation_metadata(self):
        job=self.root/'new'
        inputs={'current_snapshot':str(self.current),'target_snapshot':str(self.target),'restore_profile':'capability'}
        backend.Backend(transport_factory=lambda: self.fail('camera opened')).prepare('scoped_restore',inputs,job)
        manifest=json.loads((job/'manifest.json').read_text())
        self.assertEqual(manifest['metadata']['validation_mode'],MODE)
        self.assertEqual(manifest['metadata']['offline_plan']['validation_mode'],MODE)
        self.assertEqual(len(manifest['identity']),1)

    def test_partial_result_describes_fragments_and_never_whole_device_equality(self):
        result_plan=self.build()
        rows=[{'region':'nflasha8','status':'verified'}, {'region':'nflasha15','status':'failed'}]
        result=backend._restore_verified_result(self.root,result_plan,plan_digest='p',
                                                state={'status':'PARTIAL','fragment_results':rows})
        self.assertEqual(result['status'],'PARTIAL')
        self.assertEqual(result['planned_fragments'],2)
        self.assertEqual(result['verified_fragments'],1)
        self.assertEqual(result['failed_fragments'],1)
        self.assertEqual(result['restored_regions'],['nflasha8'])
        self.assertFalse(result['fully_matches_snapshot'])

    def test_unknown_layout_raw_only_is_explicit_partial_not_false_complete(self):
        for folder in (self.current, self.target):
            raw=bytearray((folder/'nflasha.raw.bin').read_bytes()); raw[:8]=b'82462.01'
            replace_raw(folder,'nflasha',raw)
            path=folder/'decrypted/manifest.json'; manifest=json.loads(path.read_text())
            manifest.update(partitions=[], decryption_status='UNSUPPORTED', decryption_error='unknown SDM2 version')
            path.write_text(json.dumps(manifest))
        result_plan=self.build()
        self.assertEqual(result_plan['partitions'],[])
        self.assertEqual(result_plan['skipped_regions'][0]['name'],'nflasha')
        result=backend._restore_verified_result(self.root,result_plan,plan_digest='p')
        self.assertEqual(result['status'],'PARTIAL')
        self.assertFalse(result['fully_matches_snapshot'])

    def test_no_user_capture_can_still_plan_boot_with_product_only_identity(self):
        raw=bytearray((self.current/'nflashaB1.raw.bin').read_bytes());raw[512]=9
        replace_raw(self.current,'nflashaB1',raw)
        vpath=self.current/'verified.json'; verified=json.loads(vpath.read_text());verified.pop('nflasha')
        vpath.write_text(json.dumps(verified))
        path=self.current/'capture-receipt.json';receipt=json.loads(path.read_text());receipt['reads'].pop('nflasha')
        receipt['capture_regions']={name:row['bytes'] for name,row in verified.items()};path.write_text(json.dumps(receipt))
        path=self.current/'decrypted/manifest.json';manifest=json.loads(path.read_text())
        manifest.update(source_snapshot=verified,partitions=[],decryption_status='UNSUPPORTED',decryption_error='no user capture')
        path.write_text(json.dumps(manifest))
        result=self.build()
        self.assertEqual([row['region'] for row in result['partitions']],['nflashaB1'])
        self.assertEqual(plan.capability_identity(self.current,self.target,result),[])

    def test_live_logical_mapping_can_move_but_raw_table_is_not_claimed_restored(self):
        raw=bytearray((self.target/'nflasha.raw.bin').read_bytes())
        struct.pack_into('<I',raw,32+7*16,5);struct.pack_into('<I',raw,32+14*16,1)
        replace_raw(self.target,'nflasha',raw)
        path=self.target/'decrypted/manifest.json';manifest=json.loads(path.read_text())
        for row in manifest['partitions']:
            row['start_sector']=5 if row['file'].startswith('nflasha08') else 1
        path.write_text(json.dumps(manifest))
        result=self.build()
        self.assertEqual([row['region'] for row in result['partitions']],['nflasha8','nflasha15'])
        self.assertEqual(result['partitions'][0]['start_sector'],1)
        self.assertEqual(result['partitions'][0]['target_start_sector'],5)
        self.assertEqual(result['skipped_regions'][0]['name'],'nflasha')

    def test_slot_31_pins_second_mapping_sector_before_write(self):
        for folder in (self.current,self.target):
            raw=bytearray((folder/'nflasha.raw.bin').read_bytes())
            struct.pack_into('<I',raw,8,31)
            entry=bytes(raw[32+14*16:32+15*16])
            raw[32+14*16:32+15*16]=b'\0'*16
            raw[32+30*16:32+31*16]=entry
            replace_raw(folder,'nflasha',raw)
            path=folder/'decrypted/manifest.json'; manifest=json.loads(path.read_text())
            row=next(row for row in manifest['partitions'] if row['file'].startswith('nflasha15'))
            old=folder/'decrypted'/row['file']
            row['file']='nflasha31.decrypted.bin'
            old.rename(folder/'decrypted'/row['file'])
            path.write_text(json.dumps(manifest))
        result=self.build()
        self.assertEqual([row['region'] for row in result['partitions']],['nflasha8','nflasha31'])
        identity=plan.capability_identity(self.current,self.target,result)
        self.assertEqual(identity[0]['bytes'],1024)
        changed=bytearray((self.current/'nflasha.raw.bin').read_bytes())
        changed[520]^=1
        self.assertNotEqual(hashlib.sha256(changed[:1024]).hexdigest(),identity[0]['sha256'])

    def test_unsupported_type_is_skipped_while_other_partition_remains_writable(self):
        for folder in (self.current,self.target):
            raw=bytearray((folder/'nflasha.raw.bin').read_bytes());struct.pack_into('<I',raw,32+7*16+8,3)
            replace_raw(folder,'nflasha',raw)
            path=folder/'decrypted/manifest.json';manifest=json.loads(path.read_text());manifest['partitions'][0]['partition_type']=3
            path.write_text(json.dumps(manifest))
        result=self.build()
        self.assertEqual([row['region'] for row in result['partitions']],['nflasha15'])
        self.assertEqual(result['skipped_regions'][0]['reason'],'unsupported_or_changed_partition_type')

    def test_fresh_backend_real_fragment_engine_completes_despite_live_drift(self):
        self.run_fresh_backend(fail_first=False)

    def test_fresh_backend_partial_keeps_later_fragments_and_validates_saved_evidence(self):
        self.run_fresh_backend(fail_first=True)

    def run_fresh_backend(self, *, fail_first):
        job=self.root/'fresh-job'; camera=FakeCamera(b'',b''); engine=backend.Backend(transport_factory=lambda:camera)
        with patch.object(backend.decrypt_snapshot,'verify_snapshot'):
            engine.prepare('scoped_restore',{'target_snapshot':str(self.target)},job)
        self.assertEqual(json.loads((job/'task.json').read_text())['validation_mode'],MODE)
        events=[]
        class HostOnly:
            def run_host(self,fn): return fn(Event(),events.append)
        def capture(folder,**kwargs):
            self.assertEqual(kwargs['profile'],'restore_current')
            p0_fixture(folder,changed_slots=(8,15))
            for name in ('nflashaB0','nflashaB1'):
                data=bytearray((folder/f'{name}.raw.bin').read_bytes()); data[512]=3
                replace_raw(folder,name,data)
            verified=json.loads((folder/'verified.json').read_text());verified.pop('cp-nor')
            (folder/'verified.json').write_text(json.dumps(verified))
            receipt=json.loads((folder/'capture-receipt.json').read_text());receipt['reads'].pop('cp-nor')
            receipt.update(capture_profile=snapshot.RESTORE_CURRENT_PROFILE,not_captured_regions=['cp-nor'])
            (folder/'capture-receipt.json').write_text(json.dumps(receipt))
            path=folder/'decrypted/manifest.json';decoded=json.loads(path.read_text());decoded['source_snapshot']=verified
            path.write_text(json.dumps(decoded))
            camera.images.update({name:(folder/f'{name}.raw.bin').read_bytes() for name in verified})
            camera.images.update({f"nflasha{int(Path(row['file']).stem[7:9])}":(folder/'decrypted'/row['file']).read_bytes()
                                  for row in decoded['partitions']})
            changed=bytearray(camera.images['nflasha8']); changed[512:1024]=b'Z'*512
            camera.images['nflasha8']=bytes(changed) # live differs from B after the capture.
            return receipt
        writes=[]
        def flash(transport,name,payload,sectors,**kwargs):
            journal=json.loads((job/'scoped-stage/journal.json').read_text())
            self.assertEqual(journal['status'],'WRITE_INFLIGHT')
            self.assertEqual(journal['inflight']['region'],name)
            writes.append(name)
            if not (fail_first and name=='nflasha8'):
                transport.images[name]=replay(transport.images[name],[Fragment(0,sectors,payload)])
        with patch.object(backend.snapshot,'capture',side_effect=capture), \
             patch.object(backend.decrypt_snapshot,'decrypt'), \
             patch.object(backend.decrypt_snapshot,'verify_snapshot'), \
             patch.object(engine,'_restore_idle_session',return_value=HostOnly()), \
             patch.object(tx,'_write_response',side_effect=flash):
            result=engine.restore_run(job, progress=events.append)
            self.assertEqual(result['status'],'PARTIAL' if fail_first else 'VERIFIED',result)
            self.assertEqual(result['planned_fragments'],4)
            self.assertEqual(result['verified_fragments'],3 if fail_first else 4)
            self.assertEqual(result['failed_fragments'],1 if fail_first else 0)
            summaries=[event for event in events if event['stage']=='restore_plan_summary']
            self.assertEqual(len(summaries),1)
            self.assertEqual(summaries[0]['planned_fragments'],4)
            self.assertEqual(summaries[0]['planned_bytes'],4*512)
            self.assertFalse(result['fully_matches_snapshot'])
            self.assertEqual(writes,['nflasha8']*(2 if fail_first else 1)+['nflasha15','nflashaB1','nflashaB0'])
            reopened=engine.verify(job)
            self.assertEqual(reopened['status'],result['status'])
            journal=json.loads((job/'scoped-stage/journal.json').read_text())
            saved=job/'scoped-stage'/journal['fragment_results'][-1]['readback_path']
            saved.write_bytes(b'broken')
            self.assertEqual(engine.verify(job)['status'],'INCOMPLETE')
