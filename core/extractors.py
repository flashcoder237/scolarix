"""Extraction du texte page par page, pour chaque format d'archive.

Stratégie :
- PDF texte      : PDFium (pypdfium2), page par page — pdfplumber en secours ;
- PDF scanné     : rendu 300 DPI (pypdfium2) + pré-traitement (redressement, seuillage Otsu) + Tesseract ;
- Word .docx     : lecture directe python-docx (pagination d'après les sauts de page enregistrés par
                   Word et les en-têtes de relevé) ; l'aperçu PDF est produit à la demande (core/preview.py) ;
- Word .doc      : Word (COM) ou LibreOffice l'enregistre en .docx dans le cache, puis lecture directe.

Aucune fonction de ce module n'écrit dans le dossier source : les conversions vont dans le cache.
"""
from __future__ import annotations

import os
import re
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
    """Texte de chaque page. Moteur PDFium (pypdfium2) : ~50 fois plus rapide que pdfplumber sur les
    relevés FMSP (87 pages : 0,3 s contre 15 s), résultat identique, et il démêle mieux les textes
    superposés. pdfplumber reste un recours si PDFium échoue sur un fichier."""
    try:
        pages = _pages_pdfium(pdf_path)
    except ExtractionError:
        raise
    except Exception:
        return _pages_pdfplumber(pdf_path)
    # PDFium rend le texte dans l'ordre du flux du PDF : sur une page qui porte DEUX relevés, le tableau
    # du 2e étudiant peut précéder son en-tête et être rattaché au 1er. Ces pages (rares) sont relues
    # avec pdfplumber, qui suit l'ordre visuel.
    multi = [p.number for p in pages if len(re.findall(r"M\s?A\s?T\s?R\s?I\s?C\s?U\s?L\s?E", p.text, re.I)) >= 2]
    if multi:
        try:
            import pdfplumber
            with pdfplumber.open(str(pdf_path)) as pdf:
                for n in multi:
                    pages[n - 1] = PageText(n, pdf.pages[n - 1].extract_text() or pages[n - 1].text)
        except Exception:
            pass
    return pages


def open_pdf(pdf_path: Path):
    """Document pypdfium2 lu à travers un fichier ouvert par Python, et non par son chemin : sous Windows, un
    chemin accentué (C:\\Users\\Cédric\\…) ou un fichier déjà ouvert ailleurs (aperçu, antivirus) faisait
    échouer l'ouverture par PDFium (« File access error »)."""
    import pypdfium2 as pdfium
    fh = open(pdf_path, "rb")
    try:
        return pdfium.PdfDocument(fh, autoclose=True)
    except Exception:
        fh.close()
        raise


def describe_error(exc: BaseException) -> str:
    """« PdfiumError : Failed to load page » : la classe ET le message, pour savoir quoi corriger."""
    msg = " ".join(str(exc).split())
    return f"{exc.__class__.__name__} : {msg[:160]}" if msg else exc.__class__.__name__


def read_pages(pdf_path: Path, numbers: list[int]) -> tuple[int, dict[int, str], dict[int, str]]:
    """(nombre de pages, {numéro: texte}, {numéro: raison}) pour quelques pages (contrôle avant impression).
    Une page que PDFium ne sait pas lire est relue avec pdfplumber ; si elle reste illisible, elle est
    signalée (pas de plantage du contrôle entier pour une seule page)."""
    doc = open_pdf(pdf_path)
    out: dict[int, str] = {}
    bad: dict[int, str] = {}
    try:
        n = len(doc)
        for num in numbers:
            if not 1 <= num <= n:
                continue
            try:
                page = doc[num - 1]
                try:
                    tp = page.get_textpage()
                    out[num] = tp.get_text_bounded() or ""
                    tp.close()
                finally:
                    page.close()
            except Exception as exc:
                bad[num] = describe_error(exc)
    finally:
        try:
            doc.close()
        except Exception:     # fermeture : jamais une raison de bloquer une impression déjà contrôlée
            pass
    if bad:
        try:
            import pdfplumber
            with pdfplumber.open(str(pdf_path)) as pdf:
                for num in list(bad):
                    text = pdf.pages[num - 1].extract_text() or ""
                    if text.strip():
                        out[num] = text
                        del bad[num]
        except Exception:
            pass
    return n, out, bad


def page_count_and_text(pdf_path: Path, numbers: list[int]) -> tuple[int, dict[int, str]]:
    n, out, _bad = read_pages(pdf_path, numbers)
    return n, out


def _pages_pdfium(pdf_path: Path) -> list[PageText]:
    import pypdfium2 as pdfium
    try:
        doc = open_pdf(pdf_path)
    except pdfium.PdfiumError as exc:
        msg = str(exc).lower()
        if "password" in msg:
            raise ExtractionError("PDF protégé par mot de passe : ignoré.") from exc
        raise
    pages: list[PageText] = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            try:
                tp = page.get_textpage()
                text = (tp.get_text_bounded() or "").replace("\r\n", "\n").replace("\r", "\n")
                tp.close()
            except Exception:  # page abîmée : on continue
                text = ""
            finally:
                page.close()
            pages.append(PageText(i + 1, text))
    finally:
        doc.close()
    return pages


def _pages_pdfplumber(pdf_path: Path) -> list[PageText]:
    import pdfplumber
    pages: list[PageText] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:
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
    doc = open_pdf(pdf_path)
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
class WordDied(Exception):
    """Le processus WINWORD.EXE a planté ou ne répond plus (serveur RPC indisponible)."""


# 0x800706BA (serveur RPC indisponible), 0x800706BE, RPC_E_DISCONNECTED, RPC_E_CALL_REJECTED
_RPC_DEAD = {-2147023174, -2147023170, -2147417848, -2147417846}


def _rpc_dead(exc: Exception) -> bool:
    code = getattr(exc, "hresult", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0] if isinstance(exc.args[0], int) else None
    return code in _RPC_DEAD


def _word_process_id(word) -> int | None:
    """PID de l'instance Word créée (fenêtre cachée « OpusApp » retrouvée par un titre unique)."""
    try:
        import uuid

        import win32gui  # type: ignore
        import win32process  # type: ignore
        caption = f"scolarix-{uuid.uuid4().hex}"
        word.Caption = caption
        hwnd = win32gui.FindWindow("OpusApp", caption)
        if hwnd:
            return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        pass
    return None


def _pid_file() -> Path:
    from .config import data_dir
    return data_dir() / "word_pids.txt"


def register_word_pid(pid: int) -> None:
    """Note « PID de Word  PID du processus propriétaire » (guichet, robot ou processus de lecture)."""
    try:
        with open(_pid_file(), "a", encoding="utf-8") as fh:
            fh.write(f"{pid} {os.getpid()}\n")
    except OSError:
        pass


def _read_pids() -> list[tuple[int, int]]:
    try:
        rows = []
        for line in _pid_file().read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and all(x.isdigit() for x in parts):
                rows.append((int(parts[0]), int(parts[1])))
        return rows
    except OSError:
        return []


def _write_pids(rows: list[tuple[int, int]]) -> None:
    try:
        _pid_file().write_text("".join(f"{a} {b}\n" for a, b in rows), encoding="utf-8")
    except OSError:
        pass


def unregister_word_pid(pid: int) -> None:
    _write_pids([r for r in _read_pids() if r[0] != pid])


def _process_name(pid: int) -> str | None:
    """Nom de l'exécutable d'un processus vivant, None s'il n'existe plus."""
    if platform.system() == "Windows":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        line = out.strip().splitlines()[0] if out.strip() else ""
        return line.split('","')[0].strip('"') if line.startswith('"') else None
    try:
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/comm").read_text().strip() if Path(f"/proc/{pid}/comm").exists() else "?"
    except OSError:
        return None


def kill_pid(pid: int) -> None:
    if platform.system() != "Windows":
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def kill_orphan_words() -> int:
    """Arrête les instances Word lancées par Scolarix dont le processus propriétaire a disparu (robot tué,
    processus de lecture arrêté, plantage). Jamais un Word encore utilisé, ni un Word ouvert par l'utilisateur."""
    rows = _read_pids()
    keep, n = [], 0
    for word_pid, owner in rows:
        name = _process_name(word_pid)
        if name is None:
            continue                                   # déjà fermé
        if _process_name(owner) is not None:
            keep.append((word_pid, owner))             # propriétaire vivant : on n'y touche pas
            continue
        if "WINWORD" in name.upper():
            kill_pid(word_pid)
            n += 1
    _write_pids(keep)
    return n


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

    word_pid: int | None = None

    def close(self) -> None:
        """Fermeture normale (depuis le fil qui a créé Word) ; à défaut, arrêt du processus par son PID."""
        if self._word is not None:
            try:
                self._word.Quit(SaveChanges=0)
            except Exception:
                self.kill()
            self._word = None
        if self.word_pid:
            unregister_word_pid(self.word_pid)
            self.word_pid = None

    def kill(self) -> None:
        """Arrêt sans COM (utilisable depuis n'importe quel fil) : Word bloqué, processus parent disparu…"""
        if self.word_pid:
            kill_pid(self.word_pid)
            unregister_word_pid(self.word_pid)
            self.word_pid = None
        self._word = None

    def _word_app(self):
        if self._word is None:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
            pythoncom.CoInitialize()
            word = win32com.client.DispatchEx("Word.Application")
            try:
                # Archives anciennes : macros (AutoOpen…) JAMAIS exécutées, pas de liens externes mis à jour
                word.AutomationSecurity = 3          # msoAutomationSecurityForceDisable, avant tout Documents.Open
            except Exception:
                pass
            word.Visible = False
            word.DisplayAlerts = 0
            try:
                word.Options.UpdateLinksAtOpen = False
                word.Options.CheckGrammarAsYouType = False
                word.Options.CheckSpellingAsYouType = False
            except Exception:
                pass
            self._word = word
            self.word_pid = _word_process_id(word)
            if self.word_pid:
                register_word_pid(self.word_pid)
        return self._word

    def convert(self, src: Path, dest: Path, fmt: str = "pdf") -> None:
        """Convertit ``src`` en ``fmt`` (« pdf » ou « docx ») vers ``dest``. L'original n'est jamais modifié."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        errors = []
        if self._com_ok:
            for attempt in (1, 2):
                try:
                    self._convert_com(src, dest, fmt)
                    return
                except WordDied as exc:      # Word a planté : on le relance une fois
                    self.kill()
                    if attempt == 2:
                        errors.append(f"Word COM : {exc}")
                except ExtractionError:
                    raise
                except Exception as exc:  # Word absent : on tente LibreOffice
                    errors.append(f"Word COM : {exc}")
                    self.kill()
                    break
        if self._soffice:
            try:
                self._convert_lo(src, dest, fmt)
                return
            except Exception as exc:
                errors.append(f"LibreOffice : {exc}")
        raise ExtractionError(f"Conversion en {fmt.upper()} impossible. " + " | ".join(errors) if errors
                              else "Aucun convertisseur Word → PDF disponible (installer Word ou LibreOffice).")

    def _convert_com(self, src: Path, dest: Path, fmt: str = "pdf") -> None:
        word = self._word_app()
        try:
            # Mot de passe factice : un document protégé lève une erreur au lieu d'ouvrir une boîte de dialogue.
            doc = word.Documents.Open(str(src.resolve()), ConfirmConversions=False, ReadOnly=True,
                                      AddToRecentFiles=False, PasswordDocument="__fmsp__",
                                      Visible=False, NoEncodingDialog=True)
        except Exception as exc:
            if _rpc_dead(exc):
                raise WordDied(str(exc)) from exc
            text = str(exc).lower()
            if "password" in text or "mot de passe" in text:
                raise ExtractionError("Fichier protégé par mot de passe : ignoré, indexation poursuivie.") from exc
            raise ExtractionError(f"Word ne peut pas ouvrir le fichier (endommagé ?) : {exc}") from exc
        try:
            if fmt == "docx":
                doc.SaveAs2(str(dest.resolve()), FileFormat=16, AddToRecentFiles=False)  # 16 = .docx
            else:
                doc.ExportAsFixedFormat(str(dest.resolve()), 17)  # 17 = wdExportFormatPDF
        except Exception as exc:
            if _rpc_dead(exc):
                raise WordDied(str(exc)) from exc
            raise
        finally:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass

    def _convert_lo(self, src: Path, dest: Path, fmt: str = "pdf") -> None:
        with tempfile.TemporaryDirectory(prefix="fmsp_conv_") as tmp:
            # Copie temporaire : LibreOffice ne touche jamais l'original (règle de non-destruction).
            tmp_src = Path(tmp) / ("source" + src.suffix.lower())
            shutil.copy2(src, tmp_src)
            cmd = [self._soffice, f"-env:UserInstallation={self._lo_profile.as_uri()}", "--headless",
                   "--norestore", "--convert-to", fmt, "--outdir", tmp, str(tmp_src)]
            proc = subprocess.run(cmd, capture_output=True, timeout=180,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            produced = Path(tmp) / f"source.{fmt}"
            if tmp_src.suffix.lower() == f".{fmt}":   # .docx -> .docx : LibreOffice écrirait sur la copie
                produced = tmp_src
            if proc.returncode != 0 or not produced.exists():
                raise ExtractionError("LibreOffice n'a pas pu convertir le fichier (protégé ou endommagé ?).")
            shutil.move(str(produced), dest)


# --------------------------------------------------- .docx sans convertisseur
_DOCX_HEADER_RE = re.compile(r"\s*NOMS?\s*(?:ET|&)\s*PR[ÉE]NOMS?\b", re.IGNORECASE)
_DOCX_MATRICULE_RE = re.compile(r"MATRICULE\s*:?\s*\d{2}\s*[-/. ]?\s*[A-Z]{1,4}\s*[-/. ]?\s*\d{2,4}", re.IGNORECASE)


def docx_pages_text(path: Path) -> list[PageText]:
    """Lecture python-docx, pagination d'après les sauts de page explicites et ceux mémorisés par Word."""
    import docx
    from docx.oxml.ns import qn

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ExtractionError(f"Document Word illisible ({exc.__class__.__name__}).") from exc

    pages: list[list[str]] = [[]]
    # Zones de texte : Word écrit la même zone deux fois (mc:Choice moderne + mc:Fallback VML) ;
    # on ne lit que la première (en mémoire seulement, le fichier n'est pas modifié).
    mc_fallback = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
    for fb in list(document.element.body.iter(mc_fallback)):
        fb.getparent().remove(fb)

    def add(text: str) -> None:
        # Un relevé qui commence alors que la page courante en contient déjà un : Word l'a placé sur
        # une nouvelle page par simple débordement (aucun saut enregistré dans le fichier).
        if _DOCX_HEADER_RE.match(text) and any(_DOCX_MATRICULE_RE.search(x) for x in pages[-1]):
            pages.append([])
        pages[-1].append(text)

    def walk(element):
        for child in element.iterchildren():
            tag = child.tag
            if tag == qn("w:p"):
                line = []
                for node in child.iter():
                    if node.tag == qn("w:p") and node is not child:
                        line.append("\n")  # paragraphe imbriqué (zone de texte) : ne pas coller les mots
                    elif node.tag == qn("w:t") and node.text:
                        line.append(node.text)
                    elif node.tag == qn("w:tab"):
                        line.append("\t")
                    elif (node.tag == qn("w:br") and node.get(qn("w:type")) == "page") or \
                            node.tag == qn("w:lastRenderedPageBreak"):
                        # Word enregistre souvent un saut explicite ET un saut « rendu » : on ne coupe
                        # qu'une fois, et seulement si la page courante contient déjà du texte.
                        if "".join(line).strip():
                            add("".join(line))
                            line = []
                        if any(s.strip() for s in pages[-1]):
                            pages.append([])
                if "".join(line).strip():
                    add("".join(line))
                ppr = child.find(qn("w:pPr"))
                if ppr is not None and ppr.find(qn("w:sectPr")) is not None and any(s.strip() for s in pages[-1]):
                    pages.append([])
            elif tag == qn("w:tbl"):
                for row in child.findall(qn("w:tr")):        # lignes DIRECTES (pas celles d'un tableau imbriqué)
                    cells = []
                    for cell in row.findall(qn("w:tc")):
                        cells.append(" ".join(t.text for t in cell.iter(qn("w:t")) if t.text))
                    add("  ".join(cells))

    walk(document.element.body)
    texts = ["\n".join(p).strip() for p in pages]
    while len(texts) > 1 and not texts[-1]:
        texts.pop()
    return [PageText(i, t) for i, t in enumerate(texts, start=1)]


def file_signature(path: Path) -> tuple[int, float]:
    st = os.stat(path)
    return st.st_size, st.st_mtime
