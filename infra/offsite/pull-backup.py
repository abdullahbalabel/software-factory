"""Off-site copy of the factory's nightly restic backup.

Runs on the owner's Windows PC (Task Scheduler, daily) and mirrors the restic repository of the factory VM into
D:/factory-backup/restic over SSH (alias `factory-core`). Restic files are immutable and, except `config`, named by the
SHA-256 of their content, so the mirror fetches only new files, verifies each by its name, and removes files the VM
pruned. The copy stays encrypted with the repository password, which is kept separately on this PC.

Usage: python pull-backup.py [--dest D:/factory-backup/restic]
Exit code 0 when the mirror matches the VM; non-zero on any error (Task Scheduler shows it).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

HOST = "factory-core"
REMOTE = "/srv/factory-backup/restic"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", "-n", HOST]


def remote(cmd: str) -> bytes:
    result = subprocess.run(SSH + [cmd], capture_output=True, timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(f"remote command failed ({result.returncode}): {result.stderr.decode(errors='replace')[-400:]}")
    return result.stdout


def remote_files() -> dict[str, int]:
    out = remote(f"sudo find {REMOTE} -type f -not -path '{REMOTE}/locks/*' -printf '%P\\t%s\\n'")
    files: dict[str, int] = {}
    for line in out.decode().splitlines():
        rel, size = line.rsplit("\t", 1)
        files[rel] = int(size)
    return files


def named_by_hash(rel: str) -> bool:
    return rel.split("/", 1)[0] in ("data", "index", "snapshots", "keys")


def fetch(rel: str, dest: Path, size: int) -> None:
    data = remote(f"sudo cat {REMOTE}/{rel}")
    if len(data) != size:
        raise RuntimeError(f"{rel}: got {len(data)} bytes, expected {size}")
    if named_by_hash(rel) and hashlib.sha256(data).hexdigest() != Path(rel).name:
        raise RuntimeError(f"{rel}: content does not match its name")
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def report(status: str) -> None:
    """Leave the result on the VM (/var/lib/factory-offsite/last-pull) for the supervisor's daily digest."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", HOST,
                        "sudo install -d -m 755 /var/lib/factory-offsite && "
                        "sudo tee /var/lib/factory-offsite/last-pull >/dev/null && "
                        "sudo chmod 644 /var/lib/factory-offsite/last-pull"],
                       input=f"{stamp} {status}\n".encode(), capture_output=True, timeout=120)
    except Exception:  # the copy itself is what matters; the digest then shows the previous report
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", default="D:/factory-backup/restic")
    dest = Path(parser.parse_args().dest)
    dest.mkdir(parents=True, exist_ok=True)
    lock = dest.parent / "pull-backup.lock"
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 7200:
            raise RuntimeError("another copy is running (pull-backup.lock)")
        lock.unlink()
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(lock_fd)
    try:
        return pull(dest)
    finally:
        lock.unlink(missing_ok=True)


def pull(dest: Path) -> int:
    started = time.time()
    wanted = remote_files()
    if "config" not in wanted:
        raise RuntimeError("remote repository has no config file")
    have = {str(p.relative_to(dest)).replace("\\", "/"): p.stat().st_size for p in dest.rglob("*") if p.is_file()}
    fetched = 0
    for rel, size in sorted(wanted.items()):
        if have.get(rel) != size or (rel == "config"):
            fetch(rel, dest, size)
            fetched += 1
    removed = 0
    for rel in sorted(set(have) - set(wanted)):
        (dest / rel).unlink()
        removed += 1
    for rel, size in wanted.items():
        if (dest / rel).stat().st_size != size:
            raise RuntimeError(f"{rel}: size mismatch after copy")
    total = sum(wanted.values())
    line = (f"{time.strftime('%Y-%m-%dT%H:%M:%S')} ok files={len(wanted)} bytes={total} fetched={fetched} "
            f"removed={removed} seconds={time.time() - started:.0f}")
    print(line)
    with open(dest.parent / "pull-backup.log", "a", encoding="utf-8") as log:
        log.write(line + "\n")
    report(line.split(" ", 1)[1])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # report and fail visibly in Task Scheduler
        message = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} FAILED {error}"
        print(message, file=sys.stderr)
        Path("D:/factory-backup").mkdir(parents=True, exist_ok=True)
        with open("D:/factory-backup/pull-backup.log", "a", encoding="utf-8") as log:
            log.write(message + "\n")
        if "another copy is running" not in str(error):
            report(f"FAILED {error}"[:300])
        sys.exit(1)
