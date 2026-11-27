"""Explicit qualification entry/cleanup using the existing p1/p2 transaction.

No NOR array operations, automatic RAM entry or new USB protocol. Device calls
occur only for install/cleanup CLI actions, under the App's existing worker lock.
"""
from contextlib import contextmanager
import argparse
import fcntl
import json
import os
from pathlib import Path

from . import nor_qualification as q, write_transaction as tx
from .card_connection import APP_ROOT
from .stock_trigger import _verify_sdm2
from .transport import FastbootTransport


def _capture_identity(job):
    q.need(job.get('source_kind') == 'capture', 'device seam requires explicit capture-source job')
    q.need(len(job['source_manifests']) == 1, 'capture job needs exactly one source receipt')
    receipt_path = Path(job['source_manifests'][0]['path'])
    sources, _ = q.capture_pins(receipt_path)
    q.need(sources == job['source_partitions'], 'job differs from source receipt')
    receipt = json.loads(receipt_path.read_text())
    sector0 = receipt['files']['nflasha']
    q.check_pin(sector0)
    q.need(sector0['bytes'] == 512, 'capture sector0 size differs')
    _verify_sdm2(Path(sector0['path']).read_bytes())
    p8 = sources['nflasha8']
    return [{'region':'nflasha','bytes':512,'sha256':sector0['sha256']},
            {'region':'nflasha8','bytes':p8['bytes'],'sha256':p8['sha256']}], Path(sector0['path'])


def prepare_install(job_path):
    job_path = Path(job_path).resolve()
    job = q.load_job(job_path)
    identity, sector0 = _capture_identity(job)
    return tx.prepare(job_path.parent/'install-transaction', kind='nor_qualification_entry',
        regions=[{'name':name,'original':Path(job['source_partitions'][name]['path']),
                  'target':Path(job['candidates'][name]['path'])} for name in ('nflasha1','nflasha2')],
        identity=identity,
        input_files=[job_path, sector0] + [Path(x['path']) for x in job['source_manifests']],
        reboot=True, metadata={'nor_job_id':job['job_id'],'nor_array_writes':False,
                               'cleanup_source':'existing_read_capture'})


def prepare_cleanup(job_path, plan_path):
    job_path, plan_path = Path(job_path).resolve(), Path(plan_path).resolve()
    job = q.load_job(job_path)
    plan = json.loads(plan_path.read_text())
    expected = q.cleanup_plan(job_path, plan['captured']['nflasha1']['path'],
                             plan['captured']['nflasha2']['path'])
    q.need(plan == expected, 'cleanup plan differs from validated temporary captures')
    identity, sector0 = _capture_identity(job)
    return tx.prepare(job_path.parent/'cleanup-transaction', kind='nor_qualification_cleanup',
        regions=[{'name':name,'original':Path(plan['captured'][name]['path']),
                  'target':Path(job['source_partitions'][name]['path'])} for name in ('nflasha1','nflasha2')],
        identity=identity,
        input_files=[job_path, plan_path, sector0] + [Path(x['path']) for x in job['source_manifests']],
        reboot=False, metadata={'nor_job_id':job['job_id'],'cleanup_targets':['nflasha1','nflasha2'],
                                'nfa8_restore':False})


@contextmanager
def _exclusive_transport():
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    fd = os.open(APP_ROOT/'camera-worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Attach only. A missing/stale session never resets a potentially active job.
        with FastbootTransport() as transport:
            yield transport
    finally:
        os.close(fd)


def verify_live_sources(transport, folder, pins):
    """Whole-image source equality, before any entry/cleanup write in this session."""
    for name in ('nflasha1','nflasha2'):
        pin = pins[name]
        q.need(transport.region_size(name) == pin['bytes'], f'live size changed: {name}')
        output = Path(folder)/f'{name}.strict-source.bin'
        transport.read_region(name, output, length=pin['bytes'])
        q.need(output.stat().st_size == pin['bytes'] and tx.digest(output) == pin['sha256'],
               f'live {name} differs from pinned source; no qualification write admitted')


def apply(job_path, *, cleanup=False, progress=None):
    job_path = Path(job_path).resolve()
    job = q.load_job(job_path)
    identity, _ = _capture_identity(job)
    stage = job_path.parent/('cleanup-transaction' if cleanup else 'install-transaction')
    manifest, state = tx.load(stage)
    q.need(state['status'] == 'READY' and state['epoch'] == 0 and
           state['write_may_have_occurred'] is False and state['reboot_may_have_occurred'] is False,
           'single qualification attempt already consumed; inspect its evidence before recovery')
    names = ('nflasha1','nflasha2')
    q.need([r['name'] for r in manifest['regions']] == list(names) and
           manifest['identity'] == identity and manifest['reboot'] is (not cleanup) and
           manifest['metadata']['nor_job_id'] == job['job_id'], 'qualification transaction binding changed')
    expected_targets = job['source_partitions'] if cleanup else job['candidates']
    pins = {}
    for region in manifest['regions']:
        name = region['name']
        q.need(Path(region['target']) == Path(expected_targets[name]['path']) and
               region['target_sha256'] == expected_targets[name]['sha256'], 'qualification target changed')
        pins[name] = {'path':region['original'], 'bytes':region['bytes'], 'sha256':region['original_sha256']}
        if not cleanup:
            q.need(pins[name] == job['source_partitions'][name], 'install source changed')
    if cleanup:
        # Cleanup originals must still satisfy exact job mailbox-only differences.
        q.validate_temporary(job, pins['nflasha1']['path'], pins['nflasha2']['path'])
    def validator(transport, epoch_dir):
        q.load_job(job_path)
        verify_live_sources(transport, epoch_dir, pins)
    return tx.apply(stage, transport_factory=_exclusive_transport, validator=validator, progress=progress)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare-install','install','prepare-cleanup','cleanup'))
    parser.add_argument('job', type=Path)
    parser.add_argument('--cleanup-plan', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare-install':
        result = prepare_install(args.job)
    elif args.action == 'prepare-cleanup':
        q.need(args.cleanup_plan is not None, 'cleanup plan required')
        result = prepare_cleanup(args.job, args.cleanup_plan)
    else:
        result = apply(args.job, cleanup=args.action == 'cleanup',
                       progress=lambda row:print(json.dumps(row), flush=True))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
