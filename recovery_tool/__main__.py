"""Command line entry point for explicit recovery actions."""

import argparse
import json
from pathlib import Path
import sys

from .board_install import BoardInstallError, inspect, install


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m recovery_tool")
    topics = parser.add_subparsers(dest="topic", required=True)
    board = topics.add_parser("board", help="RP2040 bridge board")
    actions = board.add_subparsers(dest="action", required=True)
    for action in ("inspect", "install"):
        sub = actions.add_parser(action)
        sub.add_argument("--bootsel-volume", type=Path, help="explicit RP2040 BOOTSEL mount point")
        sub.add_argument("--port", help="explicit RP2040 CDC serial port")
        if action == "install":
            sub.add_argument("--uf2", type=Path, help="path to the exact pinned David UF2")
            sub.add_argument("--evidence-dir", type=Path, help="receipt output directory")
            sub.add_argument("--wait-seconds", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        if args.action == "inspect":
            output = inspect(volume=args.bootsel_volume, port=args.port)
        else:
            if args.wait_seconds <= 0:
                parser.error("--wait-seconds must be positive")
            output = install(uf2=args.uf2, volume=args.bootsel_volume,
                             port=args.port, evidence_dir=args.evidence_dir,
                             wait_seconds=args.wait_seconds)
    except (BoardInstallError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
