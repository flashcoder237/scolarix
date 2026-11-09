"""Corrections de texte sur les relevés Word (.doc/.docx).

Certains relevés portent des mentions périmées dans leur en-tête (ancien numéro de téléphone,
ancienne référence des services). Ces règles corrigent le texte au moment de produire le PDF
d'aperçu, donc aussi l'impression, l'export et la fusion du cursus.

Principes :
* l'ORIGINAL n'est jamais modifié : on travaille sur une copie du .docx dans un dossier temporaire ;
* uniquement les fichiers Word (un PDF ou un scan n'a pas de texte modifiable proprement) ;
* le texte est traité paragraphe par paragraphe, en-têtes, pieds de page et zones de texte compris,
  même quand Word a découpé une expression sur plusieurs « runs » (mises en forme successives) ;
  la mise en forme du premier run touché est conservée ;
* la mise en page n'est jamais restructurée (zones de texte, cadres, champs, polices symbole intacts) et le
  rendu corrigé est comparé au rendu d'origine (core/layout_check.py) : en cas d'écart, l'original est montré.

Options de mise en forme d'une règle : police, taille, gras, italique, style_sur (« ajout » / « tout ») et,
pour le PARAGRAPHE modifié seulement : interligne (None, « simple », « 1.15 », « 1.5 », « double »,
{"exact": pt} ou {"au_moins": pt}), espace_avant / espace_apres (pt).

Une règle : {"actif": bool, "regex": bool, "chercher": str, "remplacer": str, "description": str}.
En mode texte simple (regex = False), les espaces de « chercher » acceptent n'importe quel nombre
d'espaces dans le document (« Tel : 33 » trouve « Tel  :  33 »).
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
# Parties d'un .docx qui contiennent du texte visible
_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")


@dataclass
class CompiledRule:
    pattern: re.Pattern
    replacement: str
    description: str
    size: float | None = None      # taille (pt) du texte ajouté ; None = celle du texte d'origine
    bold: bool | None = None       # None = comme le texte d'origine
    font: str | None = None        # police (« Arial », « Times New Roman »…) ; None = celle d'origine
    italic: bool | None = None
    whole: bool = False            # True : le style s'applique à tout le résultat, pas seulement au texte ajouté
    unless: re.Pattern | None = None   # ne rien faire si l'en-tête / le document contient déjà ce texte
    line: tuple | None = None      # interligne du paragraphe modifié : (w:line en 240e ou twips, w:lineRule)
    space_before: float | None = None  # espace avant / après le paragraphe modifié (pt)
    space_after: float | None = None

    @property
    def styled(self) -> bool:
        return bool(self.size or self.font) or self.bold is not None or self.italic is not None

    @property
    def spaced(self) -> bool:
        return self.line is not None or self.space_before is not None or self.space_after is not None


_LINE_MULT = {"simple": 1.0, "1": 1.0, "1.0": 1.0, "1.15": 1.15, "1.5": 1.5, "double": 2.0, "2": 2.0}


def parse_line_spacing(value) -> tuple | None:
    """Interligne d'une règle -> (w:line, w:lineRule). None / « » = celui d'origine.
    « simple », « 1.15 », « 1.5 », « double » (ou un multiple) ; {"exact": pt} ; {"au_moins": pt}."""
    if value in (None, "", "origine"):
        return None
    if isinstance(value, dict):
        for key, rule in (("exact", "exact"), ("au_moins", "atLeast")):
            if value.get(key) not in (None, ""):
                pt = float(value[key])
                if not 1 <= pt <= 200:
                    raise ValueError(f"interligne {key} hors limites ({pt} pt)")
                return int(round(pt * 20)), rule
        return None
    text = str(value).strip().lower().replace(",", ".")
    mult = _LINE_MULT.get(text)
    if mult is None:
        mult = float(text)
    if not 0.5 <= mult <= 5:
        raise ValueError(f"interligne hors limites ({value})")
    return int(round(mult * 240)), "auto"


def _pt_or_none(value, what: str) -> float | None:
    if value in (None, ""):
        return None
    pt = float(value)
    if not 0 <= pt <= 200:
        raise ValueError(f"{what} hors limites ({pt} pt)")
    return pt


class RuleError(ValueError):
    pass


def compile_rules(rules: Iterable[dict]) -> list[CompiledRule]:
    out = []
    for i, r in enumerate(rules or [], start=1):
        if not r.get("actif", True):
            continue
        search = r.get("chercher") or ""
        if not search.strip():
            continue
        if r.get("regex"):
            expr = search
        else:
            expr = r"\s*".join(re.escape(part) for part in search.split())
        try:
            pat = re.compile(expr)
        except re.error as exc:
            raise RuleError(f"Règle {i} : expression invalide ({exc}).") from exc
        repl = r.get("remplacer") or ""
        if not r.get("regex"):
            repl = repl.replace("\\", "\\\\")   # texte simple : aucune référence de groupe
        try:
            pat.sub(repl, "")                    # référence de groupe inexistante : signalée ici, pas à l'aperçu
        except (re.error, IndexError) as exc:
            raise RuleError(f"Règle {i} : texte de remplacement invalide ({exc}).") from exc
        size = r.get("taille")
        try:
            size = float(size) if size not in (None, "", 0) else None
        except (TypeError, ValueError):
            size = None
        bold, italic = r.get("gras"), r.get("italique")
        font = (r.get("police") or "").strip() or None
        unless = None
        guard = (r.get("sauf_si") or "").strip()
        if guard:
            expr_u = guard if r.get("regex") else r"\s*".join(re.escape(part) for part in guard.split())
            try:
                unless = re.compile(expr_u, re.IGNORECASE)
            except re.error as exc:
                raise RuleError(f"Règle {i} : « sauf si » invalide ({exc}).") from exc
        try:
            line = parse_line_spacing(r.get("interligne"))
            before = _pt_or_none(r.get("espace_avant"), "espace avant")
            after = _pt_or_none(r.get("espace_apres"), "espace après")
        except (TypeError, ValueError) as exc:
            raise RuleError(f"Règle {i} : {exc}.") from exc
        out.append(CompiledRule(pat, repl, r.get("description") or search, size,
                                bold if bold in (True, False) else None, font,
                                italic if italic in (True, False) else None,
                                (r.get("style_sur") or "ajout") == "tout", unless, line, before, after))
    return out


def rules_digest(rules: Iterable[dict], enabled: bool = True) -> str:
    """Empreinte courte des règles actives : change => les aperçus déjà produits sont refaits."""
    if not enabled:
        return "brut"
    keys = ("regex", "chercher", "remplacer", "taille", "gras", "police", "italique", "style_sur", "sauf_si")
    spacing = ("interligne", "espace_avant", "espace_apres")   # absentes = anciennes empreintes inchangées
    active = [dict({k: r.get(k) for k in keys}, **{k: r[k] for k in spacing if r.get(k) not in (None, "")})
              for r in rules or [] if r.get("actif", True) and (r.get("chercher") or "").strip()]
    if not active:
        return "brut"
    return hashlib.sha1(json.dumps(active, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]


def _already_done(m: re.Match, rule: CompiledRule) -> bool:
    """Le texte trouvé est déjà le texte voulu (relevé corrigé à la main) : ne pas le remplacer à nouveau."""
    return not rule.whole and m.group(0).strip() == m.expand(rule.replacement).strip()


def applicable(rules: list[CompiledRule], text: str) -> list[CompiledRule]:
    """Règles à appliquer à ce texte : celles dont le « sauf si » n'y figure pas déjà."""
    return [r for r in rules if not (r.unless and r.unless.search(text))]


def apply_text(text: str, rules: list[CompiledRule]) -> tuple[str, int]:
    n = 0
    for rule in applicable(rules, text):
        def sub(m, rule=rule):
            nonlocal n
            if _already_done(m, rule):
                return m.group(0)
            n += 1
            return m.expand(rule.replacement)
        text = rule.pattern.sub(sub, text)
    return text, n



# ------------------------------------------------------------------ .docx
#
# Fidélité de la mise en page (2.0.5). Constat sur des relevés .doc réels : les corrections faisaient « bouger »
# l'en-tête (cadre vide, référence rouge tronquée, titres français / anglais superposés). Reproduit sur des
# documents fictifs (tests/word_samples.py) : trois mécanismes, tous corrigés ici.
#  1. Texte plus long dans une zone de TAILLE FIXE (zone de texte, cadre w:framePr, ligne de tableau à hauteur
#     exacte) : il passe à la ligne et déborde ; l'espace de tête de « /FMSP/… » laissait même la 1re ligne vide.
#     -> espace de tête / de fin jamais ajoutée en bord de paragraphe ; dans une zone fixe, le texte ajouté est
#        réduit pour tenir sur le même nombre de lignes, sinon la correction n'est PAS faite dans cette zone.
#  2. Ligne raccourcie (téléphone retiré) qui passe de 2 lignes à 1 : tout ce qui suit dans le flux remonte,
#     alors que les cadres / zones ancrés à la page restent en place -> superposition des titres.
#     -> la largeur retirée est rendue par des espaces quand le nombre de lignes changerait.
#  3. Restylage d'un texte conservé (« R » en 14 pt, « ef N° RELEVE DE NOTES / » en 8 pt) : le préfixe commun
#     était calculé caractère par caractère -> différence calculée mot par mot ; seul le texte ajouté prend
#     le style de la règle.
# En outre : les deux copies d'une zone de texte (mc:Choice DrawingML / mc:Fallback VML) reçoivent exactement le
# même contenu ; les runs en police symbole (Wingdings…), w:sym et champs ne sont jamais touchés ; le texte
# ajouté va dans un run à part qui reprend les propriétés du run d'accueil. Le filet de sécurité final reste
# la comparaison des rendus (core/layout_check.py).

MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
V = "{urn:schemas-microsoft-com:vml}"

SYMBOL_FONTS = {"symbol", "wingdings", "wingdings 2", "wingdings 3", "webdings", "marlett", "zapfdingbats",
                "zapf dingbats", "mt extra", "monotype sorts", "ms outlook", "bookshelf symbol 7"}
MIN_SIZE = 5.0                    # taille minimale (pt) d'un texte ajouté réduit pour tenir dans sa zone


@dataclass
class RewriteReport:
    """Ce que rewrite_docx a fait : sert au contrôle du rendu (core/layout_check.py) et au journal."""
    replacements: list = None     # [(texte d'origine, texte mis à la place)]
    skipped: list = None          # corrections NON faites (zone fixe trop petite, police symbole, champ…)

    def __post_init__(self):
        self.replacements = self.replacements or []
        self.skipped = self.skipped or []


def _own_text_nodes(paragraph) -> list:
    """Nœuds w:t appartenant directement à ce paragraphe (pas à un paragraphe imbriqué d'une zone de texte)."""
    nodes = []
    for t in paragraph.iter(W + "t"):
        anc = t.getparent()
        while anc is not None and anc.tag != W + "p":
            anc = anc.getparent()
        if anc is paragraph:
            nodes.append(t)
    return nodes


_TOKEN = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)


def _edit_ops(old: str, new: str, whole: bool) -> list[tuple[int, int, str, bool]]:
    """Modifications à faire dans ``old`` pour obtenir ``new`` : [(début, fin, texte, stylé)], positions dans old.

    Différence MOT PAR MOT : « RELEVE DE NOTES / TRANSCRIPT » -> « Ref N° RELEVE DE NOTES /TRANSCRIPT » est
    l'insertion de « Ref N° » (stylée) et la suppression d'une espace ; le titre conservé garde sa mise en forme.
    Si le changement n'est pas une simple insertion (référence remplacée), toute la partie qui change est
    remplacée d'un bloc (préfixe / suffixe communs pris par mots entiers seulement) : jamais un mélange de
    tailles au milieu d'un mot."""
    if whole:
        return [(0, len(old), new, True)]
    if old == new:
        return []
    from difflib import SequenceMatcher

    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    pos = [0]
    for tok in a:
        pos.append(pos[-1] + len(tok))
    ops = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    blank = lambda toks: not "".join(toks).strip()  # noqa: E731
    if all(tag in ("equal", "insert") or (blank(a[i1:i2]) and blank(b[j1:j2])) for tag, i1, i2, j1, j2 in ops):
        out = []
        for tag, i1, i2, j1, j2 in ops:
            if tag == "equal":
                continue
            text = "".join(b[j1:j2])
            out.append((pos[i1], pos[i2], text, tag == "insert" and not blank(b[j1:j2])))
        return out
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    q = 0
    while q < len(a) - p and q < len(b) - p and a[-1 - q] == b[-1 - q]:
        q += 1
    return [(pos[p], pos[len(a) - q], "".join(b[p:len(b) - q]), True)]


# Ordre imposé par le schéma OOXML pour les enfants de <w:rPr> : Word refuse (« contenu illisible »)
# un fichier dont les propriétés ne sont pas dans cet ordre.
_RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline",
              "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w",
              "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign",
              "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath"]


def _rpr_child(rpr, tag: str, create: bool = True):
    from lxml import etree
    el = rpr.find(W + tag)
    if el is not None or not create:
        return el
    el = etree.Element(W + tag)
    rank = _RPR_ORDER.index(tag)
    for k, child in enumerate(rpr):
        name = child.tag.replace(W, "")
        if name in _RPR_ORDER and _RPR_ORDER.index(name) > rank:
            rpr.insert(k, el)
            return el
    rpr.append(el)
    return el


def _apply_style(run, size: float | None, bold: bool | None, font: str | None = None,
                 italic: bool | None = None) -> None:
    from lxml import etree
    rpr = run.find(W + "rPr")
    if rpr is None:
        rpr = etree.Element(W + "rPr")
        run.insert(0, rpr)
    if font:
        rf = _rpr_child(rpr, "rFonts")
        for att in ("asciiTheme", "hAnsiTheme", "cstheme", "eastAsiaTheme"):   # une police de thème l'emporterait
            rf.attrib.pop(W + att, None)
        for att in ("ascii", "hAnsi", "cs", "eastAsia"):
            rf.set(W + att, font)
    for flag, tags in ((bold, ("b", "bCs")), (italic, ("i", "iCs"))):
        if flag is None:
            continue
        for tag in tags:
            el = _rpr_child(rpr, tag)
            if flag:
                el.attrib.pop(W + "val", None)
            else:
                el.set(W + "val", "0")   # explicite : sinon le style du paragraphe (souvent gras) reprendrait le dessus
    if size:
        for tag in ("sz", "szCs"):
            _rpr_child(rpr, tag).set(W + "val", str(int(round(size * 2))))


def _is_symbol_run(run) -> bool:
    """Run en police symbole (Wingdings, Symbol…) ou portant un w:sym : un texte inséré y deviendrait des
    pictogrammes (« ∞ », « ✆ »…)."""
    if run is None or run.tag != W + "r":
        return False
    if run.find(W + "sym") is not None:
        return True
    rf = run.find(f"{W}rPr/{W}rFonts")
    if rf is None:
        return False
    return any((rf.get(W + att) or "").strip().lower() in SYMBOL_FONTS for att in ("ascii", "hAnsi", "cs", "eastAsia"))


def _locked_nodes(root) -> set:
    """w:t intouchables : runs en police symbole, texte d'un champ (instruction ou résultat), w:fldSimple."""
    locked = set()
    depth = 0
    for el in root.iter():
        tag = el.tag
        if tag == W + "fldChar":
            kind = el.get(W + "fldCharType")
            if kind == "begin":
                depth += 1
            elif kind == "end":
                depth = max(0, depth - 1)
        elif tag == W + "t":
            run = el.getparent()
            in_simple = False
            anc = run
            while anc is not None and anc.tag != W + "p":
                if anc.tag == W + "fldSimple":
                    in_simple = True
                    break
                anc = anc.getparent()
            if depth or in_simple or _is_symbol_run(run):
                locked.add(el)
    return locked


def _new_run_like(run, text: str):
    """Run neuf portant les MÊMES propriétés que ``run`` (copie de w:rPr) et le texte donné."""
    import copy

    from lxml import etree
    new = etree.Element(W + "r")
    rpr = run.find(W + "rPr") if run is not None else None
    if rpr is not None:
        new.append(copy.deepcopy(rpr))
    t = etree.SubElement(new, W + "t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    return new


def _split_insert(t_node, offset: int, text: str, rule: "CompiledRule | None"):
    """Insère ``text`` dans un run à part au milieu du run de ``t_node`` (le run est coupé en deux, ses autres
    contenus — tabulation, saut de ligne, dessin — restent à leur place). Style de la règle appliqué au seul
    texte inséré. Renvoie le run créé, ou None si ``t_node`` n'est pas dans un run (on insère alors en ligne)."""
    run = t_node.getparent()
    if run is None or run.tag != W + "r":
        return None
    cur = t_node.text or ""
    before, after = cur[:offset], cur[offset:]
    new_run = _new_run_like(run, text)
    if rule is not None and rule.styled:
        _apply_style(new_run, rule.size, rule.bold, rule.font, rule.italic)
    tail_children = list(t_node.itersiblings())
    t_node.text = before
    t_node.set(XML_SPACE, "preserve")
    run.addnext(new_run)
    if after or tail_children:
        tail = _new_run_like(run, after)
        if not after:
            tail.remove(tail.find(W + "t"))
        for child in tail_children:
            tail.append(child)            # déplacé (tabulation, saut de ligne… qui suivaient le texte)
        new_run.addnext(tail)
    return new_run


def _run_size(t_node) -> float:
    run = t_node.getparent()
    if run is not None:
        sz = run.find(f"{W}rPr/{W}sz")
        if sz is not None:
            try:
                return int(sz.get(W + "val")) / 2
            except (TypeError, ValueError):
                pass
    return 11.0


def _run_bold(t_node) -> bool:
    run = t_node.getparent()
    b = run.find(f"{W}rPr/{W}b") if run is not None else None
    return b is not None and (b.get(W + "val") or "1") not in ("0", "false", "off")


def _run_font(t_node) -> str | None:
    run = t_node.getparent()
    rf = run.find(f"{W}rPr/{W}rFonts") if run is not None else None
    return (rf.get(W + "ascii") or rf.get(W + "hAnsi")) if rf is not None else None


# ---------------------------------------------------------------- largeur du texte (estimation)
_CHAR_EM = 0.55    # largeur moyenne d'un caractère (en « em ») pour Arial / Times
_SPACE_EM = 0.27
_WIN_FONTS = {"times new roman": ("times.ttf", "timesbd.ttf"), "arial": ("arial.ttf", "arialbd.ttf"),
              "calibri": ("calibri.ttf", "calibrib.ttf"), "cambria": ("cambria.ttc", "cambriab.ttf"),
              "tahoma": ("tahoma.ttf", "tahomabd.ttf"), "verdana": ("verdana.ttf", "verdanab.ttf"),
              "century gothic": ("gothic.ttf", "gothicb.ttf"), "book antiqua": ("bkant.ttf", "antquab.ttf"),
              "garamond": ("gara.ttf", "garabd.ttf"), "arial narrow": ("arialn.ttf", "arialnb.ttf"),
              "comic sans ms": ("comic.ttf", "comicbd.ttf"), "georgia": ("georgia.ttf", "georgiab.ttf")}
_FONT_CACHE: dict = {}


def _font_object(name: str | None, bold: bool):
    """Police réelle (PIL) pour mesurer le texte ; None si introuvable (estimation moyenne alors)."""
    key = ((name or "times new roman").lower(), bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    try:
        import os
        import platform

        from PIL import ImageFont
        path = None
        if platform.system() == "Windows":
            files = _WIN_FONTS.get(key[0])
            if files:
                cand = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / files[1 if bold else 0]
                path = str(cand) if cand.exists() else None
        else:
            import subprocess
            out = subprocess.run(["fc-match", "-f", "%{file}", f"{key[0]}:style={'Bold' if bold else 'Regular'}"],
                                 capture_output=True, text=True, timeout=5)
            path = out.stdout.strip() or None
        if path:
            font = ImageFont.truetype(path, 100)
    except Exception:
        font = None
    _FONT_CACHE[key] = font
    return font


def _text_width(text: str, size: float, bold: bool = False, font: str | None = None) -> float:
    """Largeur (pt) estimée d'un texte : mesurée sur la police réelle quand elle est installée."""
    if not text:
        return 0.0
    f = _font_object(font, bold)
    if f is not None:
        try:
            return f.getlength(text.replace("\u00a0", " ")) * size / 100.0
        except Exception:
            pass
    em = sum(_SPACE_EM if c.isspace() else (0.68 if c.isupper() else _CHAR_EM) for c in text)
    return em * size * (1.06 if bold else 1.0)


def _node_width(t) -> float:
    return _text_width(t.text or "", _run_size(t), _run_bold(t), _run_font(t))


def _twips(el, att: str) -> float | None:
    try:
        return int(el.get(W + att)) / 20.0
    except (TypeError, ValueError):
        return None


_VML_LEN = re.compile(r"(-?\d+(?:\.\d+)?)\s*(pt|in|cm|mm|px|pc)?")
_UNIT_PT = {"pt": 1.0, "in": 72.0, "cm": 72 / 2.54, "mm": 72 / 25.4, "px": 0.75, "pc": 12.0, None: 1 / 12700}


def _vml_len(value: str | None, default: float | None = None) -> float | None:
    m = _VML_LEN.match((value or "").strip())
    if not m:
        return default
    unit = m.group(2)
    return float(m.group(1)) * (_UNIT_PT[unit] if unit else 1 / 12700 if float(m.group(1)) > 5000 else 1.0)


@dataclass
class _Zone:
    width: float          # largeur disponible (pt)
    fixed: bool           # taille fixe : un texte qui passe à la ligne y déborde (zone de texte, cadre…)


def _zone(paragraph, page_width: float) -> _Zone:
    """Zone dans laquelle s'écoule le paragraphe : flux de la page, cellule, zone de texte ou cadre."""
    width, fixed = page_width, False
    ppr = paragraph.find(W + "pPr")
    if ppr is not None:
        fr = ppr.find(W + "framePr")
        if fr is not None:
            width, fixed = (_twips(fr, "w") or page_width), True
    anc = paragraph.getparent()
    while anc is not None and not fixed:
        tag = anc.tag
        if tag == W + "tc":
            tcw = anc.find(f"{W}tcPr/{W}tcW")
            w = _twips(tcw, "w") if tcw is not None and (tcw.get(W + "type") in (None, "dxa")) else None
            row = anc.getparent()
            if w is None and row is not None:
                w = page_width / max(1, len(row.findall(W + "tc")))
            width = max(10.0, (w or page_width) - 10.8)
            trh = row.find(f"{W}trPr/{W}trHeight") if row is not None else None
            fixed = trh is not None and trh.get(W + "hRule") == "exact"
            break
        if tag == W + "txbxContent":
            fixed = True
            box = anc.getparent()
            if box is not None and box.tag == WPS + "txbx":
                wsp = box.getparent()
                ext = wsp.find(f"{WPS}spPr/{A}xfrm/{A}ext") if wsp is not None else None
                cx = int(ext.get("cx")) if ext is not None and ext.get("cx", "").isdigit() else None
                if cx is None:
                    a2 = wsp
                    while a2 is not None and a2.tag not in (WP + "anchor", WP + "inline"):
                        a2 = a2.getparent()
                    e2 = a2.find(WP + "extent") if a2 is not None else None
                    cx = int(e2.get("cx")) if e2 is not None and e2.get("cx", "").isdigit() else None
                body = wsp.find(WPS + "bodyPr") if wsp is not None else None
                ins = sum(int(body.get(k, 91440)) if body is not None and str(body.get(k, 91440)).isdigit()
                          else 91440 for k in ("lIns", "rIns"))
                width = (cx - ins) / 12700 if cx else page_width
            elif box is not None and box.tag == V + "textbox":
                shape = box.getparent()
                style = (shape.get("style") or "") if shape is not None else ""
                m = re.search(r"(?:^|;)\s*width\s*:\s*([^;]+)", style)
                w = _vml_len(m.group(1)) if m else None
                inset = [x.strip() for x in (box.get("inset") or "").split(",")]
                left = _vml_len(inset[0], 7.2) if inset and inset[0] else 7.2
                right = _vml_len(inset[2], 7.2) if len(inset) > 2 and inset[2] else 7.2
                width = (w - left - right) if w else page_width
            break
        anc = anc.getparent()
    if ppr is not None:
        ind = ppr.find(W + "ind")
        if ind is not None:
            width -= sum(_twips(ind, k) or 0 for k in ("left", "start", "right", "end"))
    return _Zone(max(10.0, width), fixed)


def _segments(paragraph) -> list[float]:
    """Largeur (pt) de chaque segment du paragraphe, séparés par les sauts de ligne manuels. Les objets flottants
    (ancrés) ne comptent pas ; une image en ligne compte pour sa largeur ; une tabulation pour 36 pt."""
    segs = [0.0]

    def visit(el):
        for ch in el:
            tag = ch.tag
            if tag in (W + "pPr", W + "rPr", MC + "Fallback", W + "txbxContent", WP + "anchor", W + "p"):
                continue
            if tag == W + "t":
                segs[-1] += _node_width(ch)
            elif tag == W + "tab":
                segs[-1] += 36.0
            elif tag in (W + "br", W + "cr"):
                segs.append(0.0)
            elif tag == WP + "inline":
                ext = ch.find(WP + "extent")
                if ext is not None and ext.get("cx", "").isdigit():
                    segs[-1] += int(ext.get("cx")) / 12700
            elif tag == W + "pict":
                for shape in ch:
                    style = shape.get("style") or ""
                    if "position:absolute" not in style.replace(" ", ""):
                        m = re.search(r"(?:^|;)\s*width\s*:\s*([^;]+)", style)
                        segs[-1] += (_vml_len(m.group(1)) or 0) if m else 0
            else:
                visit(ch)
    visit(paragraph)
    return segs


def _lines(segs: list[float], zone: _Zone) -> int:
    """Nombre de lignes estimé (même estimation avant / après : seule la différence compte)."""
    import math
    cap = zone.width
    return sum(max(1, math.ceil(s / cap - 1e-6)) for s in segs)


def _compensate(paragraph, growth_pt: float) -> None:
    """Le texte ajouté allonge la ligne : on retire autant d'espaces « de mise en page » (suites d'espaces)
    pour que la ligne ne passe pas à la ligne suivante, ce qui agrandirait l'en-tête et décalerait les pages."""
    nodes = _own_text_nodes(paragraph)
    first_text = next((i for i, t in enumerate(nodes) if (t.text or "").strip()), len(nodes))
    candidates = sorted(((i, t) for i, t in enumerate(nodes) if (t.text or "") and not (t.text or "").strip()),
                        key=lambda it: (it[0] < first_text, -_run_size(it[1])))  # d'abord entre les textes
    for i, t in candidates:
        if growth_pt <= 0:
            break
        keep = 0 if i < first_text else 1          # entre deux textes, on garde au moins une espace
        size = _run_size(t)
        removable = max(0, len(t.text) - keep)
        n = min(removable, int(growth_pt / (size * _SPACE_EM)) + 1)
        if n > 0:
            t.text = t.text[: len(t.text) - n]
            t.set(XML_SPACE, "preserve")
            growth_pt -= n * size * _SPACE_EM


def _set_size(run, size: float) -> None:
    rpr = run.find(W + "rPr")
    if rpr is None:
        from lxml import etree
        rpr = etree.Element(W + "rPr")
        run.insert(0, rpr)
    for tag in ("sz", "szCs"):
        _rpr_child(rpr, tag).set(W + "val", str(int(round(size * 2))))


def _restore(paragraph, snapshot) -> None:
    """Remet le paragraphe dans son état d'avant (en place : les références des paragraphes parents restent)."""
    import copy
    for child in list(paragraph):
        paragraph.remove(child)
    for child in snapshot:
        paragraph.append(copy.deepcopy(child))
    paragraph.attrib.clear()
    paragraph.attrib.update(snapshot.attrib)


class _PartContext:
    def __init__(self, root, page_width: float, report: RewriteReport):
        self.page_width = page_width
        self.report = report
        self.locked = _locked_nodes(root)


def _replace_in_paragraph(paragraph, rules: list[CompiledRule], ctx: "_PartContext | None" = None) -> int:
    import copy

    if ctx is None:   # appel direct (tests, essais) : contexte minimal
        ctx = _PartContext(paragraph, 468.0, RewriteReport())
    nodes = _own_text_nodes(paragraph)
    if not nodes:
        return 0
    full0 = "".join(t.text or "" for t in nodes)
    if not any(m.end() > m.start() and not _already_done(m, r) for r in rules for m in r.pattern.finditer(full0)):
        return 0
    zone = _zone(paragraph, ctx.page_width)
    snapshot = copy.deepcopy(paragraph)
    segs_before = _segments(paragraph)
    count = 0
    performed: list[tuple[str, str]] = []
    created: list = []           # runs insérés (réductibles si la ligne déborde)
    used: list = []              # règles qui ont modifié ce paragraphe (interligne / espacement demandés)
    removal_points: list = []    # (nœud, position) où du texte a été retiré : on peut y rendre la largeur
    for rule in rules:
        nodes = _own_text_nodes(paragraph)
        texts = [t.text or "" for t in nodes]
        full = "".join(texts)
        matches = [m for m in rule.pattern.finditer(full) if m.end() > m.start() and not _already_done(m, rule)]
        if not matches:
            continue
        starts, pos = [], 0
        for tx in texts:
            starts.append(pos)
            pos += len(tx)

        def node_at(offset: int) -> int:
            for i, tx in enumerate(texts):
                if starts[i] <= offset < starts[i] + len(tx):
                    return i
            return len(nodes) - 1

        # De droite à gauche : les positions des correspondances précédentes restent valables.
        for m in reversed(matches):
            span = range(node_at(m.start()), node_at(m.end() - 1) + 1)
            if any(nodes[k] in ctx.locked for k in span):
                ctx.report.skipped.append(f"« {m.group(0).strip()} » : police symbole ou champ, non modifié")
                continue
            repl = m.expand(rule.replacement)
            ops = _edit_ops(m.group(0), repl, rule.whole)
            fixed_ops = []
            for s, e, text, styled in ops:
                S, E = m.start() + s, m.start() + e
                # jamais d'espace ajoutée en bord de paragraphe ou contre une espace existante (1re ligne vide
                # dans une zone de texte, ligne qui s'allonge pour rien)
                if text[:1].isspace() and (S == 0 or full[S - 1].isspace()):
                    text = text.lstrip()
                if text[-1:].isspace() and (E >= len(full) or full[E].isspace()):
                    text = text.rstrip()
                if E > S or text:
                    fixed_ops.append((S, E, text, styled))
            if not fixed_ops:
                continue
            for S, E, text, styled in reversed(fixed_ops):
                if E > S:                  # 1) retirer l'ancien texte
                    i, j = node_at(S), node_at(E - 1)
                    a, b = S - starts[i], E - starts[j]
                    ti, tj = nodes[i].text or "", nodes[j].text or ""
                    if i == j:
                        nodes[i].text = ti[:a] + ti[b:]
                    else:
                        nodes[i].text = ti[:a]
                        for k in range(i + 1, j):
                            nodes[k].text = ""
                        nodes[j].text = tj[b:]
                    for k in range(i, j + 1):
                        nodes[k].set(XML_SPACE, "preserve")
                    host, off = i, a
                    removal_points.append((nodes[i], a))
                elif S > m.start():        # insertion à l'intérieur / à la fin : dans le run du caractère précédent
                    host = node_at(S - 1)
                    off = S - starts[host]
                else:                      # insertion en tête de la correspondance
                    host = node_at(S)
                    off = S - starts[host]
                if text:                   # 2) insérer le nouveau texte dans un run à part
                    new_run = _split_insert(nodes[host], off, text, rule if styled else None)
                    if new_run is None:
                        t = nodes[host].text or ""
                        nodes[host].text = t[:off] + text + t[off:]
                        nodes[host].set(XML_SPACE, "preserve")
                    else:
                        created.append(new_run)
            count += 1
            used.append(rule)
            performed.append((m.group(0), _apply_ops(m.group(0), fixed_ops, m.start())))
    if not count:
        return 0
    # ---- interligne EXACT gardé (la règle n'en demande pas d'autre) : un texte ajouté plus grand que la ligne
    # serait rogné ou chevaucherait la ligne voisine -> il est limité à la hauteur de la ligne
    exact = exact_line_pt(paragraph) if not any(r.line is not None for r in used) else None
    if exact:
        limit = max(MIN_SIZE, exact / 1.15)
        for r in created:
            t = r.find(W + "t")
            if t is not None and _run_size(t) > limit:
                _set_size(r, limit)
                ctx.report.skipped.append(f"« {(t.text or '').strip()} » : réduit à {limit:.1f} pt "
                                          f"(interligne exact de {exact:g} pt)")
    # ---- même nombre de lignes qu'avant : sinon l'en-tête grandit / rapetisse et tout ce qui suit se décale
    before_lines = _lines(segs_before, zone)
    segs = _segments(paragraph)
    growth = sum(segs) - sum(segs_before)
    if growth > 0.5 and not zone.fixed:
        _compensate(paragraph, growth)
        segs = _segments(paragraph)
    if _lines(segs, zone) > before_lines and created:
        sizes = [(r, _run_size(r.find(W + "t"))) for r in created if r.find(W + "t") is not None]
        for factor in (0.9, 0.8, 0.7, 0.6, 0.5):
            for r, sz in sizes:
                _set_size(r, max(MIN_SIZE, sz * factor))
            segs = _segments(paragraph)
            if _lines(segs, zone) <= before_lines:
                break
    if _lines(segs, zone) > before_lines and zone.fixed:
        _restore(paragraph, snapshot)
        ctx.report.skipped.extend(f"« {old.strip()} » : ne tient pas dans sa zone de texte / son cadre, non modifié"
                                  for old, _new in performed)
        return 0
    if _lines(segs, zone) < before_lines and removal_points:
        node, off = removal_points[-1]
        space = max(0.5, _text_width(" ", _run_size(node), _run_bold(node), _run_font(node)))
        n = int(round((sum(segs_before) - sum(segs)) / (2 * space)))
        if n > 0:   # espaces insécables + ordinaires : la ligne garde sa largeur et peut toujours se couper
            t = node.text or ""
            node.text = t[:off] + "\u00a0 " * n + t[off:]
            node.set(XML_SPACE, "preserve")
    for k, rule in enumerate(used):
        if all(rule is not other for other in used[:k]):
            apply_paragraph_spacing(paragraph, rule)
    ctx.report.replacements.extend(performed)
    return count


def _apply_ops(old: str, ops: list, base: int) -> str:
    """Texte obtenu en appliquant ``ops`` (positions absolues, décalées de ``base``) à ``old``."""
    out, last = [], 0
    for S, E, text, _styled in sorted(ops):
        out.append(old[last:S - base])
        out.append(text)
        last = E - base
    out.append(old[last:])
    return "".join(out)


def quiet_proofing(settings_xml: bytes) -> bytes:
    """Copie de travail uniquement : l'orthographe et la grammaire sont déclarées déjà vérifiées et masquées.
    Sans cela, Word relance à l'ouverture la vérification de TOUT le document (lent sur une compilation de
    40 pages en français) alors qu'il ne s'agit que d'exporter un PDF. L'original n'est jamais modifié."""
    from lxml import etree

    try:
        root = etree.fromstring(settings_xml)
    except etree.XMLSyntaxError:
        return settings_xml
    for tag in ("proofState", "hideSpellingErrors", "hideGrammaticalErrors"):
        for old in root.findall(W + tag):
            root.remove(old)
    # ordre du schéma (CT_Settings) : ces éléments suivent writeProtection/view/zoom/removePersonalInformation…
    anchor = 0
    for i, child in enumerate(root):
        if etree.QName(child).localname in ("writeProtection", "view", "zoom", "removePersonalInformation",
                                            "removeDateAndTime", "doNotDisplayPageBoundaries",
                                            "displayBackgroundShape", "printPostScriptOverText",
                                            "printFractionalCharacterWidth", "printFormsData", "embedTrueTypeFonts",
                                            "embedSystemFonts", "saveSubsetFonts", "saveFormsData", "mirrorMargins",
                                            "alignBordersAndEdges", "bordersDoNotSurroundHeader",
                                            "bordersDoNotSurroundFooter", "gutterAtTop"):
            anchor = i + 1
    root.insert(anchor, etree.Element(W + "hideSpellingErrors"))
    root.insert(anchor + 1, etree.Element(W + "hideGrammaticalErrors"))
    pos = anchor + 2
    while pos < len(root) and etree.QName(root[pos]).localname == "activeWritingStyle":
        pos += 1                                  # proofState vient après activeWritingStyle
    proof = etree.Element(W + "proofState")
    proof.set(W + "spelling", "clean")
    proof.set(W + "grammar", "clean")
    root.insert(pos, proof)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


# Ordre des enfants de <w:pPr> (CT_PPrBase) : w:spacing se place avant w:ind, w:jc…
_PPR_ORDER = ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
              "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
              "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid",
              "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
              "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange"]


def _spacing_el(paragraph, create: bool = True):
    from lxml import etree
    ppr = paragraph.find(W + "pPr")
    if ppr is None:
        if not create:
            return None
        ppr = etree.Element(W + "pPr")
        paragraph.insert(0, ppr)
    sp = ppr.find(W + "spacing")
    if sp is not None or not create:
        return sp
    sp = etree.Element(W + "spacing")
    rank = _PPR_ORDER.index("spacing")
    for k, child in enumerate(ppr):
        name = child.tag.replace(W, "")
        if name in _PPR_ORDER and _PPR_ORDER.index(name) > rank:
            ppr.insert(k, sp)
            return sp
    ppr.append(sp)
    return sp


def apply_paragraph_spacing(paragraph, rule: CompiledRule) -> None:
    """Interligne / espaces avant-après demandés par la règle, sur CE paragraphe seulement."""
    if not rule.spaced:
        return
    sp = _spacing_el(paragraph)
    if rule.line is not None:
        sp.set(W + "line", str(rule.line[0]))
        sp.set(W + "lineRule", rule.line[1])
    for value, att in ((rule.space_before, "before"), (rule.space_after, "after")):
        if value is not None:
            sp.set(W + att, str(int(round(value * 20))))
            sp.attrib.pop(W + att + "Lines", None)          # « n lignes » l'emporterait sur la valeur en pt
            sp.attrib.pop(W + att + "Autospacing", None)


def exact_line_pt(paragraph) -> float | None:
    """Hauteur de ligne EXACTE du paragraphe (pt), None si l'interligne s'adapte au texte."""
    sp = _spacing_el(paragraph, create=False)
    if sp is None or sp.get(W + "lineRule") != "exact":
        return None
    try:
        return int(sp.get(W + "line")) / 20.0
    except (TypeError, ValueError):
        return None


def rule_warnings(rules: list[CompiledRule]) -> list[str]:
    """Mises en garde affichées dans l'essai des règles (Réglages) : rien n'est bloqué."""
    out = []
    for r in rules:
        if r.size and r.line is None:
            out.append(f"« {r.description} » : texte en {r.size:g} pt. Dans un paragraphe à interligne EXACT plus "
                       "petit, le texte serait rogné : il y est alors limité à la hauteur de la ligne (ou choisissez "
                       "un interligne pour la règle).")
        if r.line is not None and r.line[1] == "exact" and r.size and r.line[0] / 20 < r.size * 1.1:
            out.append(f"« {r.description} » : interligne exact de {r.line[0] / 20:g} pt pour un texte de "
                       f"{r.size:g} pt : les lignes risquent de se chevaucher.")
    return out


def _page_width(zin: zipfile.ZipFile) -> float:
    """Largeur utile (pt) de la page, d'après la dernière section du document (468 pt par défaut)."""
    from lxml import etree
    try:
        root = etree.fromstring(zin.read("word/document.xml"))
    except Exception:
        return 468.0
    sects = list(root.iter(W + "sectPr"))
    if not sects:
        return 468.0
    sect = sects[-1]
    pg, mar = sect.find(W + "pgSz"), sect.find(W + "pgMar")
    width = (_twips(pg, "w") if pg is not None else None) or 612.0
    if mar is not None:
        width -= sum(_twips(mar, k) or 0 for k in ("left", "right", "gutter"))
    return max(72.0, width)


def _box_text(el) -> str:
    return "".join(t.text or "" for t in el.iter(W + "t"))


def _alternate_pairs(root) -> tuple[list, set]:
    """Zones de texte écrites deux fois (mc:Choice DrawingML + mc:Fallback VML) au contenu identique :
    seule la copie DrawingML est corrigée, puis recopiée dans la copie VML (jamais deux contenus différents).
    Renvoie ([(choice, fallback)], ensemble des w:txbxContent de secours à ne pas traiter)."""
    pairs, skip = [], set()
    for ac in root.iter(MC + "AlternateContent"):
        choice, fallback = ac.find(MC + "Choice"), ac.find(MC + "Fallback")
        if choice is None or fallback is None:
            continue
        cb, fb = list(choice.iter(W + "txbxContent")), list(fallback.iter(W + "txbxContent"))
        if not cb or len(cb) != len(fb):
            continue
        for c, f in zip(cb, fb):
            nested = any(True for _ in c.iter(W + "drawing", W + "pict", MC + "AlternateContent"))
            if not nested and _box_text(c) == _box_text(f):
                pairs.append((c, f))
                skip.add(f)
    return pairs, skip


def _in_skipped_box(paragraph, skip: set) -> bool:
    anc = paragraph.getparent()
    while anc is not None:
        if anc.tag == W + "txbxContent":
            return anc in skip
        anc = anc.getparent()
    return False


def rewrite_docx(src: Path, dest: Path, rules: list[CompiledRule], report: RewriteReport | None = None) -> int:
    """Copie ``src`` vers ``dest`` en appliquant les règles. Renvoie le nombre de remplacements ; ``report``
    (facultatif) reçoit le détail (textes remplacés, corrections écartées pour protéger la mise en page)."""
    import copy

    from lxml import etree

    report = report if report is not None else RewriteReport()
    total = 0
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        page_width = _page_width(zin) if rules else 468.0
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/settings.xml":
                data = quiet_proofing(data)
            if rules and _PARTS.match(item.filename):
                try:
                    root = etree.fromstring(data)
                except etree.XMLSyntaxError:
                    root = None
                if root is not None:
                    # « Sauf si » vérifié sur toute la partie (en-tête de section, corps…) : un « Ref N° » déjà
                    # présent ailleurs sur la ligne ou dans une zone de texte voisine suffit à ne pas en ajouter un.
                    part_text = "".join(t.text or "" for t in root.iter(W + "t"))
                    part_rules = applicable(rules, part_text)
                    n = 0
                    if part_rules:
                        ctx = _PartContext(root, page_width, report)
                        pairs, skip = _alternate_pairs(root)
                        # Paragraphes imbriqués (zones de texte) AVANT leur paragraphe d'ancrage : si celui-ci doit
                        # être remis dans son état d'origine, il l'est avec les corrections de ses zones.
                        for p in reversed(list(root.iter(W + "p"))):
                            if skip and _in_skipped_box(p, skip):
                                continue
                            n += _replace_in_paragraph(p, part_rules, ctx)
                        for c, f in pairs:
                            if _box_text(c) != _box_text(f):
                                for child in list(f):
                                    f.remove(child)
                                for child in c:
                                    f.append(copy.deepcopy(child))
                    if n:
                        total += n
                        data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(item, data)
    return total
