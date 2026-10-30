"""Build the installable source-only archive, without dependencies or config."""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

FILES = (
    "__init__.py",
    "load.py",
    "model.py",
    "model_v2.py",
    "sync.py",
    "sync_v2.py",
    "README.md",
    "LICENSE",
)


def build(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name in sorted(FILES):
            data = (source / name).read_bytes().replace(b"\r\n", b"\n")
            info = zipfile.ZipInfo("edhq/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "dist/edhq-edmc.zip")
    args = parser.parse_args()
    digest = build(root, args.output.resolve())
    print(f"{args.output.resolve()}\nSHA256 {digest}")


if __name__ == "__main__":
    main()
