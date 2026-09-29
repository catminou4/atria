"""Durable key store: atomic JSONL append, dedupe on key hash, exports.

Key material is never logged — all logging goes through mask_key().
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


def key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def mask_key(api_key: str) -> str:
    if len(api_key) <= 11:
        return "****"
    return f"{api_key[:7]}…{api_key[-4:]}"


class KeyStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        # Make the directory entry durable for the fresh file.
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _hashes(self, fh) -> set[str]:
        fh.seek(0)
        hashes = set()
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            h = rec.get("key_hash")
            if h:
                hashes.add(h)
        return hashes

    def append(
        self,
        *,
        key_id: str,
        api_key: str,
        email: str,
        run_id: str,
        meta: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Atomic append of one JSON object per line. Returns (inserted, record)."""
        record = {
            "key_id": key_id,
            "api_key": api_key,
            "key_hash": key_hash(api_key),
            "email": email,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_id": run_id,
            "meta": meta or {},
        }
        with open(self.path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                if record["key_hash"] in self._hashes(fh):
                    return False, record
                fh.seek(0, os.SEEK_END)
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True, record

    def records(self) -> list[dict[str, Any]]:
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def masked_list(self) -> list[dict[str, Any]]:
        return [
            {
                "key_id": r.get("key_id"),
                "masked": mask_key(r.get("api_key", "")),
                "email": r.get("email"),
                "created_at": r.get("created_at"),
                "run_id": r.get("run_id"),
            }
            for r in self.records()
        ]

    def export_txt(self, out_path: str | Path) -> int:
        recs = self.records()
        tmp = Path(out_path).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(r["api_key"] + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out_path)
        return len(recs)

    def export_env(self, out_path: str | Path, prefix: str = "ATRIA_API_KEY") -> int:
        recs = self.records()
        tmp = Path(out_path).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for i, r in enumerate(recs):
                fh.write(f"{prefix}_{i}={r['api_key']}\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out_path)
        return len(recs)
