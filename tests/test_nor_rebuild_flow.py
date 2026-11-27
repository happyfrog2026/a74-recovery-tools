"""App orchestration with real native evidence and synthetic camera transport."""
from contextlib import contextmanager
import json
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_tool import nor_rebuild_flow as flow, nor_qualification as q
from recovery_tool import card_connection
from recovery_tool.stock_fat import Fat
from recovery_tool.task_backend import Backend
from test_nor_rebuild_evidence import EvidenceFixture


class NativeAppFlow(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root/'nor-rebuild'
        self.folder.mkdir()
        self.fx = EvidenceFixture(self.folder)
        self.sector0 = self.fx.write('sector0.bin', bytes(512))
        p8 = Fat(self.fx.p8_source.read_bytes())
        p8.replace('nor_ldr.img', b'C'*417792)
        self.p8_actual = p8.bytes()
        self.phase = 'native'
        self.events = []
        self.prepared = []
        self.apply_count = 0
        self.normal_count = 0
        self.now = 0
        self.receipt = self.root/'nor-flow.json'
        owner = self

        class Transport:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def region_size(self,name): return owner.fx.job['source_partitions'][name]['bytes']
            def read_region(self,name,destination,**kwargs):
                if name == 'nflasha': data = bytes(512)
                elif name == 'nflasha8': data = owner.p8_actual
                elif name == 'nflasha2': data = owner.fx.p2_capture.read_bytes()
                else: data = (owner.fx.p1_source if owner.phase == 'clean' else owner.fx.p1_capture).read_bytes()
                Path(destination).write_bytes(data)

        class Backend:
            transport_factory = Transport
            def _transport(self,task,progress,**kwargs):
                owner.events.append(('bridge',kwargs))
                if not owner.observed: raise AssertionError('RAM entry before ordinary USB')
                return Transport()

        self.backend = Backend()
        self.observed = False

    def prepare(self,folder,**kwargs):
        Path(folder).mkdir()
        self.prepared.append(kwargs)

    def apply(self,folder,**kwargs):
        self.apply_count += 1
        self.events.append(('apply',Path(folder).name))
        if Path(folder).name == 'cleanup-transaction':
            kwargs['validator'](self.backend.transport_factory(),folder)
            self.phase = 'clean'
        return {'status':'REBOOT_ACKNOWLEDGED','reboot_may_have_occurred':True,'last_verified_utc':'now'}

    def normal_probe(self):
        self.normal_count += 1
        self.events.append(('probe',self.normal_count))
        if self.normal_count < 3: return None
        self.observed = True
        return {'status':'MSC'}

    def sleep(self,seconds): self.now += seconds

    def normal_return(self,**kwargs):
        self.events.append(('normal_return',None))
        self.assertEqual(self.phase,'clean')
        self.assertTrue(json.loads((self.folder/'cleanup-receipt.json').read_text())['cleanup_completed'])
        return {'normal_mode_status':'READY','current_mode':'MSC','reboot_sent':True}

    def run_flow(self, *, apply=None):
        with patch.object(flow,'_capture_identity',return_value=([],self.sector0)), \
             patch.object(flow.tx,'prepare',self.prepare), \
             patch.object(flow.tx,'apply',apply or self.apply):
            return flow.run_job(self.fx.job_path,{'inputs':{}},backend=self.backend,
                                receipt_path=self.receipt,normal_probe=self.normal_probe,
                                normal_return=self.normal_return,sleep=self.sleep,
                                monotonic=lambda:self.now)

    def test_windows_legacy_no_write_does_not_block_fastboot(self):
        directory = self.folder / 'install-transaction'
        directory.mkdir()
        journal = {'schema': 'write-transaction-state-v1', 'status': 'NO_WRITE',
                   'write_may_have_occurred': False, 'reboot_may_have_occurred': False,
                   'confirmed_fragments': 0}
        flow.tx.durable_json(directory / 'manifest.json',
                             {'schema': 'write-transaction-v1', 'kind': 'nor_rebuild_entry'})
        flow.tx.durable_json(directory / 'journal.json', journal)
        flow.tx.durable_json(self.receipt, {'status': 'PREPARED', 'writer_may_be_active': True})
        with patch.object(card_connection, 'TASK_REGISTRY', self.root / 'no-registry'):
            blockers = lambda: card_connection.native_writer_blockers(
                task_root=self.root / 'unused', extra_jobs=[self.root])
            self.assertEqual(blockers(), [])
            journal['reboot_may_have_occurred'] = True
            flow.tx.durable_json(directory / 'journal.json', journal)
            self.assertEqual(len(blockers()), 1)

    def test_nor_read_reconnect_only_retries_interrupted_region(self):
        from recovery_tool.transport import USBReadError

        reads = []
        events = []
        class Transport:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read_region(self, region, path, *, length):
                reads.append(region)
                if reads == ['nflasha', 'nflasha1']:
                    path.with_name(path.name + '.partial').write_bytes(b'partial')
                    raise USBReadError('lost read', OSError(5, 'I/O error'))
                path.write_bytes(bytes(length))
        specs = [('nflasha', 'sector0.bin', 512), ('nflasha1', 'p1.bin', 512)]
        observed = flow._read_regions(self.folder, specs, Transport, progress=events.append)
        self.assertEqual(reads, ['nflasha', 'nflasha1', 'nflasha1'])
        self.assertEqual(set(observed), {'nflasha', 'nflasha1'})
        self.assertEqual(events[0]['stage'], 'restore_nor_reconnect')
        self.assertEqual(len(list(self.folder.glob('.lost-*'))), 1)

    def test_observed_native_return_automatically_collects_and_cleans(self):
        status = self.run_flow()
        self.assertEqual(status['status'],'COMPLETE')
        self.assertFalse(status['writer_may_be_active'])
        self.assertEqual(self.apply_count,2)
        self.assertEqual(len([e for e in self.events if e[0]=='bridge']),1)
        self.assertEqual(len([e for e in self.events if e[0]=='normal_return']),1)
        self.assertLess(self.events.index(('probe',3)),next(i for i,e in enumerate(self.events) if e[0]=='bridge'))
        self.assertFalse((self.folder/'normal-view-confirmation.json').exists())
        cleanup = self.prepared[1]
        self.assertEqual([r['name'] for r in cleanup['regions']],['nflasha1'])
        self.assertEqual([r['region'] for r in cleanup['identity']],['nflasha','nflasha2','nflasha8'])
        # Repeated RPC inspects the real saved evidence, without a third write.
        result = flow._completed_result(self.folder,status)
        self.assertEqual(result['nor_status'],'VERIFIED')
        self.assertTrue(result['normal_return_handled'])
        self.assertEqual(self.apply_count,2)

    def test_estimate_expiry_only_updates_elapsed_and_keeps_observing(self):
        original = self.normal_probe
        def delayed():
            if self.now < 200:
                self.events.append(('waiting_without_reset',self.now))
                return None
            self.normal_count = 2
            return original()
        self.normal_probe = delayed
        status = self.run_flow()
        self.assertGreaterEqual(status['normal_usb']['elapsed_seconds'],200)
        self.assertEqual(self.apply_count,2)
        first_bridge = next(i for i,e in enumerate(self.events) if e[0]=='bridge')
        self.assertLess(self.events.index(('probe',3)),first_bridge)

    def test_lost_entry_reboot_ack_observes_without_reinstalling(self):
        def apply(folder,**kwargs):
            if Path(folder).name == 'install-transaction':
                self.apply_count += 1
                raise OSError('lost ACK')
            return self.apply(folder,**kwargs)
        state = {'status':'REBOOT_UNCERTAIN','reboot_may_have_occurred':True,'last_verified_utc':'now'}
        with patch.object(flow.tx,'load',return_value=({},state)):
            status = self.run_flow(apply=apply)
        self.assertEqual(status['status'],'COMPLETE')
        self.assertEqual(self.apply_count,2)

    def test_incomplete_native_receipt_never_cleans_or_reboots(self):
        self.fx.p1_capture.write_bytes(self.fx.p1_candidate.read_bytes())
        with self.assertRaises(ValueError): self.run_flow()
        self.assertEqual(self.apply_count,1)
        self.assertFalse(any(e[0]=='normal_return' for e in self.events))
        self.assertFalse((self.folder/'cleanup-transaction').exists())

    def test_cleanup_failure_preserves_verified_array_and_companion_prefix(self):
        def apply(folder,**kwargs):
            if Path(folder).name == 'cleanup-transaction':
                raise OSError('cleanup transport lost')
            return self.apply(folder,**kwargs)
        with self.assertRaisesRegex(OSError,'cleanup transport lost'):
            self.run_flow(apply=apply)
        saved=json.loads(self.receipt.read_text())
        self.assertEqual(saved['status'],'NATIVE_RESULTS_VERIFIED')
        self.assertTrue(saved['collection']['array_write_verified'])
        self.assertTrue(saved['collection']['companion_verified'])
        (self.root/'nor-admission.json').write_text(json.dumps({'supported':True}))
        result=flow.finish(self.root,{}, {'status':'VERIFIED'},backend=self.backend)
        self.assertTrue(result['nor_array_verified'])
        self.assertTrue(result['nor_companion_verified'])
        self.assertFalse(result['nor_cleanup_completed'])
        self.assertNotEqual(result['nor_status'],'VERIFIED')
        self.assertFalse(any(e[0]=='normal_return' for e in self.events))

    def test_unknown_entry_outcome_never_probes_or_enters_ram(self):
        state = {'status':'UNKNOWN_AFTER_WRITE','reboot_may_have_occurred':False,'last_verified_utc':None}
        with patch.object(flow.tx,'load',return_value=({},state)):
            with self.assertRaises(OSError):
                self.run_flow(apply=lambda *args,**kwargs: (_ for _ in ()).throw(OSError('write lost')))
        self.assertFalse(self.events)
        self.assertTrue(json.loads(self.receipt.read_text())['writer_may_be_active'])


class NativeWriterGuards(unittest.TestCase):
    def test_worker_never_repeats_native_owned_normal_return(self):
        from recovery_tool.task_worker import handle
        backend = Backend(transport_factory=lambda:self.fail('transport opened'))
        result = {'status':'VERIFIED','kind':'scoped_restore','normal_return_handled':True,
                  'nor_status':'VERIFIED','normal_mode_status':'READY'}
        with patch.object(Backend,'restore_run',return_value=result), \
             patch('recovery_tool.task_worker.return_normal',side_effect=AssertionError('extra reboot')), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            handle({'action':'restore_run','job_dir':'/unused'},backend)
        self.assertEqual(json.loads(output.getvalue())['result']['nor_status'],'VERIFIED')

    def test_persisted_active_writer_blocks_ram_entry_and_ignore_history_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            job=root/'job';job.mkdir()
            (job/'nor-flow.json').write_text(json.dumps({'writer_may_be_active':True}))
            registry=root/'registry.json';registry.write_text(json.dumps({'jobs':[str(job)]}))
            with patch.object(card_connection,'TASK_REGISTRY',registry):
                with self.assertRaisesRegex(RuntimeError,'native NOR writer'):
                    Backend(transport_factory=lambda:self.fail('opened transport'))._transport({'inputs':{}},None)
                result=card_connection.return_normal(journal_path=root/'normal.json',
                    task_root=root,ignore_historical_tasks=True,normal_probe=lambda:None,
                    transport_factory=lambda:self.fail('sent reset'))
            self.assertFalse(result['reboot_sent'])
            self.assertEqual(result['normal_mode_status'],'NO_WRITE')


if __name__ == '__main__': unittest.main()
