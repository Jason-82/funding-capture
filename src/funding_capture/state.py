"""JSON persistence for engine state (positions, forecaster, paper books)."""

from __future__ import annotations

import json
import os
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
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, path)


def load_json(path: str | Path) -> dict | None:
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else None
