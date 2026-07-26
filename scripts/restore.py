#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import tarfile

from backup import default_data_dir, restore_backup


def main() -> None:
    """恢复前要求显式确认停服，并支持 Docker 命名卷无法重命名挂载点的场景。"""
    parser = argparse.ArgumentParser(description="Restore an AnyDatas single-server backup.")
    parser.add_argument("archive", type=Path, help="Path to an anydatas-backup-*.tar.gz archive")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--force", action="store_true", help="Confirm that the service has been stopped")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Keep the data directory mount point and replace only its contents",
    )
    parser.add_argument("--owner-uid", type=int, help="Recursively set restored file owner UID")
    parser.add_argument("--owner-gid", type=int, help="Recursively set restored file owner GID")
    args = parser.parse_args()
    try:
        restored_data_dir = restore_backup(
            args.archive,
            args.data_dir,
            force=args.force,
            in_place=args.in_place,
            owner_uid=args.owner_uid,
            owner_gid=args.owner_gid,
        )
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.error(str(exc))
    print(restored_data_dir)


if __name__ == "__main__":
    main()
