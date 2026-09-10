"""Grafiskt gränssnitt för stl_cutter (PySide6)."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Starta fönstret. Anropas av `python -m stl_cutter.gui` och `stl-cutter-gui`."""
    from PySide6.QtWidgets import QApplication

    from .app import MainWindow
    from .logging_setup import setup_logging
    from .settings import Settings

    path = setup_logging()

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("STL Cutter")

    window = MainWindow(Settings.load())
    window.status(f"Full logg skrivs till {path}")
    window.show()
    return app.exec()


__all__ = ["main"]
