"""Extraction du texte page par page, pour chaque format d'archive.

Stratégie :
- PDF texte      : pdfplumber, page par page ;
- PDF scanné     : rendu 300 DPI (pypdfium2) + pré-traitement (redressement, seuillage Otsu) + Tesseract ;
- Word .doc/.docx: conversion en PDF dans le cache (COM Word sous Windows, sinon LibreOffice),
                   puis lecture du PDF. On obtient ainsi les VRAIS numéros de page et l'aperçu
                   identique au guichet. Sans convertisseur, les .docx sont lus avec python-docx
                   (pagination d'après les sauts de page enregistrés par Word) mais sans aperçu.

Aucune fonction de ce module n'écrit dans le dossier source : les conversions vont dans le cache.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .config import Settings

MIN_TEXT_CHARS = 40          # en dessous, une page PDF est considérée comme scannée


class ExtractionError(Exception):
    """Erreur lisible par l'opérateur (fichier protégé, corrompu...)."""


@dataclass
class PageText:
    number: int               # 1-based
    text: str
    ocr_confidence: float | None = None


# ----------------------------------------------------------------- détection
def detect_type(path: Path) -> str:
    """Type réel d'après la signature binaire (et non l'extension) : PDF, DOCX ou DOC."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        raise ExtractionError(f"Lecture impossible : {exc}") from exc
    if head.startswith(b"%PDF"):
        return "PDF"
    if head.startswith(b"PK\x03\x04"):
        return "DOCX"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        _check_ole_word(path)
        return "DOC"
    ext = path.suffix.lower()
    if ext == ".pdf":
        raise ExtractionError("Fichier PDF endommagé (signature %PDF absente).")
    if ext in (".doc", ".docx"):
        raise ExtractionError("Fichier Word endommagé (en-tête illisible).")
    raise ExtractionError("Format non pris en charge.")


def _check_ole_word(path: Path) -> None:
    """Un .doc 97-2003 est un conteneur OLE avec un flux « WordDocument » : sinon il est endommagé."""
    try:
        import olefile
    except ImportError:  # pragma: no cover - dépendance optionnelle
        return
    try:
        if not olefile.isOleFile(str(path)):
            raise ExtractionError("Fichier endommagé (en-tête OLE illisible). Aucune écriture tentée sur l'original.")
        with olefile.OleFileIO(str(path)) as ole:
            if ole.exists("EncryptedPackage") or ole.exists("EncryptionInfo"):
                raise ExtractionError("Fichier protégé par mot de passe : ignoré, indexation poursuivie.")
            if not ole.exists("WordDocument"):
                raise ExtractionError("Fichier OLE sans contenu Word (endommagé ou autre format).")
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Fichier endommagé (en-tête OLE illisible : {exc.__class__.__name__}).") from exc


# ---------------------------------------------------------------- PDF texte
def pdf_pages_text(pdf_path: Path) -> list[PageText]:
    import pdfplumber
    pages: list[PageText] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:  # page abîmée : on continue
                    text = ""
                pages.append(PageText(i, text))
    except Exception as exc:
        msg = str(exc).lower()
        if "password" in msg or "encrypt" in msg:
            raise ExtractionError("PDF protégé par mot de passe : ignoré.") from exc
        raise ExtractionError(f"PDF illisible ou corrompu ({exc.__class__.__name__}).") from exc
    return pages


def pdf_page_count(pdf_path: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf_path)).pages)


# ---------------------------------------------------------------------- OCR
def _otsu_threshold(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = gray.size
    sum_total = np.dot(np.arange(256), hist)
    sum_b = w_b = 0.0
    best, threshold = 0.0, 127
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, threshold = between, t
    return threshold


def _estimate_skew(binary: np.ndarray) -> float:
    """Angle (degrés) maximisant la variance du profil horizontal : méthode des projections."""
    small = Image.fromarray(binary).resize((max(1, binary.shape[1] // 4), max(1, binary.shape[0] // 4)))
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-5.0, 5.01, 0.5):
        rotated = np.asarray(small.rotate(angle, expand=False, fillcolor=255)) < 128
        profile = rotated.sum(axis=1).astype(np.float64)
        score = float(np.var(profile))
        if score > best_score:
            best_angle, best_score = float(angle), score
    return best_angle


def preprocess_for_ocr(img: Image.Image) -> tuple[Image.Image, float]:
    """Niveaux de gris -> redressement (deskew) -> seuillage adaptatif Otsu. Renvoie (image, angle)."""
    gray = ImageOps.grayscale(img)
    arr = np.asarray(gray)
    t = _otsu_threshold(arr)
    binary = np.where(arr > t, 255, 0).astype(np.uint8)
    angle = _estimate_skew(binary)
    if abs(angle) >= 0.5:
        gray = gray.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
        arr = np.asarray(gray)
        t = _otsu_threshold(arr)
        binary = np.where(arr > t, 255, 0).astype(np.uint8)
    return Image.fromarray(binary), angle


def configure_tesseract(settings: Settings) -> bool:
    import pytesseract
    cmd = settings.tesseract_cmd or shutil.which("tesseract")
    if not cmd and platform.system() == "Windows":
        for cand in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                     r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if Path(cand).exists():
                cmd = cand
                break
    if not cmd:
        return False
    pytesseract.pytesseract.tesseract_cmd = cmd
    return True


def ocr_pdf_pages(pdf_path: Path, page_numbers: list[int], settings: Settings) -> dict[int, PageText]:
    import pypdfium2 as pdfium
    import pytesseract

    out: dict[int, PageText] = {}
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        for n in page_numbers:
            page = doc[n - 1]
            img = page.render(scale=300 / 72).to_pil()
            if settings.ocr_preprocess:
                img, _angle = preprocess_for_ocr(img)
            data = pytesseract.image_to_data(img, lang=settings.ocr_lang or "fra",
                                             config="--psm 6", output_type=pytesseract.Output.DICT)
            lines: dict[tuple, list[str]] = {}
            confs = []
            for i, word in enumerate(data["text"]):
                if not word.strip():
                    continue
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                lines.setdefault(key, []).append(word)
                try:
                    c = float(data["conf"][i])
                    if c >= 0:
                        confs.append(c)
                except (TypeError, ValueError):
                    pass
            text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
            out[n] = PageText(n, text, round(sum(confs) / len(confs), 1) if confs else 0.0)
    finally:
        doc.close()
    return out


# ------------------------------------------------------- conversion Word -> PDF
class WordConverter:
    """Convertit .doc/.docx en PDF. Réutilise une seule instance Word pendant toute l'indexation."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._word = None
        self._com_ok = settings.word_com_enabled and platform.system() == "Windows"
        self._soffice = settings.soffice_cmd or shutil.which("soffice") or shutil.which("libreoffice")
        if not self._soffice and platform.system() == "Windows":
            cand = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")
            if cand.exists():
                self._soffice = str(cand)
        self._lo_profile = Path(tempfile.gettempdir()) / "fmsp_lo_profile"

    @property
    def available(self) -> bool:
        return self._com_ok or bool(self._soffice)

    def backend(self) -> str:
        if self._com_ok:
            return "Microsoft Word (COM)"
        if self._soffice:
            return "LibreOffice"
        return "aucun"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self._word is not None:
            try:
                self._word.Quit(SaveChanges=0)
            except Exception:
                pass
            self._word = None

    def _word_app(self):
        if self._word is None:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
            pythoncom.CoInitialize()
            self._word = win32com.client.DispatchEx("Word.Application")
            self._word.Visible = False
            self._word.DisplayAlerts = 0
        return self._word

    def convert(self, src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        errors = []
        if self._com_ok:
            try:
                self._convert_com(src, dest)
                return
            except ExtractionError:
                raise
            except Exception as exc:  # Word absent ou planté : on tente LibreOffice
                errors.append(f"Word COM : {exc}")
                self.close()
        if self._soffice:
            try:
                self._convert_lo(src, dest)
                return
            except Exception as exc:
                errors.append(f"LibreOffice : {exc}")
        raise ExtractionError("Conversion en PDF impossible. " + " | ".join(errors) if errors
                              else "Aucun convertisseur Word → PDF disponible (installer Word ou LibreOffice).")

    def _convert_com(self, src: Path, dest: Path) -> None:
        word = self._word_app()
        try:
            # Mot de passe factice : un document protégé lève une erreur au lieu d'ouvrir une boîte de dialogue.
            doc = word.Documents.Open(str(src.resolve()), ConfirmConversions=False, ReadOnly=True,
                                      AddToRecentFiles=False, PasswordDocument="__fmsp__",
                                      Visible=False, NoEncodingDialog=True)
        except Exception as exc:
            text = str(exc).lower()
            if "password" in text or "mot de passe" in text:
                raise ExtractionError("Fichier protégé par mot de passe : ignoré, indexation poursuivie.") from exc
            raise ExtractionError(f"Word ne peut pas ouvrir le fichier (endommagé ?) : {exc}") from exc
        try:
            doc.ExportAsFixedFormat(str(dest.resolve()), 17)  # 17 = wdExportFormatPDF
        finally:
            doc.Close(SaveChanges=0)

    def _convert_lo(self, src: Path, dest: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="fmsp_conv_") as tmp:
            # Copie temporaire : LibreOffice ne touche jamais l'original (règle de non-destruction).
            tmp_src = Path(tmp) / ("source" + src.suffix.lower())
            shutil.copy2(src, tmp_src)
            cmd = [self._soffice, f"-env:UserInstallation={self._lo_profile.as_uri()}", "--headless",
                   "--norestore", "--convert-to", "pdf", "--outdir", tmp, str(tmp_src)]
            proc = subprocess.run(cmd, capture_output=True, timeout=180,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            produced = Path(tmp) / "source.pdf"
            if proc.returncode != 0 or not produced.exists():
                raise ExtractionError("LibreOffice n'a pas pu convertir le fichier (protégé ou endommagé ?).")
            shutil.move(str(produced), dest)


# --------------------------------------------------- .docx sans convertisseur
def docx_pages_text(path: Path) -> list[PageText]:
    """Lecture python-docx, pagination d'après les sauts de page explicites et ceux mémorisés par Word."""
    import docx
    from docx.oxml.ns import qn

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ExtractionError(f"Document Word illisible ({exc.__class__.__name__}).") from exc

    pages: list[list[str]] = [[]]

    def walk(element):
        for child in element.iterchildren():
            tag = child.tag
            if tag == qn("w:p"):
                line = []
                for node in child.iter():
                    if node.tag == qn("w:t") and node.text:
                        line.append(node.text)
                    elif node.tag == qn("w:tab"):
                        line.append("\t")
                    elif (node.tag == qn("w:br") and node.get(qn("w:type")) == "page") or \
                            node.tag == qn("w:lastRenderedPageBreak"):
                        # Word enregistre souvent un saut explicite ET un saut « rendu » : on ne coupe
                        # qu'une fois, et seulement si la page courante contient déjà du texte.
                        if "".join(line).strip():
                            pages[-1].append("".join(line))
                            line = []
                        if any(s.strip() for s in pages[-1]):
                            pages.append([])
                if "".join(line).strip():
                    pages[-1].append("".join(line))
                ppr = child.find(qn("w:pPr"))
                if ppr is not None and ppr.find(qn("w:sectPr")) is not None and any(s.strip() for s in pages[-1]):
                    pages.append([])
            elif tag == qn("w:tbl"):
                for row in child.iter(qn("w:tr")):
                    cells = []
                    for cell in row.iter(qn("w:tc")):
                        cells.append(" ".join(t.text for t in cell.iter(qn("w:t")) if t.text))
                    pages[-1].append("  ".join(cells))

    walk(document.element.body)
    texts = ["\n".join(p).strip() for p in pages]
    while len(texts) > 1 and not texts[-1]:
        texts.pop()
    return [PageText(i, t) for i, t in enumerate(texts, start=1)]


def file_signature(path: Path) -> tuple[int, float]:
    st = os.stat(path)
    return st.st_size, st.st_mtime
