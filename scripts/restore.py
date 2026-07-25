#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import tarfile

from backup import default_data_dir, restore_backup


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore an AnyDatas single-server backup.")
    parser.add_argument("archive", type=Path, help="Path to an anydatas-backup-*.tar.gz archive")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--force", action="store_true", help="Confirm that the service has been stopped")
    args = parser.parse_args()
    try:
        restored_data_dir = restore_backup(args.archive, args.data_dir, force=args.force)
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.error(str(exc))
    print(restored_data_dir)


if __name__ == "__main__":
    main()
