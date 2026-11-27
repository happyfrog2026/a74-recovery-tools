"""Offline restore/NOR finalizer contracts; device entry is forbidden."""
import copy
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from recovery_tool import nor_rebuild_flow as flow
from recovery_tool import scoped_restore_plan, task_backend as tb
from recovery_tool import write_transaction as tx


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def native_verified():
    return {
        'nor_status': 'VERIFIED', 'nor_executed': True,
        'nor_array_verified': True, 'nor_companion_verified': True,
        'nor_cleanup_completed': True, 'nor_skip_reason': None,
        'nor_receipt': 'synthetic-nor-flow.json', 'normal_return_handled': True,
        'normal_mode_status': 'READY', 'current_mode': 'MSC', 'reboot_sent': True,
    }


class LocalSession:
    def run_host(self, work):
        return work(Event(), lambda _event: None)


class RestoreFinalizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job = self.root/'job'
        self.job.mkdir()
        self.task = {'kind': 'scoped_restore', 'inputs': {}}
        self.backend = tb.Backend(transport_factory=lambda: self.fail('direct device access forbidden'))
        self.transport = patch.object(self.backend, '_transport',
                                      side_effect=AssertionError('bridge/device entry forbidden'))
        self.transport_mock = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.run_job_patch = patch.object(flow, 'run_job', side_effect=AssertionError('native writer forbidden'))
        self.run_job = self.run_job_patch.start()
        self.addCleanup(self.run_job_patch.stop)
        self.emmc = {
            'kind': 'scoped_restore', 'status': 'VERIFIED',
            'completion_scope': 'planned_changed_fragments',
            'summary': 'eMMC verified', 'fully_matches_snapshot': False,
            'skipped_regions': [
                {'name': 'cp-nor', 'reason': 'not_captured_read_only', 'changed': None},
                {'name': 'nflashaB0', 'reason': 'outside_reviewed_scope', 'changed': True},
            ],
        }

    def finish(self, result=None):
        return self.backend._finish_restore(self.job, self.task, copy.deepcopy(result or self.emmc))

    def assert_saved_result(self, result):
        self.assertEqual(json.loads((self.job/'restore-result.json').read_text()), result)
        self.transport_mock.assert_not_called()
        self.run_job.assert_not_called()

    def test_partial_emmc_never_starts_nor_or_requires_admission(self):
        emmc = {**self.emmc, 'status': 'PARTIAL', 'failed_fragments': 1}
        with patch.object(flow.nor_resources, 'resolve', side_effect=AssertionError('resource access forbidden')) as resolve:
            result = self.finish(emmc)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['emmc_status'], 'PARTIAL')
        self.assertEqual(result['nor_status'], 'NOT_STARTED')
        self.assertEqual(result['nor_skip_reason'], 'emmc_restore_not_fully_verified')
        self.assertFalse(result['nor_executed'])
        self.assertFalse(result['normal_return_handled'])
        self.assertEqual(result['failed_fragments'], 1)
        self.assertFalse((self.job/'nor-admission.json').exists())
        self.assertFalse((self.job/'nor-flow.json').exists())
        resolve.assert_not_called()
        self.assert_saved_result(result)

    def test_unsupported_profile_keeps_verified_emmc_and_precise_skip(self):
        save(self.job/'nor-admission.json', {'supported': False, 'reason': 'unknown CP lineage: MIS.bin'})
        with patch.object(flow.nor_resources, 'resolve', side_effect=AssertionError('unsupported resources forbidden')) as resolve:
            result = self.finish()
        self.assertEqual((result['status'], result['emmc_status'], result['nor_status']),
                         ('VERIFIED', 'VERIFIED', 'UNSUPPORTED'))
        self.assertFalse(result['nor_executed'])
        self.assertEqual(result['completion_scope'], 'planned_changed_fragments')
        self.assertTrue(result['success_with_skips'])
        self.assertFalse(result['fully_matches_snapshot'])
        self.assertEqual(result['skipped_regions'], [self.emmc['skipped_regions'][1], {
            'name': 'cp-nor', 'reason': 'native_rebuild_unsupported_profile',
            'detail': 'unknown CP lineage: MIS.bin'}])
        self.assertFalse((self.job/'nor-flow.json').exists())
        resolve.assert_not_called()
        self.assert_saved_result(result)

    def test_supported_verified_nor_filters_only_cp_nor_skips(self):
        original_skips = copy.deepcopy(self.emmc['skipped_regions'])
        with patch.object(flow, 'finish', return_value=native_verified()) as finish:
            result = self.finish()
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[:2], (self.job, self.task))
        self.assertEqual(finish.call_args.args[2], self.emmc)
        self.assertIs(finish.call_args.kwargs['backend'], self.backend)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertEqual(result['emmc_status'], 'VERIFIED')
        self.assertEqual(result['skipped_regions'], [original_skips[1]])
        self.assertEqual(self.emmc['skipped_regions'], original_skips)
        self.assertEqual(result['completion_scope'], 'planned_emmc_fragments_and_native_nor')
        self.assertTrue(result['normal_return_handled'])
        self.assertTrue(result['nor_array_verified'])
        self.assertTrue(result['nor_companion_verified'])
        self.assertTrue(result['nor_cleanup_completed'])
        self.assertTrue(result['success_with_skips'])
        self.assert_saved_result(result)

    def test_supported_success_without_other_skips_reports_no_skips(self):
        emmc = {**self.emmc, 'skipped_regions': [self.emmc['skipped_regions'][0]]}
        with patch.object(flow, 'finish', return_value=native_verified()):
            result = self.finish(emmc)
        self.assertEqual(result['skipped_regions'], [])
        self.assertFalse(result['success_with_skips'])
        self.assert_saved_result(result)

    def test_known_resource_failure_is_incomplete_without_losing_emmc_verification(self):
        for prepared_error in (True, False):
            with self.subTest(prepared_error=prepared_error):
                job = self.root/f'resource-failure-{prepared_error}'
                job.mkdir()
                admission = {'supported': True}
                if prepared_error:
                    admission['resource_error'] = 'bundled mksquashfs missing'
                save(job/'nor-admission.json', admission)
                with patch.object(flow.nor_resources, 'resolve',
                                  side_effect=FileNotFoundError('bundled mksquashfs missing')) as resolve:
                    result = self.backend._finish_restore(job, self.task, copy.deepcopy(self.emmc))
                self.assertEqual((result['status'], result['emmc_status'], result['nor_status']),
                                 ('INCOMPLETE', 'VERIFIED', 'INCOMPLETE'))
                self.assertFalse(result['nor_executed'])
                self.assertFalse(result['reboot_sent'])
                self.assertIn('bundled mksquashfs missing', result['nor_skip_reason'])
                self.assertEqual(result['skipped_regions'][-1]['reason'], 'native_rebuild_not_completed')
                self.assertEqual(resolve.call_count, 0 if prepared_error else 1)
                self.assertIn('bundled mksquashfs missing', json.loads((job/'nor-flow.json').read_text())['error'])
        self.transport_mock.assert_not_called()
        self.run_job.assert_not_called()

    def test_legacy_no_write_shows_nor_never_started(self):
        save(self.job/'nor-admission.json', {'supported': True})
        error = 'temporary file path is too long'
        save(self.job/'nor-flow.json', {'status': 'PREPARED',
                                       'writer_may_be_active': True, 'error': error})
        with patch.object(flow, 'native_entry_has_no_write', return_value=True):
            result = self.finish()
        self.assertEqual((result['status'], result['emmc_status'], result['nor_status']),
                         ('INCOMPLETE', 'VERIFIED', 'NOT_STARTED'))
        self.assertIn('did not start', result['summary'])
        self.assertEqual(result['nor_skip_reason'], error)
        self.run_job.assert_not_called()

    def test_prior_uncertain_attempt_is_unknown_and_owns_normal_return(self):
        save(self.job/'nor-admission.json', {'supported': True})
        prior = {'status': 'WAITING_FOR_NATIVE_NORMAL_RETURN', 'writer_may_be_active': True,
                 'entry': {'status': 'REBOOT_UNCERTAIN'}}
        save(self.job/'nor-flow.json', prior)
        with patch.object(flow.nor_resources, 'resolve', side_effect=AssertionError('replay forbidden')) as resolve:
            result = self.finish()
        self.assertEqual((result['status'], result['emmc_status'], result['nor_status']),
                         ('UNKNOWN_AFTER_WRITE', 'VERIFIED', 'UNKNOWN'))
        self.assertTrue(result['nor_executed'])
        self.assertTrue(result['normal_return_handled'])
        self.assertFalse(result['reboot_sent'])
        self.assertEqual(result['normal_mode_status'], 'NEEDS_USER_ACTION')
        self.assertEqual(result['nor'], prior)
        self.assertEqual(json.loads((self.job/'nor-flow.json').read_text()), prior)
        resolve.assert_not_called()
        self.assert_saved_result(result)

    def test_unexpected_flow_error_keeps_emmc_outcome_and_blocks_generic_return(self):
        with patch.object(flow, 'finish', side_effect=ValueError('invalid native receipt')):
            result = self.finish()
        self.assertEqual((result['status'], result['emmc_status']), ('INCOMPLETE', 'VERIFIED'))
        self.assertEqual(result['nor_status'], 'INCOMPLETE')
        self.assertTrue(result['normal_return_handled'])
        self.assertFalse(result['reboot_sent'])
        self.assertIn('invalid native receipt', result['nor_skip_reason'])
        self.assert_saved_result(result)

    def test_prior_complete_attempt_revalidates_saved_evidence_without_reinstalling(self):
        save(self.job/'nor-admission.json', {'supported': True})
        prior = {'status': 'COMPLETE', 'normal_return': {'normal_mode_status': 'READY',
                  'current_mode': 'MSC', 'reboot_sent': True}}
        save(self.job/'nor-flow.json', prior)
        proof = {key: True for key in ('array_write_verified', 'companion_verified',
                                      'host_p8_companion_and_other_files_verified',
                                      'exit_trigger_verified', 'native_reboot_requested')}
        with patch.object(flow.w, 'collect', return_value=proof) as collect, \
             patch.object(flow.w, 'verify_cleanup', return_value={'cleanup_completed': True}) as cleanup, \
             patch.object(flow.nor_resources, 'resolve', side_effect=AssertionError('reinstall forbidden')) as resolve:
            result = self.finish()
        collect.assert_called_once()
        cleanup.assert_called_once()
        self.assertEqual(result['nor_status'], 'VERIFIED')
        self.assertTrue(result['normal_return_handled'])
        self.assertEqual(result['normal_mode_status'], 'READY')
        resolve.assert_not_called()
        self.assert_saved_result(result)

    def test_prior_complete_attempt_with_changed_evidence_never_reinstalls(self):
        save(self.job/'nor-admission.json', {'supported': True})
        save(self.job/'nor-flow.json', {'status': 'COMPLETE',
                                      'normal_return': {'normal_mode_status': 'READY'}})
        proof = {key: True for key in ('array_write_verified', 'companion_verified',
                                      'host_p8_companion_and_other_files_verified',
                                      'exit_trigger_verified', 'native_reboot_requested')}
        proof['host_p8_companion_and_other_files_verified'] = False
        with patch.object(flow.w, 'collect', return_value=proof), \
             patch.object(flow.w, 'verify_cleanup', return_value={'cleanup_completed': True}), \
             patch.object(flow.nor_resources, 'resolve', side_effect=AssertionError('reinstall forbidden')) as resolve:
            result = self.finish()
        self.assertEqual((result['status'], result['emmc_status'], result['nor_status']),
                         ('INCOMPLETE', 'VERIFIED', 'INCOMPLETE'))
        self.assertIn('completion evidence changed', result['nor_skip_reason'])
        self.assertFalse(result['reboot_sent'])
        resolve.assert_not_called()
        self.assert_saved_result(result)


class RestoreRunFinalizerTests(unittest.TestCase):
    """Exercise all three real restore_run returns using local task receipts."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.backend = tb.Backend(transport_factory=lambda: self.fail('device access forbidden'))
        self.task_transport = patch.object(self.backend, '_transport',
                                           side_effect=AssertionError('bridge access forbidden'))
        self.transport_mock = self.task_transport.start()
        self.addCleanup(self.task_transport.stop)

    def fixture(self, branch, *, final_status='VERIFIED'):
        target = self.root/'target'
        save(target/'verified.json', {'synthetic': True})
        job = self.root/'restore'
        job.mkdir()
        task = {'kind': 'scoped_restore', 'validation_mode': scoped_restore_plan.FRAGMENT_VALIDATION_MODE,
                'inputs': {'target_snapshot': str(target), 'restore_profile': 'capability'},
                'target_snapshot_pins': {'verified.json': tx.digest(target/'verified.json')},
                'plan_digest': 'selected-target-plan'}
        save(job/'task.json', task)
        plan = {'partitions': [], 'skipped_regions': [{'name': 'cp-nor', 'reason': 'not_captured_read_only'}]}
        if branch == 'zero-diff':
            save(job/'current-snapshot/verified.json', {'synthetic': True})
            save(job/'current-snapshot/capture-receipt.json', {'status': 'VERIFIED'})
            save(job/'current-snapshot/decrypted/manifest.json', {'status': 'VERIFIED'})
            return job, target, plan, None, None
        plan['partitions'] = [{'region': 'nflasha2', 'fragments': [{'sectors': 1}]}]
        stage = job/'scoped-stage'
        manifest = {'metadata': {'offline_plan': plan}}
        save(stage/'manifest.json', manifest)
        save(stage/'task.json', {'kind': 'scoped_restore', 'plan_digest': tx.digest(stage/'manifest.json')})
        verified = {'status': final_status, 'fragment_results': [
            {'status': 'failed' if final_status == 'PARTIAL' else 'verified', 'region': 'nflasha2'}]}
        save(stage/'journal.json', verified if branch == 'resumed' else {'status': 'PREPARED'})
        return job, target, plan, manifest, verified

    def test_all_successful_emmc_paths_run_the_shared_native_finalizer(self):
        for branch in ('zero-diff', 'resumed', 'newly-applied'):
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as folder:
                self.root = Path(folder).resolve()
                job, target, plan, manifest, verified = self.fixture(branch)
                events = []
                def apply(_stage, **kwargs):
                    loaded, state = kwargs['host_load']()
                    self.assertEqual(loaded, manifest)
                    self.assertEqual(state['status'], 'PREPARED')
                    return verified
                with patch.object(flow, 'admission', return_value={'supported': True}) as admission, \
                     patch.object(flow, 'finish', return_value=native_verified()) as finish, \
                     patch.object(self.backend, '_restore_idle_session', return_value=LocalSession()), \
                     patch.object(tb.decrypt_snapshot, 'verify_snapshot'), \
                     patch.object(tb.scoped_restore_plan, 'build_plan', return_value=plan), \
                     patch.object(tb.tx, 'load', return_value=(manifest, verified if branch == 'resumed' else {'status': 'PREPARED'})) as load, \
                     patch.object(tb.tx, 'apply_with_reconnect', side_effect=apply) as applied, \
                     patch.object(tb, '_verify_saved_scoped_readbacks') as readbacks, \
                     patch.object(tb.snapshot, 'capture', side_effect=AssertionError('capture forbidden')):
                    result = self.backend.restore_run(job, events.append)
                admission.assert_called_once_with(target)
                self.assertEqual(events[0]["stage"], "snapshot_nor_profile")
                finish.assert_called_once()
                self.assertEqual(finish.call_args.args[0], job)
                self.assertEqual(finish.call_args.args[2]['status'], 'VERIFIED')
                self.assertEqual(result['emmc_status'], 'VERIFIED')
                self.assertEqual(result['nor_status'], 'VERIFIED')
                self.assertEqual(result['completion_scope'], 'planned_emmc_fragments_and_native_nor')
                self.assertEqual(result['skipped_regions'], [])
                self.assertEqual(applied.call_count, int(branch == 'newly-applied'))
                self.assertEqual(readbacks.call_count, int(branch != 'zero-diff'))
                self.assertEqual(load.call_count, int(branch != 'zero-diff'))
                self.assertEqual(json.loads((job/'restore-result.json').read_text()), result)
        self.transport_mock.assert_not_called()

    def test_resumed_partial_emmc_never_resolves_native_resources(self):
        job, target, plan, manifest, partial = self.fixture('resumed', final_status='PARTIAL')
        with patch.object(flow, 'admission', return_value={'supported': True}), \
             patch.object(flow.nor_resources, 'resolve', side_effect=AssertionError('NOR forbidden')) as resolve, \
             patch.object(flow, 'run_job', side_effect=AssertionError('native writer forbidden')) as run_job, \
             patch.object(tb.tx, 'load', return_value=(manifest, partial)), \
             patch.object(tb, '_verify_saved_scoped_readbacks'), \
             patch.object(tb.tx, 'apply_with_reconnect', side_effect=AssertionError('reapply forbidden')):
            result = self.backend.restore_run(job)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['emmc_status'], 'PARTIAL')
        self.assertEqual(result['nor_status'], 'NOT_STARTED')
        self.assertFalse(result['nor_executed'])
        self.assertEqual(result['failed_fragments'], 1)
        resolve.assert_not_called()
        run_job.assert_not_called()
        self.transport_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
