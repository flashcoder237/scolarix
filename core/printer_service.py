"""Impression directe, sans ouvrir Word ni Adobe.

Ordre de préférence :
1. une commande configurée (ex. SumatraPDF ou PDFtoPrinter), impression vectorielle silencieuse ;
2. SumatraPDF.exe ou PDFtoPrinter.exe déposé dans le dossier ``tools`` de l'application ;
3. impression via Qt (QPdfDocument -> QPrinter) sur l'imprimante par défaut, sans boîte de dialogue.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from .config import Settings


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _auto_command() -> str | None:
    tools = app_dir() / "tools"
    sumatra = tools / "SumatraPDF.exe"
    if sumatra.exists():
        return f'"{sumatra}" -print-to-default -print-settings "duplex" -silent "{{file}}"'
    p2p = tools / "PDFtoPrinter.exe"
    if p2p.exists():
        return f'"{p2p}" "{{file}}"'
    return None


def print_pdf(pdf_path: str | Path, settings: Settings) -> str:
    """Imprime le PDF. Renvoie une description de la méthode utilisée. Lève RuntimeError en cas d'échec."""
    pdf_path = str(pdf_path)
    command = settings.print_command or _auto_command()
    if command:
        cmd = command.replace("{file}", pdf_path).replace("{printer}", settings.printer_name or "")
        args = cmd if sys.platform == "win32" else shlex.split(cmd)
        proc = subprocess.run(args, capture_output=True, timeout=120,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            raise RuntimeError(f"La commande d'impression a échoué (code {proc.returncode}).")
        return "commande d'impression"
    return _print_with_qt(pdf_path, settings)


def _print_with_qt(pdf_path: str, settings: Settings) -> str:
    from PySide6.QtCore import QRectF, QSize
    from PySide6.QtGui import QPainter, QPageSize
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

    doc = QPdfDocument()
    if doc.load(pdf_path) != QPdfDocument.Error.None_:
        raise RuntimeError("Le PDF à imprimer n'a pas pu être chargé.")
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    if settings.printer_name:
        info = QPrinterInfo.printerInfo(settings.printer_name)
        if not info.isNull():
            printer.setPrinterName(settings.printer_name)
    elif QPrinterInfo.defaultPrinter().isNull():
        raise RuntimeError("Aucune imprimante par défaut n'est configurée sur ce poste.")
    printer.setDocName(Path(pdf_path).name)
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    printer.setFullPage(True)
    if settings.duplex:
        printer.setDuplex(QPrinter.DuplexMode.DuplexAuto)
    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("Impossible d'ouvrir l'imprimante.")
    try:
        dpi = min(printer.resolution(), 300)
        for i in range(doc.pageCount()):
            if i:
                printer.newPage()
            pt = doc.pagePointSize(i)
            img = doc.render(i, QSize(int(pt.width() / 72 * dpi), int(pt.height() / 72 * dpi)))
            target = QRectF(printer.pageLayout().fullRectPixels(printer.resolution()))
            scale = min(target.width() / img.width(), target.height() / img.height())
            w, h = img.width() * scale, img.height() * scale
            painter.drawImage(QRectF((target.width() - w) / 2, (target.height() - h) / 2, w, h), img)
    finally:
        painter.end()
        doc.close()
    return "Qt (imprimante par défaut)"
