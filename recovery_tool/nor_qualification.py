"""Offline Sony-p1 NOR qualification builder and evidence validator.

No transport or device executor. Build only from a verified modern snapshot;
cleanup references that snapshot, never another prechange/rollback image.
Requires PySquashfsImage and mksquashfs only for the build operation.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import subprocess
import tarfile
import tempfile
from uuid import uuid4

from .stock_fat import Fat
from .stock_sd import all_files, STOCK_UPDATER_INITRD_SHA256

SCHEMA = "sony-nor-qualification-v1"
BOX_SIZE = 65536
READ_ADDRESSES = (0, 0x40000, 0x200000, 0x1000000, 0x3fff000)
STAGES = ("PREPARED", "ENTERED", "IMAGE_PREPARED", "CP_BOOT_STARTED", "CP_READY",
          "READ_STARTED", "READ_COMPLETE", "FAILED")
P1_PINS = {
    "boot/initrd.img": STOCK_UPDATER_INITRD_SHA256,
    "boot/vmlinux.bin": "838f5ff30cc9d3d3c99d8db3098e16a2dff204b370adfbbee2e58ab78756c106",
    "boot/cxd90057.dtb": "87abb2b72c3d373cf8f0779f8981a6e3f6780b54d08862d61a252248ebcf36ca",
    "boot/kemco.txt": "eb184157afc8eaf2d9a3dc795e7b52057130cb25f7799bc72fd6c85cbb99c94a",
    "boot/loadaddr.txt": "8c7cc8eb65c40311172f4bf1dc591fe3bcbc1efe689ededdd225142139881452",
}
P8_PINS = {
    "DGU.bin": "39eeb6d572a060149194463507800ed66b866a20dceb8ed49b01a04eb6fb2ba7",
    "MIS.bin": "4cd3e9bc3a0a4aec4793bff77e7ca4dccdba8637212cd632cdc5ecbe550daf98",
    "loader_spi.bin": "314d372d0d2b9883c3b9dc04b40389edb3100e6d5ef529633cc427c3d0cab72e",
    "pcie_phy_fw.bin": "963669fdc92d57141fa41095ecd7c5a1b972162d2e699a702ebfc8b6c1ef64dc",
}
SONY_PINS = {
    "bin/ud_cp_firmup.elf": "ea896626a2503f9ab8b90c9506176ffbab7d3b8081a378734d93d7dd410f2c95",
    "bin/ud_spicom_boot.elf": "558f99bca3e3fb25513c642654d582b8589f681ee8632359bff39798da3e8939",
    "bin/ud_init_spi_port_setting.elf": "9f158b469cb9ae8ee2e23d34aab10766d6dedbc8d0a6e08650f44a36962f7649",
    "bin/spicom.elf": "83544c8f02e9a55c3ec45dc2a6f141cba8a707b5f16351516dc13c4e7906e9b1",
    "bin/ud_nor_pformat.elf": "065955c5e6f34f622c9d35490bc3bfc84e3daeb6de882cda1cc9a7b63a15f9da",
    "bin/bksb.elf": "c14f9fd70302d67f45ab50b3edc130b40d471f3415bc40a20e7a30d7b815ddb9",
    "bin/ud_get_data_from_mis.elf": "557029e21eac8b9eccfc410a37d44e2d08b881817b2c6ee01a96779ce832a222",
    "bin/ud_get_boot_ext.elf": "072f6ea804f7037546b0cdfc1d908f346715fd7ae4bdee9e931dad63d55a9e71",
    "bin/ud_get_boot_device.elf": "f9b48a5c02a9ccf025a85d0fd8b4ec539379dad58d621c2f3622326d45309e8c",
    "cp/udtrcp.bin": "2c04cf41c63decc8bc9e4ff0441570aaf25a7c50687051716bfdcae69af0ac25",
    "cp/tomco.txt": "d9401e6a7d0cdd85803e2a6823fd9dac0ff186910d82a5671f32fc0a37fc21f1",
}
LOADER_SHA = "afae0d0054e369924c224cc85b2c2222b0940635786d15b1c570406b495a4a41"


class QualificationError(ValueError):
    pass


def need(value, message):
    if not value:
        raise QualificationError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_pin(path):
    path = Path(path).resolve()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest}


def check_pin(pin):
    need(file_pin(pin["path"]) == pin, f"pinned input changed: {pin['path']}")


def save(path, value):
    path = Path(path)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def _fat_get(fs, c):
    off = fs.fat_start + (c * 3 // 2 if fs.bits == 12 else c * 2)
    value = struct.unpack_from("<H", fs.data, off)[0]
    return ((value >> 4 if c & 1 else value) & 0xfff) if fs.bits == 12 else value


def _fat_set(fs, c, value):
    for copy in range(fs.nfat):
        off = fs.fat_start + copy * fs.fat_size + (c * 3 // 2 if fs.bits == 12 else c * 2)
        if fs.bits == 12:
            old = struct.unpack_from("<H", fs.data, off)[0]
            value16 = ((old & 0x000f) | (value << 4)) if c & 1 else ((old & 0xf000) | value)
        else:
            value16 = value
        struct.pack_into("<H", fs.data, off, value16)


def read_root_file(fs, name):
    """Resolve genuine root VFAT long names, validating their short-name checksum."""
    aliases = {}
    pending = []
    for off in range(fs.root_start, fs.root_start + fs.roots * 32, 32):
        row = bytes(fs.data[off:off+32])
        if row[0] == 0:
            break
        if row[0] == 0xe5:
            pending = []
            continue
        if row[11] == 0x0f:
            pending.append(row)
            continue
        short = row[:8].decode('ascii').rstrip()
        ext = row[8:11].decode('ascii').rstrip()
        short += '.' + ext if ext else ''
        names = [short]
        if pending:
            count = len(pending)
            checksum = 0
            for byte in row[:11]:
                checksum = (((checksum & 1) << 7) + (checksum >> 1) + byte) & 255
            need([x[0] for x in pending] == [count | 0x40, *range(count-1, 0, -1)], 'invalid LFN sequence')
            need(all(x[13] == checksum and x[12] == 0 and x[26:28] == b'\0\0' for x in pending),
                 'invalid LFN checksum')
            raw = b''.join(x[1:11]+x[14:26]+x[28:32] for x in reversed(pending))
            units = [raw[i:i+2] for i in range(0, len(raw), 2)]
            if b'\0\0' in units:
                end = units.index(b'\0\0')
                need(all(x == b'\xff\xff' for x in units[end+1:]), 'invalid LFN padding')
                units = units[:end]
            names.append(b''.join(units).decode('utf-16le'))
        for candidate in names:
            key = candidate.casefold()
            need(key not in aliases or aliases[key] == short, 'ambiguous FAT filename')
            aliases[key] = short
        pending = []
    need(name.casefold() in aliases, f'missing FAT root file: {name}')
    return fs.read(aliases[name.casefold()])


def _put_file(fs, path, data, *, new_root=False):
    """Allocate only genuinely free clusters; preserve other files and physical tail."""
    if new_root:
        need('/' not in path and re.fullmatch(r"[A-Z0-9]{1,8}\.[A-Z0-9]{1,3}", path), "invalid new root name")
        need(path not in fs.entries(), "mailbox already exists")
        slots = range(fs.root_start, fs.root_start + fs.roots * 32, 32)
        entry = next((x for x in slots if fs.data[x:x+64] == bytes(64)), None)
        need(entry is not None and entry + 64 <= fs.data_start, "no clean root directory slot")
        first, chain = 0, []
    else:
        entry, first, _, _ = fs.locate(path)
        chain = fs.chain(first)
    count = (len(data) + fs.cluster_size - 1) // fs.cluster_size
    extra = max(0, count - len(chain))
    free = [c for c in range(2, fs.cluster_count + 2) if _fat_get(fs, c) == 0]
    need(len(free) >= extra, "insufficient FAT space")
    added = free[:extra]
    complete = chain + added
    for i, c in enumerate(complete):
        _fat_set(fs, c, complete[i+1] if i + 1 < len(complete) else (0xfff if fs.bits == 12 else 0xffff))
    if new_root:
        base, ext = path.split('.')
        row = bytearray(32)
        row[:11] = base.encode().ljust(8, b' ') + ext.encode().ljust(3, b' ')
        row[11] = 0x20
        fs.data[entry:entry+32] = row
    struct.pack_into("<H", fs.data, entry + 26, complete[0] if complete else 0)
    struct.pack_into("<I", fs.data, entry + 28, len(data))
    for i, c in enumerate(complete):
        chunk = data[i*fs.cluster_size:(i+1)*fs.cluster_size]
        if chunk:
            fs.data[fs.offset(c):fs.offset(c)+len(chunk)] = chunk
    need(fs.read(path) == data, "FAT replacement readback mismatch")
    return entry


def p2_candidate(original):
    fs = Fat(original)
    root = fs.entries().get("UPDATER")
    need(root and root[3] & 16, "missing updater directory")
    entries = fs.entries(root[1])
    need(not any(n.startswith("MODE") or n in ("UINCOMP", "CPUART") for n in entries),
         "snapshot contains updater entry/conflict flags")
    slots = [x for c in fs.chain(root[1]) for x in range(fs.offset(c), fs.offset(c)+fs.cluster_size, 32)]
    index = next((i for i, off in enumerate(slots) if fs.data[off] == 0), None)
    need(index is not None and index + 2 < len(slots), "no updater directory space")
    need(all(fs.data[x:x+32] == bytes(32) for x in slots[index:index+3]), "nonempty directory tail")
    for name, off in zip(("MODE", "MODE1"), slots[index:index+2]):
        row = bytearray(32)
        row[:11] = name.encode().ljust(11, b' ')
        row[11] = 0x20
        fs.data[off:off+32] = row
    before, after = all_files(Fat(original)), all_files(Fat(fs.bytes()))
    need(set(after) - set(before) == {"UPDATER/MODE", "UPDATER/MODE1"}, "unexpected trigger files")
    need(all(after[k] == v for k, v in before.items()), "p2 content changed")
    return fs.bytes()


def initial_mailbox(job_id):
    need(re.fullmatch(r"[0-9a-f]{32}", job_id), "invalid job id")
    head = (f"schema={SCHEMA}\njob_id={job_id}\nstate=PREPARED\nerror=none\n"
            "flash_type=-1\nnor_array_write_started=false\nreads=0\n").encode()
    return head.ljust(BOX_SIZE, b'\0')


def patch_init(data):
    anchor = b"#execute main procedure\nUdtrMain.sh &"
    need(data.count(anchor) == 1, "unknown init dispatch")
    return data.replace(anchor, b"# Temporary standalone NOR qualification; no updater fallthrough.\n"
                        b"exec /norq/runner.sh\nwhile true; do sleep 60; done\n" + anchor)


def _squash_inventory(image):
    result = {}
    for x in image:
        need(x.xattr == 0xffffffff, "extended attributes not supported by this exact profile")
        content = x.read_bytes() if x.is_file else None
        kind = ("file" if x.is_file else "dir" if x.is_dir else "link" if x.is_symlink else
                "char" if x.is_char_device else "block" if x.is_block_device else "fifo" if x.is_fifo else "unsupported")
        need(kind != "unsupported", "unsupported SquashFS inode")
        result[x.path] = (x.mode, x.uid, x.gid, x.time, kind,
                          x.readlink() if x.is_symlink else None,
                          (x.major, x.minor) if x.is_char_device or x.is_block_device else None,
                          sha(content) if content is not None else None)
    return result


def rebuild_initrd(original, additions, mksquashfs, *, init_transform=patch_init):
    from PySquashfsImage import SquashFsImage
    with SquashFsImage.from_bytes(original) as image:
        before = _squash_inventory(image)
        init = init_transform(image.select("/sbin/init").read_bytes())
        root = image.select("/")
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for x in image:
                if x.path == '/':
                    continue
                t = tarfile.TarInfo(x.path.lstrip('/'))
                t.mode, t.uid, t.gid, t.mtime = x.mode & 0o7777, x.uid, x.gid, x.time
                content = None
                if x.is_file:
                    content = init if x.path == '/sbin/init' else x.read_bytes()
                    t.size = len(content)
                elif x.is_dir:
                    t.type = tarfile.DIRTYPE
                elif x.is_symlink:
                    t.type, t.linkname = tarfile.SYMTYPE, x.readlink()
                elif x.is_fifo:
                    t.type = tarfile.FIFOTYPE
                else:
                    t.type = tarfile.CHRTYPE if x.is_char_device else tarfile.BLKTYPE
                    t.devmajor, t.devminor = x.major, x.minor
                archive.addfile(t, io.BytesIO(content) if content is not None else None)
            for name in ("norq", "norq/bin", "norq/cp"):
                t = tarfile.TarInfo(name); t.type = tarfile.DIRTYPE; t.mode = 0o755
                t.mtime = root.time; archive.addfile(t)
            for name, content in sorted(additions.items()):
                need(name.startswith('norq/') and name not in before, "invalid added path")
                t = tarfile.TarInfo(name); t.size = len(content); t.mtime = root.time
                t.mode = 0o755 if name.endswith(('.elf', '.sh')) else 0o644
                archive.addfile(t, io.BytesIO(content))
        with tempfile.TemporaryDirectory(prefix="norq-squash-") as tmp:
            target = Path(tmp) / "initrd.img"
            command = [str(mksquashfs), "-", str(target), "-tar", "-noappend", "-comp", "gzip",
                       "-b", str(struct.unpack_from('<I', original, 12)[0]), "-no-xattrs", "-numeric-owner",
                       "-processors", "1", "-no-progress", "-quiet", "-mkfs-time", str(root.time),
                       "-root-time", str(root.time), "-root-mode", oct(root.mode & 0o7777)[2:],
                       "-root-uid", str(root.uid), "-root-gid", str(root.gid)]
            command += ["-exports" if struct.unpack_from('<H', original, 24)[0] & 0x80 else "-no-exports"]
            subprocess.run(command, input=payload.getvalue(), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rebuilt = target.read_bytes()
        with SquashFsImage.from_bytes(rebuilt) as result:
            after = _squash_inventory(result)
            for path, attributes in before.items():
                expected = (*attributes[:-1], sha(init)) if path == '/sbin/init' else attributes
                need(after[path] == expected, f"SquashFS metadata/content drift: {path}")
            need(set(after)-set(before) == {'/'+x for x in additions} | {'/norq','/norq/bin','/norq/cp'},
                 "unexpected SquashFS files")
            for name, content in additions.items():
                need(result.select('/'+name).read_bytes() == content, "added file roundtrip failed")
    return rebuilt


def snapshot_pins(snapshot):
    from .decrypt_snapshot import verify_snapshot
    snapshot = Path(snapshot).resolve()
    verified = verify_snapshot(snapshot)
    need(verified.get('decryption_status') == 'SUPPORTED', "snapshot lacks decrypted input")
    manifest = json.loads((snapshot/'decrypted/manifest.json').read_text())
    rows = {r['slot']: r for r in manifest['partitions']}
    need({1,2,8} <= rows.keys(), "snapshot requires p1/p2/p8")
    pins = {}
    for slot in (1,2,8):
        row = rows[slot]
        path = snapshot/'decrypted'/row['file']
        pin = file_pin(path)
        need(pin['sha256'] == row['sha256'] and pin['bytes'] == row['bytes'], "snapshot row mismatch")
        pins[f'nflasha{slot}'] = pin
    manifests = [file_pin(snapshot/name) for name in ('verified.json','capture-receipt.json','decrypted/manifest.json')]
    return pins, manifests


CAPTURE_SIZES = {'nflasha1': 25100288, 'nflasha2': 20971520, 'nflasha8': 67108864}


def capture_pins(receipt_path):
    """Explicit qualification inputs, not a fabricated verified snapshot."""
    receipt_path = Path(receipt_path).resolve()
    receipt = json.loads(receipt_path.read_text())
    need(receipt.get('schema') == 'nor-qualification-input-read-v1' and
         receipt.get('status') == 'READ_COMPLETE' and
         receipt.get('persistent_write_commands_issued') is False,
         'capture receipt is not a completed read-only qualification capture')
    sources = {}
    for name, size in CAPTURE_SIZES.items():
        row = receipt.get('files', {}).get(name)
        need(isinstance(row, dict) and row.get('bytes') == size,
             f'capture requires complete {name}')
        check_pin(row)
        sources[name] = file_pin(row['path'])
    need(len({p['path'] for p in sources.values()}) == 3, 'capture source paths overlap')
    return sources, [file_pin(receipt_path)]


def build(snapshot, sony_body, loader_tar, outdir, *, mksquashfs='mksquashfs'):
    sources, manifests = snapshot_pins(snapshot)
    return _build(sources, manifests, sony_body, loader_tar, outdir,
                  source_kind='snapshot', mksquashfs=mksquashfs)


def build_capture(receipt, sony_body, loader_tar, outdir, *, mksquashfs='mksquashfs'):
    sources, manifests = capture_pins(receipt)
    return _build(sources, manifests, sony_body, loader_tar, outdir,
                  source_kind='capture', mksquashfs=mksquashfs)


def _build(sources, manifests, sony_body, loader_tar, outdir, *, source_kind, mksquashfs):
    outdir = Path(outdir).resolve()
    need(not outdir.exists(), "output directory already exists")
    p1, p2, p8 = (Path(sources[f'nflasha{s}']['path']).read_bytes() for s in (1,2,8))
    f1, f2, f8 = Fat(p1), Fat(p2), Fat(p8)
    for name, expected in P1_PINS.items():
        need(sha(f1.read(name)) == expected, f"unknown p1 profile: {name}")
    for name, expected in P8_PINS.items():
        need(sha(read_root_file(f8,name)) == expected, f"unknown p8 bootstrap profile: {name}")
    need(f8.read('nor_ldr.img')[:4] == b'EXBL', "invalid A nor_ldr.img")
    need(0 < len(f8.read('cpapp-b.bin')) <= 24*1024*1024, "invalid CPAPP size")
    job_id = uuid4().hex
    additions = {}
    for name, expected in SONY_PINS.items():
        data = (Path(sony_body)/name).read_bytes()
        need(sha(data) == expected, f"Sony artifact mismatch: {name}")
        additions['norq/'+name] = data
    loader = Path(loader_tar).read_bytes()
    need(sha(loader) == LOADER_SHA, "loader TAR mismatch")
    additions['norq/loader_nor.tar'] = loader
    additions['norq/runner.sh'] = Path(__file__).with_name('nor_qualification_runner.sh.in').read_bytes()
    additions['norq/job.conf'] = f"JOB_ID={job_id}\n".encode()
    md5rows = [(read_root_file(f8,n), '/cp/'+n) for n in (*P8_PINS, 'cpapp-b.bin')]
    md5rows.append((f2.read('Backup.bin'), '/setting/Backup.bin'))
    additions['norq/source-files.md5'] = ''.join(hashlib.md5(data).hexdigest()+'  '+name+'\n' for data,name in md5rows).encode()
    trigger = p2_candidate(p2)
    initrd = rebuild_initrd(f1.read('boot/initrd.img'), additions, mksquashfs)
    original_files = all_files(f1)
    checksum = f1.read('p1chksum')
    old_md5 = hashlib.md5(original_files['BOOT/INITRD.IMG']).hexdigest().encode()
    need(checksum.count(old_md5) == 1, "original p1chksum mismatch")
    _put_file(f1, 'boot/initrd.img', initrd)
    f1.replace('p1chksum', checksum.replace(old_md5, hashlib.md5(initrd).hexdigest().encode()))
    _put_file(f1, 'NORQ.DAT', initial_mailbox(job_id), new_root=True)
    candidate = f1.bytes()
    after = all_files(Fat(candidate))
    need(set(after)-set(original_files) == {'NORQ.DAT'}, "unexpected p1 file set")
    need(all(after[n] == v for n,v in original_files.items() if n not in ('BOOT/INITRD.IMG','P1CHKSUM')),
         "unrelated p1 file changed")
    need(candidate[f1.total:] == p1[f1.total:], "physical partition tail changed")
    for pin in [*sources.values(), *manifests]:
        check_pin(pin)
    outdir.mkdir(mode=0o700)
    for name, data in [('p1.candidate.bin',candidate),('p2.candidate.bin',trigger)]:
        path=outdir/name;path.write_bytes(data);path.chmod(0o600)
    result = {'schema':SCHEMA,'job_id':job_id,'profile':'sony602-stock-p1-bootstrap-v1',
              'status':'OFFLINE_BUILT','can_execute':False,'camera_io':False,
              'source_kind':source_kind,'source_use':'qualification_inputs_and_cleanup',
              'nor_array_write_supported':False,'nfa8_write_in_qualification':False,
              'later_rebuild_retains_nfa8_nor_ldr_update':True,
              'source_partitions':sources,'source_manifests':manifests,
              'candidates':{f'nflasha{s}':file_pin(outdir/f'p{s}.candidate.bin') for s in (1,2)},
              'runner_sha256':sha(additions['norq/runner.sh']), 'initrd_sha256':sha(initrd),
              'read_addresses':list(READ_ADDRESSES),'read_bytes':4096,
              'cleanup':{'targets':['nflasha1','nflasha2'],'source':('existing_selected_snapshot' if source_kind == 'snapshot' else 'existing_read_capture'),
                         'extra_rollback_copies':False,'completed':False},
              'required_next_action':'separately_reviewed_device_entry_then_parked_receipt_collection'}
    save(outdir/'job.json',result)
    return result


def load_job(path):
    path=Path(path).resolve(); job=json.loads(path.read_text())
    need(job.get('schema') == SCHEMA and re.fullmatch(r'[0-9a-f]{32}',job.get('job_id','')), 'invalid job')
    need(job.get('nor_array_write_supported') is False, 'unexpected write capability')
    for pin in [*job['source_partitions'].values(),*job['source_manifests'],*job['candidates'].values()]:
        check_pin(pin)
    return job


def validate_temporary(job, captured_p1, captured_p2):
    """No cleanup admission for unrelated live changes or another job's mailbox."""
    expected=Path(job['candidates']['nflasha1']['path']).read_bytes()
    actual=Path(captured_p1).read_bytes()
    need(len(actual)==len(expected),'temporary p1 size changed')
    fs=Fat(expected); entry,first,size,_=fs.locate('NORQ.DAT')
    allowed=[(fs.offset(c),fs.cluster_size) for c in fs.chain(first)]
    # FAT creation tenths/time/date, access date and write time/date only.
    # The creation subsecond byte is at +13; cluster fields remain excluded.
    allowed += [(entry+13,7),(entry+22,4)]
    normalized=bytearray(actual)
    # Linux fat_boot_sector.fat16.state (FAT12/16 offset 37): mounting rw
    # sets FAT_STATE_DIRTY (bit 0). A controlled interrupt can leave it set.
    # Admit that bit alone; every other BPB/state bit remains exact.
    if fs.bits in (12,16) and expected[37] == 0 and actual[37] == 1:
        normalized[37]=0
    for start,length in allowed: normalized[start:start+length]=expected[start:start+length]
    need(bytes(normalized)==expected,'unexpected non-job p1 changes; cleanup refused')
    need(Path(captured_p2).read_bytes()==Path(job['candidates']['nflasha2']['path']).read_bytes(),
         'unexpected non-job p2 changes; cleanup refused')
    box=Fat(actual).read('NORQ.DAT')
    header=box[:4096].split(b'\0',1)[0].decode('ascii')
    fields={}
    for line in header.splitlines():
        if not line:continue
        need('=' in line,'malformed mailbox')
        key,value=line.split('=',1);need(key not in fields,'duplicate mailbox field');fields[key]=value
    need(fields.get('schema')==SCHEMA and fields.get('job_id')==job['job_id'],'foreign mailbox')
    need(fields.get('state') in STAGES and fields.get('nor_array_write_started')=='false','invalid mailbox stage')
    count=int(fields.get('reads','-1')); need(0<=count<=len(READ_ADDRESSES),'invalid read count')
    if fields['state']=='READ_COMPLETE':
        need(count==len(READ_ADDRESSES) and fields.get('error')=='none' and fields.get('flash_type') in ('0','1'),
             'incomplete read receipt')
    for index in range(count):
        sample=box[(index+1)*4096:(index+2)*4096]
        need(fields.get(f'sample_{index}_md5')==hashlib.md5(sample).hexdigest(),'sample integrity mismatch')
    return fields,box


def volume_state_observation(job, captured_p1):
    expected=Path(job['candidates']['nflasha1']['path']).read_bytes()
    actual=Path(captured_p1).read_bytes()
    return {'offset':37,'candidate':expected[37],'captured':actual[37],
            'dirty_bit_changed':bool((expected[37]^actual[37]) & 1)}


def collect(job_path,captured_p1,captured_p2,outdir,*,fastboot_nor=None):
    job=load_job(job_path);fields,box=validate_temporary(job,captured_p1,captured_p2)
    outdir=Path(outdir).resolve();need(not outdir.exists(),'collection directory exists')
    result={'schema':SCHEMA,'job_id':job['job_id'],'job_manifest':file_pin(job_path),
            'stage':fields,'qualification_read_complete':fields['state']=='READ_COMPLETE',
            'nor_array_write_completed':False,'cleanup_completed':False,
            'captured':{'nflasha1':file_pin(captured_p1),'nflasha2':file_pin(captured_p2)},
            'fat_volume_state':volume_state_observation(job,captured_p1),
            'cross_reader_equal':None,'samples':[]}
    reference=None
    if fastboot_nor is not None:
        reference=Path(fastboot_nor);need(reference.stat().st_size==64*1024*1024,'NOR reference must be 64 MiB')
        result['nor_reference']=file_pin(reference)
    outdir.mkdir(mode=0o700)
    for i in range(int(fields['reads'])):
        data=box[(i+1)*4096:(i+2)*4096];p=outdir/f'read-{READ_ADDRESSES[i]:08x}.bin';p.write_bytes(data);p.chmod(0o600)
        row={'offset':READ_ADDRESSES[i],**file_pin(p)}
        if reference:
            with reference.open('rb') as stream:stream.seek(READ_ADDRESSES[i]);row['reference_equal']=stream.read(4096)==data
        result['samples'].append(row)
    if reference:
        result['cross_reader_equal']=(fields['state']=='READ_COMPLETE' and all(r['reference_equal'] for r in result['samples']))
        check_pin(result['nor_reference'])
    log=outdir/'native.log';log.write_bytes(box[32768:].rstrip(b'\0'));log.chmod(0o600)
    save(outdir/'collection.json',result)
    return result


def cleanup_plan(job_path,captured_p1,captured_p2):
    job=load_job(job_path);fields,_=validate_temporary(job,captured_p1,captured_p2)
    return {'schema':SCHEMA,'job_id':job['job_id'],'job_manifest':file_pin(job_path),
            'status':'OFFLINE_CLEANUP_PLAN','can_execute':False,'cleanup_completed':False,
            'observed_stage':fields['state'],
            'fat_volume_state':volume_state_observation(job,captured_p1),
            'captured':{'nflasha1':file_pin(captured_p1),'nflasha2':file_pin(captured_p2)},
            'restore':{n:job['source_partitions'][n] for n in ('nflasha1','nflasha2')},
            'nfa8_restore':False,'requires_parked_device_and_fresh_read_before_write':True}


def verify_cleanup(job_path,plan_path,p1_readback,p2_readback):
    job=load_job(job_path);plan=json.loads(Path(plan_path).read_text())
    need(plan.get('schema')==SCHEMA and plan.get('job_id')==job['job_id'],'cleanup plan job mismatch')
    need(plan.get('job_manifest')==file_pin(job_path),'cleanup plan binding changed')
    expected={n:job['source_partitions'][n] for n in ('nflasha1','nflasha2')}
    need(plan.get('restore')==expected,'cleanup restore target changed')
    for pin in plan['captured'].values():check_pin(pin)
    validate_temporary(job,plan['captured']['nflasha1']['path'],plan['captured']['nflasha2']['path'])
    proof={}
    for name,path in [('nflasha1',p1_readback),('nflasha2',p2_readback)]:
        need(not Path(path).samefile(expected[name]['path']), 'restore source is not an independent cleanup readback')
        pin=file_pin(path);need(all(pin[k]==expected[name][k] for k in ('bytes','sha256')),f'{name} cleanup readback mismatch')
        proof[name]=pin
    return {'schema':SCHEMA,'job_id':job['job_id'],'status':'CLEANUP_READBACK_VERIFIED',
            'cleanup_completed':True,'job_manifest':file_pin(job_path),
            'cleanup_plan':file_pin(plan_path),'readbacks':proof,
            'evidence_scope':'caller_supplied_captured_files_not_live_transport_attestation',
            'nor_array_write_completed':False,'nfa8_restored':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    build_p=sub.add_parser('build');build_p.add_argument('snapshot',type=Path);build_p.add_argument('sony_body',type=Path)
    build_p.add_argument('loader_tar',type=Path);build_p.add_argument('outdir',type=Path);build_p.add_argument('--mksquashfs',default='mksquashfs')
    capture_p=sub.add_parser('build-capture');capture_p.add_argument('receipt',type=Path)
    capture_p.add_argument('sony_body',type=Path);capture_p.add_argument('loader_tar',type=Path)
    capture_p.add_argument('outdir',type=Path);capture_p.add_argument('--mksquashfs',default='mksquashfs')
    for name in ('collect','cleanup-plan'):
        p=sub.add_parser(name);p.add_argument('job',type=Path);p.add_argument('p1',type=Path);p.add_argument('p2',type=Path)
        p.add_argument('output',type=Path)
        if name=='collect':p.add_argument('--fastboot-nor',type=Path)
    p=sub.add_parser('verify-cleanup');p.add_argument('job',type=Path);p.add_argument('plan',type=Path)
    p.add_argument('p1',type=Path);p.add_argument('p2',type=Path);p.add_argument('output',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.action=='build':result=build(args.snapshot,args.sony_body,args.loader_tar,args.outdir,mksquashfs=args.mksquashfs)
        elif args.action=='build-capture':result=build_capture(args.receipt,args.sony_body,args.loader_tar,args.outdir,mksquashfs=args.mksquashfs)
        elif args.action=='collect':result=collect(args.job,args.p1,args.p2,args.output,fastboot_nor=args.fastboot_nor)
        elif args.action=='cleanup-plan':result=cleanup_plan(args.job,args.p1,args.p2);save(args.output,result)
        else:result=verify_cleanup(args.job,args.plan,args.p1,args.p2);save(args.output,result)
        print(json.dumps(result,indent=2));return 0
    except (ValueError,RuntimeError,OSError,subprocess.SubprocessError) as exc:
        parser.exit(1,f'nor-qualification: {exc}\n')


if __name__=='__main__':
    raise SystemExit(main())
