"""Aperçu AVANT / APRÈS des remplacements Word (Réglages > Remplacements).

Deux niveaux de fidélité, du plus rapide au plus exact :

* rendu TYPOGRAPHIQUE (immédiat, sans convertisseur) : les paragraphes de l'en-tête du relevé choisi sont lus dans
  le .docx d'origine et dans une copie corrigée (core/text_rules.rewrite_docx, celui de l'aperçu réel), avec leur
  police, leur taille, gras et italique ; la différence est calculée mot par mot (retiré / ajouté) ;
* rendu du MOTEUR : la copie corrigée est convertie en PDF par le même convertisseur que l'aperçu du guichet
  (Word ou LibreOffice), le haut de la 1re page du relevé est découpé en image ; les textes retirés (sur
  l'original) et ajoutés (sur la copie) sont situés sur les images, et le contrôle de fidélité du rendu
  (core/layout_check.py) dit si l'aperçu réel garderait ces corrections ou retomberait sur l'original.

Rien n'est enregistré ni publié dans le cache des aperçus : la copie et son rendu vivent dans le dossier
temporaire de Scolarix (vidé au démarrage et à la fermeture). L'original n'est jamais modifié.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from difflib import SequenceMatcher
from pathlib import Path

from .text_rules import (W, CompiledRule, RewriteReport, _TOKEN, _edit_ops, _own_text_nodes, applicable,
                         rewrite_docx)

PREVIEW_DIR_NAME = "regles_apercu"
HEADER_MAX_PARAGRAPHS = 18
CROP_MIN, CROP_MAX, CROP_DEFAULT = 0.22, 0.5, 0.34      # part de la hauteur de page montrée (haut de page)
RENDER_SCALE = 2.0                                       # 144 dpi : net à 100 % sur un écran 1920


# ------------------------------------------------------------------ rendu typographique (paragraphes et runs)
def _run_style(run) -> dict:
    rpr = run.find(W + "rPr") if run is not None else None
    st: dict = {}
    if rpr is None:
        return st
    sz = rpr.find(W + "sz")
    if sz is not None:
        try:
            st["size"] = int(sz.get(W + "val")) / 2
        except (TypeError, ValueError):
            pass
    for tag, key in (("b", "bold"), ("i", "italic")):
        el = rpr.find(W + tag)
        if el is not None:
            st[key] = (el.get(W + "val") or "1") not in ("0", "false", "off")
    rf = rpr.find(W + "rFonts")
    if rf is not None:
        font = rf.get(W + "ascii") or rf.get(W + "hAnsi")
        if font:
            st["font"] = font
    return st


def _paragraph(p) -> dict:
    """{"chars": [(caractère, style)], "align": …} d'un paragraphe (texte propre, sans zones imbriquées)."""
    chars: list[tuple[str, dict]] = []
    for t in _own_text_nodes(p):
        st = _run_style(t.getparent())
        chars.extend((c, st) for c in (t.text or ""))
    jc = p.find(f"{W}pPr/{W}jc")
    align = (jc.get(W + "val") if jc is not None else None) or "left"
    return {"chars": chars, "align": {"both": "justify", "start": "left", "end": "right"}.get(align, align)}


def _page_breaks_before(p) -> int:
    n = 0
    for br in p.iter(W + "br"):
        if br.get(W + "type") == "page":
            n += 1
    ppr = p.find(W + "pPr")
    if ppr is not None and ppr.find(W + "pageBreakBefore") is not None:
        n += 1
    return n


def header_paragraphs(docx: Path, matricule: str | None = None, page: int = 1) -> list[dict]:
    """Paragraphes de l'en-tête d'un relevé : en-têtes de section, puis le haut de sa page jusqu'à la ligne du
    matricule (et deux lignes après). Sans matricule retrouvé : les premiers paragraphes de la page."""
    from lxml import etree

    out: list[dict] = []
    with zipfile.ZipFile(docx) as z:
        names = z.namelist()
        for name in sorted(n for n in names if re.match(r"^word/header\d*\.xml$", n)):
            try:
                root = etree.fromstring(z.read(name))
            except etree.XMLSyntaxError:
                continue
            for p in root.iter(W + "p"):
                par = _paragraph(p)
                if "".join(c for c, _ in par["chars"]).strip():
                    out.append(par)
            if out:
                break                                   # un seul en-tête (le premier non vide)
        root = etree.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    pars: list[tuple[int, dict]] = []                   # (n° de page, paragraphe)
    cur = 1
    for p in (body.iter(W + "p") if body is not None else []):
        pars.append((cur, _paragraph(p)))
        cur += _page_breaks_before(p)                   # le saut de page est en fin de paragraphe (python-docx)
    key = re.sub(r"\s+", "", (matricule or "")).upper()
    on_page = [i for i, (pg, _) in enumerate(pars) if pg == page] or list(range(len(pars)))
    hit = None
    if key:
        for i in on_page:
            txt = re.sub(r"\s+", "", "".join(c for c, _ in pars[i][1]["chars"])).upper()
            if key in txt:
                hit = i
                break
    start = on_page[0] if on_page else 0
    end = min(len(pars), (hit + 3) if hit is not None else start + 14)
    sel = [par for _pg, par in pars[start:end] if "".join(c for c, _ in par["chars"]).strip()]
    out.extend(sel[-HEADER_MAX_PARAGRAPHS:])
    return out


def _bridge(chars: list[tuple[str, dict]], kinds: list[str], kind: str) -> list[str]:
    """Espaces entre deux mots retirés (ou ajoutés) : retirées (ajoutées) aussi, pour un trait continu."""
    out = list(kinds)
    i, n = 0, len(chars)
    while i < n:
        if chars[i][0].isspace() and out[i] != kind:
            j = i
            while j < n and chars[j][0].isspace():
                j += 1
            if i > 0 and j < n and out[i - 1] == kind and out[j] == kind:
                for k in range(i, j):
                    out[k] = kind
            i = j
        else:
            i += 1
    return out


def _segments(chars: list[tuple[str, dict]], kinds: list[str]) -> list[dict]:
    if "del" in kinds:
        kinds = _bridge(chars, kinds, "del")
    if "ins" in kinds:
        kinds = _bridge(chars, kinds, "ins")
    segs: list[dict] = []
    for (c, st), k in zip(chars, kinds):
        if segs and segs[-1]["k"] == k and segs[-1]["st"] == st:
            segs[-1]["t"] += c
        else:
            segs.append({"t": c, "k": k, "st": st})
    return [dict({"t": s["t"], "k": s["k"]}, **s["st"]) for s in segs]


def diff_paragraph(before: dict, after: dict) -> tuple[list[dict], list[dict]]:
    """Différence mot par mot de deux versions d'un paragraphe : segments AVANT (« eq » / « del ») et APRÈS
    (« eq » / « ins »). Les espaces seules retirées ou ajoutées (compensation de largeur) ne sont pas signalées."""
    a = "".join(c for c, _ in before["chars"])
    b = "".join(c for c, _ in after["chars"])
    ta, tb = _TOKEN.findall(a), _TOKEN.findall(b)
    ka = ["eq"] * len(a)
    kb = ["eq"] * len(b)
    pa = [0]
    for t in ta:
        pa.append(pa[-1] + len(t))
    pb = [0]
    for t in tb:
        pb.append(pb[-1] + len(t))
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if "".join(ta[i1:i2]).strip():
            for k in range(pa[i1], pa[i2]):
                ka[k] = "del"
        if "".join(tb[j1:j2]).strip():
            for k in range(pb[j1], pb[j2]):
                kb[k] = "ins"
    return _segments(before["chars"], ka), _segments(after["chars"], kb)


def typographic_releve(work: Path, copy: Path, matricule: str | None, page: int) -> tuple[list, list]:
    """Paragraphes de l'en-tête, AVANT et APRÈS, prêts à afficher (segments stylés)."""
    pb = header_paragraphs(work, matricule, page)
    pa = header_paragraphs(copy, matricule, page)
    avant, apres = [], []
    for k in range(max(len(pb), len(pa))):
        b = pb[k] if k < len(pb) else {"chars": [], "align": "left"}
        a = pa[k] if k < len(pa) else {"chars": [], "align": "left"}
        sb, sa = diff_paragraph(b, a)
        avant.append({"align": b["align"], "seg": sb})
        apres.append({"align": a["align"], "seg": sa})
    return avant, apres


def typographic_text(text: str, rules: list[CompiledRule]) -> tuple[list, list, str, int]:
    """Texte libre : mêmes règles que apply_text, en suivant l'origine de chaque caractère pour situer ce qui est
    retiré (AVANT) et ajouté (APRÈS, avec la mise en forme de la règle qui l'a ajouté)."""
    from .text_rules import _already_done
    cur: list[tuple[str, int, dict]] = [(c, i, {}) for i, c in enumerate(text)]     # (car., indice d'origine, style)
    n = 0
    for rule in applicable(rules, text):
        plain = "".join(c for c, _, _ in cur)
        style = {k: v for k, v in (("size", rule.size), ("bold", rule.bold), ("italic", rule.italic),
                                   ("font", rule.font)) if v is not None}
        out: list[tuple[str, int, dict]] = []
        last = 0
        for m in rule.pattern.finditer(plain):
            if m.end() == m.start() or _already_done(m, rule):
                continue
            n += 1
            out.extend(cur[last:m.start()])
            pos = m.start()
            for s, e, ins, styled in _edit_ops(m.group(0), m.expand(rule.replacement), rule.whole):
                out.extend(cur[pos:m.start() + s])
                out.extend((c, -1, style if styled else {}) for c in ins)
                pos = m.start() + e
            out.extend(cur[pos:m.end()])
            last = m.end()
        out.extend(cur[last:])
        cur = out
    kept = {i for _, i, _ in cur if i >= 0}
    before = {"chars": [(c, {}) for c in text], "align": "left"}
    ka = ["eq" if (i in kept or c.isspace()) else "del" for i, c in enumerate(text)]
    kb = ["ins" if (i < 0 and not c.isspace()) else "eq" for c, i, _ in cur]
    result = "".join(c for c, _, _ in cur)
    return ([{"align": "left", "seg": _segments(before["chars"], ka)}],
            [{"align": "left", "seg": _segments([(c, st) for c, _, st in cur], kb)}], result, n)


# ------------------------------------------------------------------ rendu du moteur (PDF -> image)
def _page_rects(tp, needle: str, max_y: float, page_h: float) -> list[tuple[float, float, float, float]]:
    """Rectangles (gauche, haut, droite, bas en pt depuis le haut de page) d'un texte sur une page PDF.
    Un texte introuvable tel quel est cherché mot par mot (les espaces du PDF ne suivent pas toujours Word)."""
    needle = re.sub(r"\s+", " ", needle).strip()
    if not needle:
        return []

    def find(txt: str) -> list:
        rects = []
        try:
            searcher = tp.search(txt, match_case=True)
        except Exception:
            return rects
        try:
            while True:
                occ = searcher.get_next()
                if not occ:
                    break
                idx, cnt = occ
                line: list | None = None
                for k in range(idx, idx + cnt):
                    try:
                        l, b, r, t = tp.get_charbox(k, loose=True)     # boîte de ligne : même haut pour ° et N
                    except TypeError:
                        l, b, r, t = tp.get_charbox(k)
                    if r - l <= 0:
                        continue
                    top, bot = page_h - t, page_h - b
                    if line and top < line[3] and bot > line[1]:       # chevauchement vertical : même ligne
                        line[0], line[2] = min(line[0], l), max(line[2], r)
                        line[1], line[3] = min(line[1], top), max(line[3], bot)
                    else:
                        if line:
                            rects.append(tuple(line))
                        line = [l, top, r, bot]
                if line:
                    rects.append(tuple(line))
        finally:
            try:
                searcher.close()
            except Exception:
                pass
        return [x for x in rects if x[1] <= max_y]

    rects = find(needle)
    if rects:
        return rects[:1]                     # la première occurrence dans l'en-tête
    out = []
    for word in needle.split(" "):
        if len(word) >= 2:
            out.extend(find(word)[:1])
    return out


def crop_page(pdf: Path, page: int, anchor: str | None, removed: list[str], added: list[str],
              frac: float | None = None, top: float | None = None) -> dict:
    """Haut de la page ``page`` (1 = première) en image PNG, et rectangles des textes retirés / ajoutés
    (fractions de l'image). La hauteur découpée va jusqu'à la ligne ``anchor`` (matricule) quand elle est trouvée."""
    import pypdfium2 as pdfium

    from .extractors import open_pdf
    doc = open_pdf(Path(pdf))
    try:
        p = doc[max(0, min(page - 1, len(doc) - 1))]
        pw, ph = p.get_size()
        tp = p.get_textpage()
        try:
            if frac is None:
                frac = CROP_DEFAULT
                if anchor:
                    hits = _page_rects(tp, anchor, ph, ph)
                    if hits:
                        frac = min(CROP_MAX, max(CROP_MIN, (hits[0][3] + 30) / ph))
            max_y = ph * frac
            if top is None:                      # marge du haut retirée : on montre le texte, pas du blanc
                top = max_y
                for k in range(min(tp.count_chars(), 600)):
                    l, b, r, t = tp.get_charbox(k)
                    if r > l:
                        top = min(top, ph - t)
                top = max(0.0, min(top - 14, max_y - 60))
            rects = [{"k": "del", "r": r} for t in dict.fromkeys(removed) for r in _page_rects(tp, t, max_y, ph)]
            rects += [{"k": "ins", "r": r} for t in dict.fromkeys(added) for r in _page_rects(tp, t, max_y, ph)]
            rects = [dict(x, r=(x["r"][0], x["r"][1] - top, x["r"][2], x["r"][3] - top)) for x in rects]
        finally:
            tp.close()
        img = p.render(scale=RENDER_SCALE).to_pil()
        crop = img.crop((0, int(img.height * top / ph), img.width, int(img.height * frac)))
        buf = io.BytesIO()
        crop.save(buf, format="PNG", optimize=True)
        p.close()
    finally:
        doc.close()
    h_pt = ph * frac - top
    return {"img": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
            "w": crop.width, "h": crop.height, "w_pt": round(pw, 1), "h_pt": round(h_pt, 1), "frac": round(frac, 3), "top": round(top, 1),
            "rects": [{"k": x["k"], "x": round(x["r"][0] / pw, 4), "y": round(x["r"][1] / h_pt, 4),
                       "w": round((x["r"][2] - x["r"][0]) / pw, 4), "h": round((x["r"][3] - x["r"][1]) / h_pt, 4)}
                      for x in rects]}


def changed_texts(replacements: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Textes retirés et ajoutés (mots entiers) d'après les remplacements faits sur la copie."""
    removed, added = [], []
    for old, new in replacements:
        for s, e, text, _styled in _edit_ops(old, new, False):
            if old[s:e].strip():
                removed.append(old[s:e].strip())
            if text.strip():
                added.append(text.strip())
    return removed, added


def preview_dir() -> Path:
    from .pdf_tools import TEMP_DIR
    d = TEMP_DIR / PREVIEW_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def cached_render(converter, copy: Path, key: str, label: str) -> Path:
    """PDF de la copie corrigée, gardé (clé : fichier + règles) dans le dossier temporaire : revenir à un réglage
    déjà essayé est immédiat. Les 24 rendus les plus récents sont conservés."""
    d = preview_dir()
    dest = d / f"{key}.pdf"
    if dest.exists():
        dest.touch()
        return dest
    with tempfile.TemporaryDirectory(prefix="scolarix_regles_", ignore_cleanup_errors=True) as tmp:
        out = Path(tmp) / "rendu.pdf"
        converter.convert(copy, out, "pdf", label=label)
        shutil.copyfile(out, dest)
    olds = sorted(d.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)[24:]
    for f in olds:
        try:
            f.unlink()
        except OSError:
            pass
    return dest


def render_key(source: str, signature: str, rules: list[dict]) -> str:
    keep = [r for r in rules or [] if r.get("actif", True) and (r.get("chercher") or "").strip()]
    raw = json.dumps([source, signature, keep], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def rewrite_copy(work: Path, rules: list[CompiledRule]) -> tuple[Path, RewriteReport, int]:
    """Copie corrigée (dossier temporaire de Scolarix) : même réécriture que l'aperçu réel."""
    report = RewriteReport()
    d = preview_dir()
    copy = d / f"copie_{hashlib.sha1(str(work).encode('utf-8')).hexdigest()[:10]}_{id(report):x}.docx"
    n = rewrite_docx(work, copy, rules, report)
    return copy, report, n
