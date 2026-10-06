"""Append-only JSON Lines files, one per kind of event."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Journal:
    def __init__(self, directory: Path):
        self._dir = Path(directory)

    def write(self, name: str, record: dict) -> None:
        line = json.dumps({"ts": time.time(), **record}, separators=(",", ":"))
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._dir / f"{name}.jsonl", "a") as f:
                f.write(line + "\n")
        except OSError as exc:
            log.warning("could not write %s journal: %s", name, exc)
