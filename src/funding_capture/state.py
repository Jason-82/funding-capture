"""JSON persistence for engine state (positions, forecaster, paper books)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path

from .data import instrument_from_dict, instrument_to_dict
from .models import Candidate, Construction, PairPosition


def position_to_dict(p: PairPosition) -> dict:
    d = asdict(p)
    c = p.candidate
    d["candidate"] = {
        "construction": c.construction.value,
        "asset": c.asset,
        "long": instrument_to_dict(c.long),
        "short": instrument_to_dict(c.short),
    }
    return d


def position_from_dict(d: dict) -> PairPosition:
    c = d["candidate"]
    cand = Candidate(
        Construction(c["construction"]),
        c["asset"],
        instrument_from_dict(c["long"]),
        instrument_from_dict(c["short"]),
    )
    return PairPosition(**{**d, "candidate": cand})


def save_json(path: str | Path, data: dict) -> None:
    """Atomic write so a crash mid-save never leaves a corrupt state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: another program is reading the file right now
            if attempt == 9:
                raise
            time.sleep(0.2)


def load_json(path: str | Path) -> dict | None:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class StateLocked(RuntimeError):
    """Another engine process already runs on this state directory."""


@contextmanager
def lock_state_dir(state_dir: str | Path) -> Iterator[None]:
    """Hold an OS lock on ``<state_dir>/engine.lock`` while the block runs.

    Two engines on one state file would each trade and overwrite the other's positions.
    The OS drops the lock when the process ends, even if it is killed, so it never goes
    stale the way a PID file would."""
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / "engine.lock", "a+")  # noqa: SIM115 (held open for the whole block)
    try:
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StateLocked(f"another engine is already running on {d}") from exc
        try:
            yield
        finally:
            if os.name == "nt":  # closing would release it too, but not at once
                import msvcrt

                with suppress(OSError):
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        f.close()
