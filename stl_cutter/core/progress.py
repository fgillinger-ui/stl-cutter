"""Framstegsrapportering och avbrott för långa operationer.

Kärnan känner inte till något GUI. Den anropar bara en valfri callback med
(andel, text). Vill anroparen avbryta kastar callbacken `Cancelled`, som
kärnan låter passera orörd.
"""

from __future__ import annotations

from typing import Callable, Protocol

__all__ = ["Cancelled", "ProgressCallback", "report"]


class Cancelled(Exception):
    """Kastas av anroparens progress-callback för att avbryta en operation."""


class ProgressCallback(Protocol):
    def __call__(self, fraction: float, message: str) -> None:  # pragma: no cover
        ...


def report(progress: Callable[[float, str], None] | None, fraction: float, message: str) -> None:
    """Anropa callbacken om den finns. `Cancelled` släpps vidare."""
    if progress is None:
        return
    progress(max(0.0, min(1.0, float(fraction))), message)
