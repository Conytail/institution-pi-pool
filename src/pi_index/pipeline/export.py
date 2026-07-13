from __future__ import annotations

from pathlib import Path

from ..storage import PIIndexStorage


def export_outputs(storage: PIIndexStorage, out_dir: str | Path) -> None:
    storage.export(out_dir)
