"""Bakgrundstrådar.

Analys, kapning och export tar sekunder till minuter. De körs i en QThread så
att fönstret aldrig fryser, med framsteg och möjlighet att avbryta.
"""

from __future__ import annotations

import logging
import traceback
from typing import Callable

from PySide6.QtCore import QThread, Signal

from ..core.progress import Cancelled

log = logging.getLogger(__name__)


def friendly_error(exc: BaseException) -> str:
    """Översätt ett undantag till något en användare förstår.

    Hela stacktracen hamnar i loggfilen - användaren ska aldrig se den.
    """
    if isinstance(exc, FileNotFoundError):
        return "Filen gick inte att hitta. Har den flyttats eller tagits bort?"
    if isinstance(exc, PermissionError):
        return "Du saknar behörighet att läsa eller skriva där. Välj en annan mapp."
    if isinstance(exc, MemoryError):
        return "Modellen är för stor för datorns minne. Prova en enklare modell."
    if isinstance(exc, ValueError):
        return f"Modellen gick inte att tolka: {exc}"
    if isinstance(exc, KeyError):
        return f"Något saknades: {exc}"
    if isinstance(exc, OSError):
        return f"Det gick inte att läsa eller skriva filen: {exc}"
    return f"Något gick fel: {exc}"


class Worker(QThread):
    """Kör en funktion i bakgrunden och rapporterar tillbaka.

    Funktionen måste ta emot ett `progress`-argument: en callable som anropas
    med (andel, text). Trycker användaren på Avbryt kastar den `Cancelled`,
    vilket avslutar arbetet vid nästa rapport.
    """

    progressed = Signal(float, str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, work: Callable, parent=None):
        super().__init__(parent)
        self._work = work
        self._cancel = False

    def cancel(self) -> None:
        """Be arbetet avbryta. Sker vid nästa framstegsrapport."""
        self._cancel = True

    @property
    def is_cancelling(self) -> bool:
        return self._cancel

    def _progress(self, fraction: float, message: str) -> None:
        if self._cancel:
            raise Cancelled(message)
        self.progressed.emit(float(fraction), str(message))

    def run(self) -> None:  # pragma: no cover - trådkropp
        try:
            result = self._work(progress=self._progress)
        except Cancelled:
            log.info("Operationen avbröts av användaren.")
            self.cancelled.emit()
        except BaseException as exc:  # noqa: BLE001 - allt ska loggas, inget får krascha
            log.error("Bakgrundsarbetet misslyckades: %s", exc)
            log.debug("%s", traceback.format_exc())
            self.failed.emit(friendly_error(exc))
        else:
            self.succeeded.emit(result)
