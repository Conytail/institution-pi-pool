from __future__ import annotations

from ..storage import PIIndexStorage


def audit(storage: PIIndexStorage) -> dict:
    return storage.audit_counts()
