"""Rebuild ``fastapi-template.tar.gz`` from GitHub (needs the network; tests never run this).

    uv run python packages/engine/tests/fixtures/build_fastapi_template.py

Downloads the pinned commit's tarball, replaces the PNG screenshots with a 1x1 PNG (to keep the
fixture small; still binary, same names) and writes the tree back out deterministically: sorted
entries, owner 0, mtime 0, gzip without a timestamp. Everything else is byte-for-byte the commit.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import tarfile
import urllib.request
from pathlib import Path

REPOSITORY = "fastapi/full-stack-fastapi-template"
COMMIT = "b1125d8839482cb7bbae603ddc6d0081bf87532e"
PREFIX = "fastapi-template"
OUT = Path(__file__).with_name("fastapi-template.tar.gz")
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def main() -> None:
    url = f"https://codeload.github.com/{REPOSITORY}/tar.gz/{COMMIT}"
    with urllib.request.urlopen(url, timeout=60) as response:
        source = response.read()
    print(f"source sha256 {hashlib.sha256(source).hexdigest()}")
    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.type == tarfile.XGLTYPE or "/" not in member.name:
                continue  # the pax global header (holds the commit id) and the top directory
            data = None
            if member.isfile():
                extracted = tar.extractfile(member)
                assert extracted is not None
                data = extracted.read()
                if member.name.endswith(".png"):
                    data = TINY_PNG
                    member.size = len(data)
            member.name = f"{PREFIX}/{member.name.split('/', 1)[1]}"
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mtime = 0
            members.append((member, data))
    members.sort(key=lambda item: item[0].name)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as out:
        for member, data in members:
            out.addfile(member, io.BytesIO(data) if data is not None else None)
    OUT.write_bytes(gzip.compress(buffer.getvalue(), compresslevel=9, mtime=0))
    print(f"wrote {OUT.name}: {OUT.stat().st_size} bytes, {len(members)} entries")


if __name__ == "__main__":
    main()
