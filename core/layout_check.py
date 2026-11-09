"""Filet de sécurité des corrections Word : le rendu CORRIGÉ est comparé au rendu d'ORIGINE.

Les corrections de texte (core/text_rules.py) ne doivent changer QUE les textes remplacés. Sur des relevés .doc
réels, elles avaient déplacé l'en-tête (titres superposés, référence tronquée sous un cadre vide). Avant de
publier un aperçu corrigé, on vérifie donc, page par page, sur les deux PDF produits par le même Word :

* même nombre de pages ;
* même texte, aux remplacements effectués près (espaces ignorées) : un texte rogné par une zone de texte
  (Word ne dessine pas ce qui déborde) ou dédoublé est détecté ;
* le texte conservé n'a pas bougé : aucun déplacement vertical, et un déplacement horizontal seulement sur
  une ligne qui porte une correction (ligne centrée qui se recentre) ;
* le texte ajouté reste sur la ligne du texte qu'il remplace (pas de ligne supplémentaire) ;
* les images (logo) sont toutes là, à la même place.

En cas d'écart, l'aperçu montre le document d'origine (sans correction) et l'agent en est averti.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

TOLERANCE = 1.5      # pt : au-delà, un caractère est considéré comme déplacé
MAX_MOVED = 2        # caractères déplacés tolérés (arrondis, crénage) avant de déclarer la mise en page modifiée


@dataclass
class PageLayout:
    text: str = ""                                   # caractères visibles, sans espaces
    boxes: list = field(default_factory=list)        # (gauche, bas, droite, haut) de chaque caractère de text
    images: list = field(default_factory=list)       # (gauche, bas, droite, haut) de chaque image


def read_layout(pdf: Path) -> list[PageLayout]:
    """Caractères (avec leur position) et images de chaque page d'un PDF."""
    import pypdfium2.raw as pdfium_c

    from .extractors import open_pdf
    doc = open_pdf(Path(pdf))
    pages: list[PageLayout] = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            lay = PageLayout()
            try:
                tp = page.get_textpage()
                try:
                    n = tp.count_chars()
                    whole = tp.get_text_range(0, n) if n else ""
                    per_char = len(whole) == n
                    chars = []
                    for k in range(n):
                        ch = whole[k] if per_char else tp.get_text_range(k, 1)
                        if not ch or ch.isspace():
                            continue
                        box = tp.get_charbox(k)
                        chars.append(ch)
                        lay.boxes.append(tuple(box))
                    lay.text = "".join(chars)
                finally:
                    tp.close()
                for obj in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=4):
                    try:
                        lay.images.append(tuple(obj.get_bounds()))
                    except Exception:
                        pass
            finally:
                page.close()
            pages.append(lay)
    finally:
        doc.close()
    return pages


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def expected_text(original: str, replacements: list[tuple[str, str]]) -> str:
    """Texte (sans espaces) attendu sur le rendu corrigé : l'original où chaque remplacement effectué est appliqué."""
    out = original
    seen = set()
    for old, new in replacements:
        key = (_squeeze(old), _squeeze(new))
        if key in seen or not key[0]:
            continue
        seen.add(key)
        out = out.replace(key[0], key[1])
    return out


def _center(box) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _height(box) -> float:
    return max(1.0, box[3] - box[1])


def _snippet(text: str, i: int, width: int = 24) -> str:
    return text[max(0, i - 4): i + width]


def _find_all(text: str, sub: str) -> list[int]:
    out, k = [], text.find(sub)
    while sub and k >= 0:
        out.append(k)
        k = text.find(sub, k + len(sub))
    return out


def compare_pages(orig: PageLayout, corr: PageLayout, number: int,
                  replacements: list[tuple[str, str]], tol: float = TOLERANCE) -> str | None:
    """None si la page corrigée ne diffère de l'originale que par les remplacements ; sinon le motif."""
    want = expected_text(orig.text, replacements)
    if want != corr.text:
        k = next((i for i, (x, y) in enumerate(zip(want, corr.text)) if x != y), min(len(want), len(corr.text)))
        return (f"page {number} : texte différent de l'attendu (« {_snippet(want, k)} » attendu, "
                f"« {_snippet(corr.text, k)} » obtenu) — texte rogné ou masqué par une zone de texte ?")
    if sorted(orig.images) != sorted(corr.images):
        imgs_o, imgs_c = sorted(orig.images), sorted(corr.images)
        if len(imgs_o) != len(imgs_c) or any(max(abs(p - q) for p, q in zip(x, y)) > tol
                                             for x, y in zip(imgs_o, imgs_c)):
            return f"page {number} : image (logo) déplacée ou absente"
    a, b = orig.text, corr.text
    if a == b and all(max(abs(p - q) for p, q in zip(x, y)) <= tol for x, y in zip(orig.boxes, corr.boxes)):
        return None
    # textes remplacés (d'origine et nouveaux) : ils peuvent bouger ; ils définissent les « lignes corrigées »
    mask_a, mask_b = [False] * len(a), [False] * len(b)
    occurrences = []
    seen = set()
    for old, new in replacements:
        key = (_squeeze(old), _squeeze(new))
        if key in seen or not key[0]:
            continue
        seen.add(key)
        ia, ib = _find_all(a, key[0]), _find_all(b, key[1]) if key[1] else []
        for st in ia:
            mask_a[st:st + len(key[0])] = [True] * len(key[0])
        for st in ib:
            mask_b[st:st + len(key[1])] = [True] * len(key[1])
        occurrences.extend((range(sa, sa + len(key[0])), range(sb, sb + len(key[1]))) for sa, sb in zip(ia, ib))
    bands = [(_center(orig.boxes[k])[1], _height(orig.boxes[k]) * 0.6) for k in range(len(a)) if mask_a[k]]
    bands += [(_center(corr.boxes[k])[1], _height(corr.boxes[k]) * 0.6) for k in range(len(b)) if mask_b[k]]
    # le texte mis à la place reste sur la (les) ligne(s) du texte remplacé : sinon passage à la ligne, débordement
    for old_idx, new_idx in occurrences:
        lo = min(_center(orig.boxes[k])[1] - _height(orig.boxes[k]) * 0.6 for k in old_idx) - tol
        hi = max(_center(orig.boxes[k])[1] + _height(orig.boxes[k]) * 0.6 for k in old_idx) + tol
        off = [k for k in new_idx if not lo <= _center(corr.boxes[k])[1] <= hi]
        if off:
            return (f"page {number} : le texte corrigé n'est plus à sa hauteur d'origine (« {_snippet(b, off[0])} ») "
                    "— passage à la ligne, débordement d'une zone de texte ou ligne décalée")
    # alignement du texte conservé : préfixe et suffixe communs directement, le milieu par difflib
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    q = 0
    while q < len(a) - p and q < len(b) - p and a[-1 - q] == b[-1 - q]:
        q += 1
    pairs = [(i, i) for i in range(p)] + [(len(a) - 1 - k, len(b) - 1 - k) for k in range(q)]
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a[p:len(a) - q], b[p:len(b) - q], autojunk=False).get_opcodes():
        if tag == "equal":
            pairs.extend((i1 + p + k, j1 + p + k) for k in range(i2 - i1))
    pairs = [(i, j) for i, j in pairs if not mask_a[i] and not mask_b[j]]

    def on_edit_line(y: float) -> bool:
        return any(abs(y - by) <= bh for by, bh in bands)

    moved = []
    for i, j in pairs:
        (xo, yo), (xc, yc) = _center(orig.boxes[i]), _center(corr.boxes[j])
        if abs(yo - yc) > tol or (abs(xo - xc) > tol and not (on_edit_line(yo) or on_edit_line(yc))):
            moved.append(i)
    if len(moved) > MAX_MOVED:
        return (f"page {number} : {len(moved)} caractère(s) conservé(s) déplacé(s) (ex. « {_snippet(a, moved[0])} ») "
                "— lignes décalées ou superposées")
    return None


def compare(original_pdf: Path, corrected_pdf: Path, replacements: list[tuple[str, str]],
            tol: float = TOLERANCE) -> str | None:
    """None si le rendu corrigé est fidèle à l'original (aux remplacements près) ; sinon le motif de l'écart."""
    orig, corr = read_layout(original_pdf), read_layout(corrected_pdf)
    if len(orig) != len(corr):
        return f"nombre de pages différent ({len(orig)} à l'origine, {len(corr)} corrigé)"
    for k, (po, pc) in enumerate(zip(orig, corr), start=1):
        motif = compare_pages(po, pc, k, replacements, tol)
        if motif:
            return motif
    return None
