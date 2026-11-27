"""App-owned native NOR lifecycle; caller holds the existing worker lock.

Reuses the hardware-qualified one-shot transaction and native writer. There is
no confirmation file, second USB protocol, elapsed-time reset, or writer retry.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import tempfile
from uuid import uuid4

from . import nor_rebuild as w, nor_qualification as q, write_transaction as tx
from . import nor_resources
from .card_connection import native_entry_has_no_write, return_normal
from .nor_qualification_device import _capture_identity, verify_live_sources
from .transport import observe_normal_camera_usb


def now():
    return datetime.now(timezone.utc).isoformat()


def _read_regions(folder, specs, factory, *, progress):
    """Read each remaining region with a bounded USB retry before/after native execution."""
    captured = {}
    for region, name, size in specs:
        path = folder / name
        for attempt in range(4):
            try:
                with factory() as transport:
                    transport.read_region(region, path, length=size)
                pin = q.file_pin(path)
                q.need(pin['bytes'] == size, 'incomplete NOR source: ' + region)
                captured[region] = pin
                break
            except Exception as exc:
                if attempt == 3 or not tx._fragment_transport_unavailable(exc):
                    raise
                partial = path.with_name(path.name + '.partial')
                for incomplete in (path, partial):
                    if incomplete.exists():
                        incomplete.rename(incomplete.with_name('.lost-' + uuid4().hex[:12]))
                progress({'stage': 'restore_nor_reconnect',
                          'message': f'{region} read interrupted; reconnect attempt {attempt + 1}/3: {exc}',
                          'retry': attempt + 1, 'max_retries': 3})
    return captured


def run_job(job_path, task, *, backend, receipt_path, progress=None,
            normal_probe=observe_normal_camera_usb, normal_return=return_normal,
            sleep=time.sleep, monotonic=time.monotonic):
    progress = progress or (lambda event: None)
    transport_factory = backend.transport_factory
    managed = getattr(backend, 'backup_fastboot_state', None) is not None
    def reconnect():
        progress({'stage':'restore_nor_reconnect','message':'Reopening NOR restore Fastboot session'})
        with backend._transport(task, progress):
            pass
    def apply_entry(path, validator):
        options = dict(transport_factory=transport_factory, validator=validator, progress=progress)
        if managed:
            return tx.apply_with_reconnect(path, reconnect=reconnect, max_retries=3, **options)
        return tx.apply(path, **options)
    job_path=Path(job_path).resolve();folder=job_path.parent;job=w.load_job(job_path)
    q.need(job['completion_route']=='verified_trigger_restore_then_sony_native_reboot','wrong writer completion route')
    identity,sector0=_capture_identity(job)
    q.need(not (folder/'install-transaction').exists(),'single trial already prepared/consumed; inspect its evidence, do not replay')
    tx.prepare(folder/'install-transaction',kind='nor_rebuild_entry',
        regions=[{'name':name,'original':Path(job['source_partitions'][name]['path']),
                  'target':Path(job['candidates'][name]['path'])} for name in ('nflasha1','nflasha2')],
        identity=identity,input_files=[job_path,sector0]+[Path(p['path']) for p in job['source_manifests']],
        reboot=True,metadata={'nor_job_id':job['job_id'],'native_nor_writer':True})
    status={'schema':'nor-rebuild-live-trial-v1','job_id':job['job_id'],'started_utc':now(),
            'job_manifest':q.file_pin(job_path),'status':'PREPARED','estimated_seconds':180,
            'timer_reset_permitted':False,'cleanup_completed':False,'writer_may_be_active':False}
    def save():
        tx.durable_json(folder/'live-trial-receipt.json',status)
        tx.durable_json(receipt_path,status)
    try:
        status.update(writer_may_be_active=True);save()
        progress({'stage':'restore_nor_install','message':'Installing the single-use native rebuild entry'})
        def install_guard(t,epoch):
            w.load_job(job_path);verify_live_sources(t,epoch,job['source_partitions'])
        try:
            installed=apply_entry(folder/'install-transaction', install_guard)
        except BaseException:
            _,installed=tx.load(folder/'install-transaction')
            if not (installed.get('reboot_may_have_occurred') and installed.get('last_verified_utc')):raise
            progress({'stage':'entry_reboot_uncertain','message':'Entry may already be running; retaining the lock and observing without another reboot'})
        q.need(installed['status'] in ('REBOOT_ACKNOWLEDGED','REBOOT_UNCERTAIN'),
               'entry did not reach its one-reboot boundary')
        status.update(status='WAITING_FOR_NATIVE_NORMAL_RETURN',entry=installed);save()
        started=monotonic();last=-10
        while True:
            elapsed=int(monotonic()-started)
            if elapsed-last>=10:
                progress({'stage':'restore_nor_wait','message':'Waiting for the success-only ordinary Sony USB return',
                          'elapsed_seconds':elapsed,'estimated_seconds':180});last=elapsed
            try:ordinary=normal_probe()
            except Exception as exc:
                ordinary=None;progress({'stage':'normal_usb_probe','error':str(exc)})
            if ordinary and ordinary.get('status') in {'MSC','NORMAL_USB'}:break
            sleep(0.5)
        normal={'schema':'nor-rebuild-normal-usb-observation-v1','job_id':job['job_id'],
                'job_manifest':q.file_pin(job_path),'observed_utc':now(),'elapsed_seconds':elapsed,'usb':ordinary}
        tx.durable_json(folder/'native-normal-usb.json',normal)
        status.update(status='NATIVE_NORMAL_USB_OBSERVED',normal_usb=normal,writer_may_be_active=False);save()
        progress({'stage':'restore_nor_collect','message':'Native writer returned ordinary Sony USB; collecting its durable results'})
        # Only this new endpoint closes the autonomous execution boundary.
        # Elapsed time never authorizes RAM entry or reboot.
        after=folder/'after-job';after.mkdir(mode=0o700)
        specs = [('nflasha','sector0.bin',512),('nflasha1','p1.bin',25100288),
                 ('nflasha2','p2.bin',20971520),('nflasha8','p8.bin',67108864)]
        if managed:
            first_open = True
            def open_collection():
                nonlocal first_open
                force_bridge = first_open
                first_open = False
                return backend._transport(task, progress, force_bridge=force_bridge)
            captured = _read_regions(after, specs, open_collection, progress=progress)
        else:
            captured = {}
            with backend._transport(task,progress,force_bridge=True) as t:
                for region,name,size in specs:
                    t.read_region(region,after/name,length=size)
                    captured[region]=q.file_pin(after/name)
        for region, pin in captured.items():
            progress({'stage':'restore_nor_collect','message':f'Captured {region}',**pin})
        q.need(captured['nflasha']['sha256']==q.file_pin(sector0)['sha256'],'sector0 identity changed')
        collected=w.collect(job_path,after/'p1.bin',after/'p2.bin',folder/'collection',p8_path=after/'p8.bin')
        q.need(collected['array_write_verified'] and collected['companion_verified'] and
               collected['exit_trigger_verified'] and collected['native_reboot_requested'],
               'native completion receipt is incomplete; do not blanket-clean or replay')
        status.update(status='NATIVE_RESULTS_VERIFIED',collection=collected,
                      array_verified=True,companion_verified=True);save()
        plan=w.cleanup_plan(job_path,after/'p1.bin',after/'p2.bin');q.save(folder/'cleanup-plan.json',plan)
        q.need(set(plan['restore'])=={'nflasha1'},'successful cleanup must preserve current normal-boot p2')
        tx.prepare(folder/'cleanup-transaction',kind='nor_rebuild_entry_cleanup',
            regions=[{'name':'nflasha1','original':after/'p1.bin','target':Path(plan['restore']['nflasha1']['path'])}],
            identity=[{'region':n,'bytes':captured[n]['bytes'],'sha256':captured[n]['sha256']}
                      for n in ('nflasha','nflasha2','nflasha8')],
            input_files=[job_path,folder/'cleanup-plan.json',folder/'native-normal-usb.json'],
            reboot=False,metadata={'nor_job_id':job['job_id'],'nfa8_restore':False,'p2_preserved':True})
        def cleanup_guard(t,epoch):
            w.load_job(job_path);verify_live_sources(t,epoch,{n:captured[n] for n in ('nflasha1','nflasha2')})
        progress({'stage':'restore_nor_cleanup','message':'Restoring the temporary entry and independently verifying cleanup'})
        apply_entry(folder/'cleanup-transaction', cleanup_guard)
        out=folder/'after-cleanup';out.mkdir(mode=0o700)
        cleanup_specs = [(region,name,captured[region]['bytes'])
                         for region,name in [('nflasha1','p1.bin'),('nflasha2','p2.bin')]]
        if managed:
            _read_regions(out, cleanup_specs, lambda: backend._transport(task,progress),
                          progress=progress)
        else:
            with transport_factory() as t:
                for region,name,size in cleanup_specs:
                    t.read_region(region,out/name,length=size)
        cleaned=w.verify_cleanup(job_path,folder/'cleanup-plan.json',out/'p1.bin',out/'p2.bin')
        q.save(folder/'cleanup-receipt.json',cleaned)
        status.update(status='CLEANUP_VERIFIED',cleanup_completed=True,collection=collected);save()
        progress({'stage':'restore_nor_normal','message':'Returning the cleaned camera to ordinary Sony USB'})
        returned=normal_return(journal_path=folder/'normal-return-journal.json',transport_factory=transport_factory,
                               ignore_historical_tasks=True,once_only=True,normal_probe=normal_probe,progress=progress)
        tx.durable_json(folder/'normal-return-receipt.json',returned)
        status.update(status=('COMPLETE' if returned.get('normal_mode_status')=='READY'
                              else 'CLEANUP_VERIFIED_NORMAL_RETURN_UNCERTAIN'),
                      normal_return=returned,finished_utc=now());save()
        progress({'stage':'restore_nor_normal','message':'Native rebuild and entry cleanup verified',**status})
        return status
    except BaseException as exc:
        if status['status'] == 'PREPARED' and native_entry_has_no_write(folder.parent):
            status.update(status='ENTRY_NO_WRITE',writer_may_be_active=False)
        status.update(error=repr(exc),stopped_utc=now());save();raise


def _base(status, *, reason=None, receipt=None, executed=False):
    return {'nor_status': status, 'nor_executed': executed,
            'nor_array_verified': False, 'nor_companion_verified': False,
            'nor_cleanup_completed': False, 'nor_skip_reason': reason,
            'nor_receipt': str(receipt) if receipt else None,
            'normal_return_handled': False}


def admission(target):
    profile = nor_resources.snapshot_profile(target)
    result = {**profile, 'checked_utc': now()}
    if profile['supported']:
        try:
            resources = nor_resources.resolve(profile['updater_profile'])
            result['resource_pins'] = resources['pins']
        except Exception as exc:
            result['resource_error'] = str(exc)
    return result


def _verified_prefix(folder):
    """Preserve completed stages after later failure, based on real saved proof."""
    flags = {'nor_array_verified':False, 'nor_companion_verified':False,
             'nor_cleanup_completed':False}
    if not (folder/'collection/collection.json').is_file():
        return flags
    try:
        with tempfile.TemporaryDirectory(prefix='nor-prefix-') as temporary:
            collected = w.collect(folder/'job.json',folder/'after-job/p1.bin',folder/'after-job/p2.bin',
                                  Path(temporary)/'collection',p8_path=folder/'after-job/p8.bin')
        flags.update(nor_array_verified=collected['array_write_verified'],
                     nor_companion_verified=(collected['companion_verified'] and
                                              collected['host_p8_companion_and_other_files_verified']))
        if (folder/'cleanup-receipt.json').is_file():
            cleaned = w.verify_cleanup(folder/'job.json',folder/'cleanup-plan.json',
                                       folder/'after-cleanup/p1.bin',folder/'after-cleanup/p2.bin')
            flags['nor_cleanup_completed'] = cleaned['cleanup_completed']
    except (OSError, ValueError, RuntimeError, KeyError):
        # A damaged proof cannot become a successful stage merely from a flag.
        pass
    return flags


def _completed_result(folder, status):
    # A repeated restore RPC inspects its existing evidence; it never reinstalls.
    job_path = folder / 'job.json'
    with tempfile.TemporaryDirectory(prefix='nor-evidence-') as temporary:
        collected = w.collect(job_path, folder/'after-job/p1.bin', folder/'after-job/p2.bin',
                              Path(temporary)/'collection', p8_path=folder/'after-job/p8.bin')
    cleaned = w.verify_cleanup(job_path, folder/'cleanup-plan.json',
                               folder/'after-cleanup/p1.bin', folder/'after-cleanup/p2.bin')
    q.need(all(collected.get(key) is True for key in
               ('array_write_verified', 'companion_verified', 'host_p8_companion_and_other_files_verified',
                'exit_trigger_verified', 'native_reboot_requested')),
           'native rebuild completion evidence changed')
    q.need(cleaned.get('cleanup_completed') is True, 'native rebuild cleanup evidence changed')
    result = _base('VERIFIED', receipt=folder.parent/'nor-flow.json', executed=True)
    result.update(nor_array_verified=True, nor_companion_verified=True,
                  nor_cleanup_completed=True, normal_return_handled=True,
                  nor=status, **status['normal_return'])
    return result


def finish(job_dir, task, emmc, *, backend, progress=None):
    """Finalize a verified eMMC restore once, preserving its separate outcome."""
    job_dir = Path(job_dir)
    progress = progress or (lambda event: None)
    receipt = job_dir/'nor-flow.json'
    folder = job_dir/'nor-rebuild'
    if emmc['status'] != 'VERIFIED':
        return _base('NOT_STARTED', reason='emmc_restore_not_fully_verified', receipt=receipt)
    prepared = json.loads((job_dir/'nor-admission.json').read_text())
    if not prepared['supported']:
        return _base('UNSUPPORTED', reason=prepared.get('reason') or 'native_rebuild_unsupported_profile', receipt=receipt)
    entered = False
    try:
        if receipt.exists():
            prior = json.loads(receipt.read_text())
            if prior.get('status') == 'COMPLETE':
                return _completed_result(folder, prior)
            if prior.get('status') in {'PREPARED', 'ENTRY_NO_WRITE'} and native_entry_has_no_write(job_dir):
                result = _base('NOT_STARTED', reason=prior.get('error'), receipt=receipt)
                result.update(normal_return_handled=True, nor=prior, reboot_sent=False,
                              normal_mode_status='NEEDS_USER_ACTION', current_mode='UNKNOWN')
                return result
            # Any prior attempt is single-use, including uncertain reboot ACK.
            result = _base('UNKNOWN', reason='native_rebuild_attempt_already_started', receipt=receipt,
                           executed=bool(prior.get('writer_may_be_active') or prior.get('entry')))
            result.update(_verified_prefix(folder))
            result.update(normal_return_handled=True, nor=prior, reboot_sent=False,
                          normal_mode_status='NEEDS_USER_ACTION', current_mode='UNKNOWN')
            return result
        tx.durable_json(receipt, {'schema':'nor-rebuild-app-flow-v1', 'status':'PREPARING',
                                 'writer_may_be_active':False, 'started_utc':now()})
        q.need(not prepared.get('resource_error'), prepared.get('resource_error'))
        resources = nor_resources.resolve(prepared.get('updater_profile', nor_resources.DEFAULT_UPDATER))
        progress({'stage':'restore_nor_prepare', 'message':'Preparing native NOR rebuild from the restored eMMC',
                  'estimated_seconds':180, 'elapsed_seconds':0})
        capture = job_dir/'nor-input-capture'
        capture.mkdir(mode=0o700)
        read_receipt = {'schema':'nor-qualification-input-read-v1', 'status':'READ_INCOMPLETE',
                        'persistent_write_commands_issued':False, 'files':{},
                        'source':'current_emmc_after_verified_restore', 'started_utc':now()}
        tx.durable_json(capture/'capture-receipt.json', read_receipt)
        # This is a new capture after all eMMC writes, never the target backup
        # or the pre-restore snapshot. Reentry is allowed at this verified boundary.
        specs = [('nflasha','sector0.bin',512),('nflasha1','p1.bin',25100288),
                 ('nflasha2','p2.bin',20971520),('nflasha8','p8.bin',67108864)]
        if getattr(backend, 'backup_fastboot_state', None) is not None:
            factory = lambda: backend._transport(task, progress)
            read_receipt['files'] = _read_regions(capture, specs, factory, progress=progress)
            tx.durable_json(capture/'capture-receipt.json', read_receipt)
        else:
            with backend._transport(task, progress) as transport:
                q.need(transport.verify_product() == 'ILCE-7M4', 'NOR source product changed')
                for name, filename, size in specs:
                    transport.read_region(name,capture/filename,length=size)
                    pin = q.file_pin(capture/filename)
                    q.need(pin['bytes'] == size, 'incomplete current NOR source: '+name)
                    read_receipt['files'][name] = pin
                    tx.durable_json(capture/'capture-receipt.json', read_receipt)
        actual = nor_resources.inspect_profile(capture/'p1.bin',capture/'p8.bin')
        q.need(actual['supported'], 'current restored eMMC does not match admitted NOR profile: '+actual.get('reason',''))
        q.need(actual['updater_profile'] == prepared.get('updater_profile', actual['updater_profile']),
               'restored updater environment differs from selected snapshot')
        read_receipt.update(status='READ_COMPLETE',finished_utc=now())
        tx.durable_json(capture/'capture-receipt.json',read_receipt)
        sources, manifests = q.capture_pins(capture/'capture-receipt.json')
        w.build(sources, manifests, resources['sony_body'], resources['loader_tar'], folder,
                source_kind='capture', mksquashfs=str(resources['mksquashfs']))
        entered = True  # From here the flow owns all mode exits, including errors.
        status = run_job(folder/'job.json', task, backend=backend, receipt_path=receipt, progress=progress)
        if status['status'] != 'COMPLETE':
            result = _base('INCOMPLETE', reason='cleanup_verified_normal_return_uncertain',receipt=receipt,executed=True)
            result.update(nor_array_verified=True,nor_companion_verified=True,nor_cleanup_completed=True,
                          normal_return_handled=True,nor=status,**status['normal_return'])
            return result
        return _completed_result(folder,status)
    except Exception as exc:
        saved = json.loads(receipt.read_text()) if receipt.is_file() else {}
        active = bool(saved.get('writer_may_be_active'))
        saved.update(error=str(exc),stopped_utc=now())
        tx.durable_json(receipt,saved)
        result = _base('NOT_STARTED' if saved.get('status') == 'ENTRY_NO_WRITE' else
                       'UNKNOWN' if active else 'INCOMPLETE', reason=str(exc), receipt=receipt,
                       executed=bool(saved.get('entry') or active))
        result.update(_verified_prefix(folder))
        result.update(normal_return_handled=entered, nor=saved, reboot_sent=False,
                      normal_mode_status='NEEDS_USER_ACTION',current_mode='UNKNOWN')
        return result


def offline_smoke(capture_receipt, output_dir):
    """Frozen-resource build/parse check. No transport or camera APIs are used."""
    sources, manifests = q.capture_pins(capture_receipt)
    profile = nor_resources.inspect_profile(sources['nflasha1']['path'], sources['nflasha8']['path'])
    q.need(profile['supported'], profile.get('reason', 'unsupported NOR profile'))
    resources = nor_resources.resolve(profile['updater_profile'])
    output = Path(output_dir).resolve()
    w.build(sources, manifests, resources['sony_body'], resources['loader_tar'], output,
            source_kind='capture', mksquashfs=str(resources['mksquashfs']))
    job = w.load_job(output/'job.json')
    head, _ = w.validate_temporary(job, job['candidates']['nflasha1']['path'],
                                    job['candidates']['nflasha2']['path'])
    q.need(head['state'] == 'PREPARED', 'offline mailbox was not prepared')
    return {'kind':'nor_offline_smoke','status':'VERIFIED','camera_io':False,
            'native_nor_executed':False,'job_manifest':q.file_pin(output/'job.json'),
            'profile':job['profile'],'resource_pins':resources['pins'],
            'candidate_pins':job['candidates'], 'runner_sha256':
                q.sha(Path(w.__file__).with_name('nor_rebuild_runner.sh.in').read_bytes())}
