from __future__ import annotations


class OptionalVectorIndex:
    """Placeholder interface for local/pluggable embeddings.

    The MVP intentionally has no paid embedding dependency. A future local
    embedding backend can implement this interface.
    """

    def available(self) -> bool:
        return False
