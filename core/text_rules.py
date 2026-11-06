"""Corrections de texte sur les relevés Word (.doc/.docx).

Certains relevés portent des mentions périmées dans leur en-tête (ancien numéro de téléphone,
ancienne référence des services). Ces règles corrigent le texte au moment de produire le PDF
d'aperçu, donc aussi l'impression, l'export et la fusion du cursus.

Principes :
* l'ORIGINAL n'est jamais modifié : on travaille sur une copie du .docx dans un dossier temporaire ;
* uniquement les fichiers Word (un PDF ou un scan n'a pas de texte modifiable proprement) ;
* le texte est traité paragraphe par paragraphe, en-têtes, pieds de page et zones de texte compris,
  même quand Word a découpé une expression sur plusieurs « runs » (mises en forme successives) ;
  la mise en forme du premier run touché est conservée.

Une règle : {"actif": bool, "regex": bool, "chercher": str, "remplacer": str, "description": str}.
En mode texte simple (regex = False), les espaces de « chercher » acceptent n'importe quel nombre
d'espaces dans le document (« Tel : 33 » trouve « Tel  :  33 »).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
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
    bold: bool | None = None


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
        size = r.get("taille")
        try:
            size = float(size) if size not in (None, "", 0) else None
        except (TypeError, ValueError):
            size = None
        bold = r.get("gras")
        out.append(CompiledRule(pat, repl, r.get("description") or search, size, bold if bold in (True, False) else None))
    return out


def rules_digest(rules: Iterable[dict], enabled: bool = True) -> str:
    """Empreinte courte des règles actives : change => les aperçus déjà produits sont refaits."""
    if not enabled:
        return "brut"
    active = [{k: r.get(k) for k in ("regex", "chercher", "remplacer", "taille", "gras")} for r in rules or [] if r.get("actif", True)
              and (r.get("chercher") or "").strip()]
    if not active:
        return "brut"
    return hashlib.sha1(json.dumps(active, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]


def apply_text(text: str, rules: list[CompiledRule]) -> tuple[str, int]:
    n = 0
    for rule in rules:
        text, k = rule.pattern.subn(rule.replacement, text)
        n += k
    return text, n


# ------------------------------------------------------------------ .docx
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


def _common_affixes(a: str, b: str) -> tuple[int, int]:
    """(préfixe, suffixe) communs. Le suffixe d'abord : « X » -> « Ref N° X » est une insertion devant X."""
    q = 0
    while q < len(a) and q < len(b) and a[-1 - q] == b[-1 - q]:
        q += 1
    p = 0
    while p < len(a) - q and p < len(b) - q and a[p] == b[p]:
        p += 1
    return p, q


def _styled_insert(t_node, offset: int, text: str, size: float | None, bold: bool | None) -> bool:
    """Insère ``text`` dans un run séparé (taille/gras imposés) au milieu du run de ``t_node``.
    Renvoie False si le run est trop complexe pour être découpé sans risque."""
    import copy

    run = t_node.getparent()
    if run is None or run.tag != W + "r":
        return False
    content = [c for c in run if c.tag != W + "rPr"]
    if content != [t_node]:
        return False
    before, after = (t_node.text or "")[:offset], (t_node.text or "")[offset:]
    new_run = copy.deepcopy(run)
    rpr = new_run.find(W + "rPr")
    if rpr is None:
        from lxml import etree
        rpr = etree.SubElement(new_run, W + "rPr")
        new_run.remove(rpr)
        new_run.insert(0, rpr)
    from lxml import etree
    if size:
        for tag in ("sz", "szCs"):
            el = rpr.find(W + tag)
            if el is None:
                el = etree.SubElement(rpr, W + tag)
            el.set(W + "val", str(int(round(size * 2))))
    if bold is not None:
        for tag in ("b", "bCs"):
            el = rpr.find(W + tag)
            if bold and el is None:
                etree.SubElement(rpr, W + tag)
            elif not bold and el is not None:
                rpr.remove(el)
    nt = new_run.find(W + "t")
    nt.text = text
    nt.set(XML_SPACE, "preserve")
    t_node.text = before
    t_node.set(XML_SPACE, "preserve")
    run.addnext(new_run)
    if after:
        tail = copy.deepcopy(run)
        tail.find(W + "t").text = after
        new_run.addnext(tail)
    return True


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


_CHAR_EM = 0.55    # largeur moyenne d'un caractère (en « em ») pour Arial / Times
_SPACE_EM = 0.27


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


def _replace_in_paragraph(paragraph, rules: list[CompiledRule]) -> int:
    count = 0
    growth = 0.0
    for rule in rules:
        nodes = _own_text_nodes(paragraph)
        if not nodes:
            return count
        texts = [t.text or "" for t in nodes]
        full = "".join(texts)
        matches = [m for m in rule.pattern.finditer(full) if m.end() > m.start()]
        if not matches:
            continue
        starts = []
        pos = 0
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
            repl = m.expand(rule.replacement)
            # On ne touche que la partie qui change : le texte conservé garde sa mise en forme d'origine
            # (« Ref N° » ajouté devant le titre ne transforme pas « /TRANSCRIPT » en gros caractères).
            p, q = _common_affixes(m.group(0), repl)
            start, end = m.start() + p, m.end() - q
            middle = repl[p:len(repl) - q]
            count += 1
            i0 = node_at(min(start, max(0, len(full) - 1)))
            growth += len(middle) * (rule.size or _run_size(nodes[i0])) * _CHAR_EM
            growth -= sum(len(texts[k]) and _run_size(nodes[k]) * _CHAR_EM * max(0, min(end, starts[k] + len(texts[k]))
                                                                                - max(start, starts[k]))
                          for k in range(len(nodes)))
            i, j = node_at(start), (node_at(end - 1) if end > start else node_at(start))
            ti, tj = nodes[i].text or "", nodes[j].text or ""
            a, b = start - starts[i], end - starts[j]
            if end > start:   # 1) retirer l'ancien texte
                if i == j:
                    nodes[i].text = ti[:a] + ti[b:]
                else:
                    nodes[i].text = ti[:a]
                    for k in range(i + 1, j):
                        nodes[k].text = ""
                    nodes[j].text = tj[b:]
                for k in range(i, j + 1):
                    nodes[k].set(XML_SPACE, "preserve")
            if middle:        # 2) insérer le nouveau (dans un run à part si une taille / un gras est imposé)
                styled = (rule.size or rule.bold is not None) and _styled_insert(nodes[i], a, middle, rule.size,
                                                                                   rule.bold)
                if not styled:
                    t = nodes[i].text or ""
                    nodes[i].text = t[:a] + middle + t[a:]
                    nodes[i].set(XML_SPACE, "preserve")
    if growth > 0.5:
        _compensate(paragraph, growth)
    return count


def rewrite_docx(src: Path, dest: Path, rules: list[CompiledRule]) -> int:
    """Copie ``src`` vers ``dest`` en appliquant les règles. Renvoie le nombre de remplacements."""
    from lxml import etree

    if not rules:
        shutil.copyfile(src, dest)
        return 0
    total = 0
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if _PARTS.match(item.filename):
                try:
                    root = etree.fromstring(data)
                except etree.XMLSyntaxError:
                    root = None
                if root is not None:
                    n = sum(_replace_in_paragraph(p, rules) for p in list(root.iter(W + "p")))
                    if n:
                        total += n
                        data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(item, data)
    return total
