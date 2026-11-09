"""Relevés Word FICTIFS à en-tête « complexe », comme les vieux .doc de la FMSP : logo en ligne, zones de texte
flottantes (DrawingML + VML de secours dans mc:AlternateContent), cadre (w:framePr) à interligne exact,
titres bilingues centrés, puce en police symbole. Aucune donnée réelle."""
from __future__ import annotations

import io
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
    "w10": "urn:schemas-microsoft-com:office:word",
}
_DECL = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
REF = "FMSP/VDAAP/VDSSSE/SSSE"


def _png() -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (120, 120), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((5, 5, 115, 115), outline=(0, 0, 128), width=8)
    d.rectangle((40, 40, 80, 80), fill=(0, 0, 128))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _run(text: str, size: float = 9, color: str | None = None, bold: bool = False, font: str | None = None) -> str:
    rpr = ""
    if font:
        rpr += f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:cs="{font}"/>'
    if bold:
        rpr += "<w:b/>"
    if color:
        rpr += f'<w:color w:val="{color}"/>'
    rpr += f'<w:sz w:val="{int(size * 2)}"/><w:szCs w:val="{int(size * 2)}"/>'
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'


def textbox(text: str, x_pt: float, y_pt: float, w_pt: float, h_pt: float, shape_id: int = 1,
            size: float = 9, color: str = "FF0000", bold: bool = True, rel_v: str = "page",
            border: str = "0070C0") -> str:
    """Zone de texte flottante de taille FIXE : DrawingML (mc:Choice) + VML (mc:Fallback), même texte."""
    emu = lambda pt: int(pt * 12700)  # noqa: E731
    inner = f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr>{_run(text, size, color, bold)}</w:p>'
    choice = (
        f'<w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="{shape_id}" '
        f'behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1"><wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="page"><wp:posOffset>{emu(x_pt)}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="{rel_v}"><wp:posOffset>{emu(y_pt)}</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{emu(w_pt)}" cy="{emu(h_pt)}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
        f'<wp:docPr id="{100 + shape_id}" name="Zone de texte {shape_id}"/><wp:cNvGraphicFramePr/>'
        f'<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        f'<wps:wsp><wps:cNvSpPr txBox="1"/><wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{emu(w_pt)}" '
        f'cy="{emu(h_pt)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
        f'<a:ln w="9525"><a:solidFill><a:srgbClr val="{border}"/></a:solidFill></a:ln></wps:spPr>'
        f'<wps:txbx><w:txbxContent>{inner}</w:txbxContent></wps:txbx>'
        f'<wps:bodyPr rot="0" vert="horz" wrap="square" lIns="18000" tIns="0" rIns="18000" bIns="0" anchor="t" '
        f'anchorCtr="0"><a:noAutofit/></wps:bodyPr></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing>')
    fallback = (
        f'<w:pict><v:shapetype id="_x0000_t202" coordsize="21600,21600" o:spt="202" path="m,l,21600r21600,l21600,xe">'
        f'<v:stroke joinstyle="miter"/><v:path gradientshapeok="t" o:connecttype="rect"/></v:shapetype>'
        f'<v:shape id="ZoneTexte{shape_id}" o:spid="_x0000_s{1024 + shape_id}" type="#_x0000_t202" '
        f'style="position:absolute;margin-left:{x_pt}pt;margin-top:{y_pt}pt;width:{w_pt}pt;height:{h_pt}pt;'
        f'z-index:{shape_id};mso-position-horizontal-relative:page;mso-position-vertical-relative:{rel_v}" '
        f'strokecolor="#{border}"><v:textbox inset="1.4pt,0,1.4pt,0"><w:txbxContent>{inner}</w:txbxContent>'
        f'</v:textbox><w10:wrap anchorx="page"/></v:shape></w:pict>')
    return (f'<w:r><mc:AlternateContent><mc:Choice Requires="wps">{choice}</mc:Choice>'
            f'<mc:Fallback>{fallback}</mc:Fallback></mc:AlternateContent></w:r>')


def make_letterhead_docx(path: Path, matricule: str = "99EM001", nom: str = "ETUDIANT FICTIF UN",
                         pages: int = 1) -> Path:
    """Relevé fictif dont l'en-tête reproduit les pièges des vrais relevés .doc (voir le module)."""
    doc = Document()
    sec = doc.sections[0]
    sec.header_distance = Pt(18)
    header = sec.header
    # 1) logo en ligne entre la colonne française et la colonne anglaise
    p0 = header.paragraphs[0]
    p0.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p0.add_run("UNIVERSITE DE DOUALA            ")
    r.font.size = Pt(9)
    p0.add_run().add_picture(io.BytesIO(_png()), width=Pt(40))
    r = p0.add_run("            THE UNIVERSITY OF DOUALA")
    r.font.size = Pt(9)
    hdr = header._element
    extra = (
        # 2) zone de texte flottante (taille fixe) portant la référence des services, en rouge
        f'<w:p {_DECL}><w:pPr><w:jc w:val="center"/></w:pPr>'
        + textbox(REF, 230, 82, 135, 12, 1)
        + _run(" ", 9) + '</w:p>'
        # 3) ligne de contact (puce en police symbole Wingdings + téléphone périmé), assez longue pour 2 lignes
        f'<w:p {_DECL}><w:pPr><w:jc w:val="center"/></w:pPr>'
        + _run("(", 9, font="Wingdings") + _run(" Tel : 33 01 41 39 / 33 01 41 29. B.P. 2701 Douala ", 9)
        + _run("*", 9, font="Wingdings")
        + _run(" courriel : faculte.fictive@example.org  site : www.example.org  BP fictive 0000", 9) + '</w:p>'
        # 4) titre français dans un cadre (w:framePr) à hauteur et interligne EXACTS, ancré à la page
        f'<w:p {_DECL}><w:pPr><w:framePr w:w="9000" w:h="280" w:hRule="exact" w:wrap="notBeside" '
        f'w:vAnchor="page" w:hAnchor="margin" w:xAlign="center" w:y="1980"/>'
        f'<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="exact"/><w:jc w:val="center"/></w:pPr>'
        + _run("FACULTE DE MEDECINE ET DES SCIENCES PHARMACEUTIQUES", 11, bold=True) + '</w:p>'
        # 5) titre anglais dans le flux, juste sous le cadre
        f'<w:p {_DECL}><w:pPr><w:spacing w:before="0" w:after="0" w:line="260" w:lineRule="exact"/>'
        f'<w:jc w:val="center"/></w:pPr>'
        + _run("FACULTY OF MEDICINE AND PHARMACEUTICAL SCIENCES", 11, bold=True) + '</w:p>'
        # 6) titre du relevé (espace après la barre, comme dans beaucoup de fichiers)
        f'<w:p {_DECL}><w:pPr><w:jc w:val="center"/></w:pPr>'
        + _run("RELEVE DE NOTES / TRANSCRIPT", 14, bold=True) + '</w:p>'
    )
    for frag in etree.fromstring(f"<root {_DECL}>{extra}</root>"):
        hdr.append(frag)
    for k in range(pages):
        if k:
            doc.add_page_break()
        mat = matricule if k == 0 else f"{matricule[:-3]}{int(matricule[-3:]) + k:03d}"
        doc.add_paragraph(f"NOM ET PRENOM: {nom} {k + 1}                MATRICULE : {mat}")
        doc.add_paragraph("NÉ(E) LE : 01/01/1990          A : DOUALA")
        doc.add_paragraph("NIVEAU: VII          ANNÉE ACADÉMIQUE: 2015 - 2016          FILIÈRE: MEDECINE")
        doc.add_paragraph("CYCLE : DOCTORAT          SEMESTRE: XIII")
        t = doc.add_table(rows=2, cols=6)
        for i, v in enumerate(("19", "PEDIATRIE", "14,50", "B+", "4", "VALIDEE")):
            t.cell(0, i).text = v
        for i, v in enumerate(("20", "CHIRURGIE", "12,40", "B", "4", "VALIDEE")):
            t.cell(1, i).text = v
    doc.save(str(path))
    return Path(path)
