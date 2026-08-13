from __future__ import annotations

from typing import Protocol


class ReleasePlatform(Protocol):
    def stage(self) -> None: ...

    def verify(self) -> None: ...

    def save_pointer(self) -> bytes: ...

    def promote(self) -> None: ...

    def restore(self, previous: bytes) -> None: ...


class ReleaseTransactionError(RuntimeError):
    pass


def publish_transaction(
    github: ReleasePlatform,
    modelscope: ReleasePlatform,
) -> None:
    try:
        github.stage()
        modelscope.stage()
        github.verify()
        modelscope.verify()
        previous = modelscope.save_pointer()
        modelscope.promote()
        try:
            github.promote()
        except Exception:
            modelscope.restore(previous)
            raise
    except Exception as exc:
        raise ReleaseTransactionError(f"release transaction failed ({type(exc).__name__})") from exc
