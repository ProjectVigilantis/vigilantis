"""`docs/PROJECT_PROPOSAL.md` -> Connect Day 제출용 .docx (이슈 #335)

    python scripts/build_proposal_docx.py

**이 스크립트가 저장소에 있는 이유** — SSOT는 "본문은 `docs/PROJECT_PROPOSAL.md`로
저장소에 두고 제출용 변환본을 거기서 낸다"고 정했다. 변환 방법이 한 사람의 PC에만
있으면 그 "거기서 낸다"를 그 사람만 할 수 있다. 담당자 부재 구간(2026-09-20~09-30)에
PM 검토 지적을 반영해야 할 수도 있어 절차를 저장소에 올린다.

**필요한 것** — `python-docx` 하나다. 앱 의존성이 아니라 문서 도구라 `pyproject.toml`에
넣지 않았다: `pip install python-docx`

**하는 일**

- HTML 주석 제거 — Word·Confluence는 주석을 지워 버리므로 본문이 될 수 없다
- 상대 링크 -> GitHub `dev` 절대 URL (Word에서 클릭되게)
- `§3.1` 구성도는 `docs/img/architecture.svg`가 원본이고, Word에는 그 래스터본
  `docs/img/architecture.png`를 넣는다(python-docx가 SVG를 직접 넣지 못한다).
  **SVG를 고치면 PNG도 다시 구워야 한다** — 브라우저에서 SVG를 캔버스에 3배로 그려
  PNG로 내보내면 된다(pandoc·LibreOffice·cairosvg 없이 되는 방법이다).
- 표는 머리글 행이 비어 있으면 그 행을 버린다(남기면 빈 남색 띠가 된다)
- 4열 이상 표는 열별 내용 길이의 제곱근으로 폭을 나눈다(자동 맞춤이 긴 열을 눌러서)
"""
import os
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, 'docs', 'PROJECT_PROPOSAL.md')
PNG = os.path.join(REPO, 'docs', 'img', 'architecture.png')
OUT = os.path.join(REPO, 'docs', 'submission', 'Vigilantis_프로젝트_기획서.docx')
GH = 'https://github.com/ProjectVigilantis/vigilantis/blob/dev/'

KO = 'Malgun Gothic'
MONO = 'Consolas'
NAVY = RGBColor(0x1B, 0x3A, 0x5C)
SLATE = RGBColor(0x43, 0x56, 0x6B)
LINK = RGBColor(0x1F, 0x5C, 0xA8)


# ---------- 공통 헬퍼 ----------

def set_font(run, name=KO, size=None, bold=None, italic=None, color=None):
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn('w:rFonts'))
    if rf is None:
        rf = OxmlElement('w:rFonts')
        rpr.insert(0, rf)
    for attr in ('w:ascii', 'w:hAnsi', 'w:eastAsia', 'w:cs'):
        rf.set(qn(attr), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color


def shade(el, hex_fill):
    sh = OxmlElement('w:shd')
    sh.set(qn('w:val'), 'clear')
    sh.set(qn('w:color'), 'auto')
    sh.set(qn('w:fill'), hex_fill)
    el.append(sh)


def para_border(p, side='bottom', sz=6, color='C8D4E3'):
    pPr = p._p.get_or_add_pPr()
    bd = pPr.find(qn('w:pBdr'))
    if bd is None:
        bd = OxmlElement('w:pBdr')
        pPr.append(bd)
    e = OxmlElement('w:' + side)
    e.set(qn('w:val'), 'single')
    e.set(qn('w:sz'), str(sz))
    e.set(qn('w:space'), '4')
    e.set(qn('w:color'), color)
    bd.append(e)


def add_hyperlink(paragraph, url, segments):
    part = paragraph.part
    r_id = part.relate_to(
        url,
        'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink',
        is_external=True,
    )
    h = OxmlElement('w:hyperlink')
    h.set(qn('r:id'), r_id)
    for text, style in segments:
        r = OxmlElement('w:r')
        rPr = OxmlElement('w:rPr')
        rf = OxmlElement('w:rFonts')
        name = MONO if style.get('code') else KO
        for a in ('w:ascii', 'w:hAnsi', 'w:eastAsia', 'w:cs'):
            rf.set(qn(a), name)
        rPr.append(rf)
        if style.get('bold'):
            rPr.append(OxmlElement('w:b'))
        u = OxmlElement('w:u')
        u.set(qn('w:val'), 'single')
        rPr.append(u)
        c = OxmlElement('w:color')
        c.set(qn('w:val'), '1F5CA8')
        rPr.append(c)
        sz = OxmlElement('w:sz')
        sz.set(qn('w:val'), str(int(style.get('size', 10) * 2)))
        rPr.append(sz)
        r.append(rPr)
        t = OxmlElement('w:t')
        t.set(qn('xml:space'), 'preserve')
        t.text = text
        r.append(t)
        h.append(r)
    paragraph._p.append(h)


# ---------- 인라인 파싱 ----------

TOKEN = re.compile(
    r'(?P<link>\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)\))'
    r'|(?P<code>`[^`\n]+`)'
    r'|(?P<bold>\*\*[^*\n]+\*\*)'
    r'|(?P<ital>(?<![\*\w])\*[^*\n]+\*(?!\*))'
)


def abs_url(u):
    if u.startswith('http'):
        return u
    if u.startswith('../'):
        return GH + u[3:]
    return GH + 'docs/' + u


def emit_inline(p, text, size=10, base_bold=False, color=None):
    """마크다운 인라인을 run으로 푼다. 링크는 하이퍼링크로."""
    pos = 0
    for m in TOKEN.finditer(text):
        if m.start() > pos:
            r = p.add_run(text[pos:m.start()])
            set_font(r, KO, size, base_bold, None, color)
        if m.group('link'):
            inner = m.group('ltext')
            url = abs_url(m.group('lurl'))
            segs = []
            ipos = 0
            for im in re.finditer(r'`([^`]+)`|\*\*([^*]+)\*\*', inner):
                if im.start() > ipos:
                    segs.append((inner[ipos:im.start()], {'size': size}))
                if im.group(1):
                    segs.append((im.group(1), {'code': True, 'size': size}))
                else:
                    segs.append((im.group(2), {'bold': True, 'size': size}))
                ipos = im.end()
            if ipos < len(inner):
                segs.append((inner[ipos:], {'size': size}))
            add_hyperlink(p, url, segs or [(inner, {'size': size})])
        elif m.group('code'):
            r = p.add_run(m.group('code')[1:-1])
            set_font(r, MONO, size - 0.5, base_bold, None, RGBColor(0x8A, 0x2E, 0x4E))
        elif m.group('bold'):
            r = p.add_run(m.group('bold')[2:-2])
            set_font(r, KO, size, True, None, color)
        else:
            r = p.add_run(m.group('ital')[1:-1])
            set_font(r, KO, size, base_bold, True, color)
        pos = m.end()
    if pos < len(text):
        r = p.add_run(text[pos:])
        set_font(r, KO, size, base_bold, None, color)


# ---------- 문서 조립 ----------

def heading(doc, level, text):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    sizes = {1: 22, 2: 16, 3: 12.5, 4: 11}
    pf.space_before = Pt({1: 0, 2: 20, 3: 14, 4: 10}[level])
    pf.space_after = Pt({1: 10, 2: 8, 3: 6, 4: 4}[level])
    pf.keep_with_next = True
    emit_inline(p, text, size=sizes[level], base_bold=True, color=NAVY)
    if level == 2:
        para_border(p, 'bottom', sz=10, color='1B3A5C')
    return p


def quote(doc, lines):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.left_indent = Cm(0.5)
    pf.space_before = Pt(4)
    pf.space_after = Pt(6)
    para_border(p, 'left', sz=18, color='8FA8C4')
    shade(p._p.get_or_add_pPr(), 'F4F7FA')
    for i, ln in enumerate(lines):
        if i:
            p.add_run().add_break()
        emit_inline(p, ln, size=9.5, color=SLATE)
    return p


USABLE_CM = 17.4  # A4 21cm - 좌우 여백 1.8cm*2


def _vis_len(s):
    """마크다운 기호·URL을 뺀 표시 길이. 전각은 2로 센다."""
    s = re.sub(r'\]\([^)]*\)', ']', s)
    s = re.sub(r'[*`\[\]]', '', s)
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def _col_widths(rows, ncols):
    """열별 최대 표시 길이의 제곱근으로 가중(극단값을 눌러 준다)."""
    import math
    weights = []
    for ci in range(ncols):
        mx = max((_vis_len(r[ci]) for r in rows if ci < len(r)), default=1)
        weights.append(math.sqrt(max(mx, 4)))
    total = sum(weights)
    raw = [USABLE_CM * w / total for w in weights]
    # 최소 1.5cm 보장 후 나머지에서 비례 회수
    MIN = 1.5
    short = [i for i, v in enumerate(raw) if v < MIN]
    if short:
        deficit = sum(MIN - raw[i] for i in short)
        pool = sum(raw[i] - MIN for i in range(ncols) if i not in short)
        for i in range(ncols):
            raw[i] = MIN if i in short else raw[i] - (raw[i] - MIN) * deficit / pool
    return [Cm(v) for v in raw]


def table(doc, rows):
    header = rows[0]
    # 머리글 칸이 전부 비어 있으면 그 행을 아예 버린다
    # (헤더로 쓰면 빈 남색 띠가 되고, 본문 행으로 남기면 빈 줄이 된다)
    has_header = any(c.strip() for c in header)
    body = rows[1:]
    ncols = len(header)

    t = doc.add_table(rows=len(body) + (1 if has_header else 0), cols=ncols)
    t.style = 'Table Grid'
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    tblPr = t._tbl.tblPr
    lay = OxmlElement('w:tblLayout')
    wide = ncols >= 4
    lay.set(qn('w:type'), 'fixed' if wide else 'autofit')
    tblPr.append(lay)
    w = OxmlElement('w:tblW')
    w.set(qn('w:w'), '5000')
    w.set(qn('w:type'), 'pct')
    tblPr.append(w)

    widths = _col_widths(rows, ncols) if wide else None
    if widths:
        t.autofit = False
        for ci, cw in enumerate(widths):
            for cell in t.columns[ci].cells:
                cell.width = cw

    def fill(cell, text, is_head):
        if is_head:
            shade(cell._tc.get_or_add_tcPr(), '1B3A5C')
        p = cell.paragraphs[0]
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        emit_inline(p, text, size=9, base_bold=is_head,
                    color=RGBColor(0xFF, 0xFF, 0xFF) if is_head else None)

    r0 = 0
    if has_header:
        for ci, cell_text in enumerate(header):
            fill(t.cell(0, ci), cell_text, True)
        # 헤더 행을 페이지마다 반복
        t.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
        r0 = 1
    for ri, row in enumerate(body):
        for ci in range(ncols):
            cell = t.cell(ri + r0, ci)
            if ri % 2 == 1:
                shade(cell._tc.get_or_add_tcPr(), 'F4F7FA')
            fill(cell, row[ci] if ci < len(row) else '', False)

    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t


def split_row(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def build():
    raw = open(SRC, encoding='utf-8').read()
    raw = re.sub(r'^<!--.*?-->[ \t]*\n', '', raw, flags=re.M)
    raw = re.sub(r'^<!--\n.*?\n-->[ \t]*\n', '', raw, flags=re.S | re.M)
    assert '<!--' not in raw, 'HTML 주석이 남았다'
    lines = raw.split('\n')

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.top_margin = sec.bottom_margin = Cm(2.0)
    sec.left_margin = sec.right_margin = Cm(1.8)

    st = doc.styles['Normal']
    st.font.name = KO
    st.font.size = Pt(10)
    st.element.rPr.rFonts.set(qn('w:eastAsia'), KO)
    st.paragraph_format.space_after = Pt(6)
    st.paragraph_format.line_spacing = 1.15

    # 쪽 번호 (바닥글 가운데)
    footer_p = sec.footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for instr in ('PAGE', ):
        fld = OxmlElement('w:fldSimple')
        fld.set(qn('w:instr'), instr)
        footer_p._p.append(fld)
    for r in footer_p.runs:
        set_font(r, KO, 9, color=SLATE)

    i, n = 0, len(lines)
    first_chapter = True
    while i < n:
        line = lines[i]
        s = line.strip()

        if not s:
            i += 1
            continue

        if s == '---':
            i += 1
            continue

        m = re.match(r'^(#{1,4})\s+(.*)$', s)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
            if level == 2:
                if first_chapter:
                    first_chapter = False
                else:
                    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            heading(doc, level, text)
            i += 1
            continue

        if s.startswith('!['):
            im = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', s)
            if im:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.add_run().add_picture(PNG, width=Cm(17.0))
                cap = doc.add_paragraph()
                cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                r = cap.add_run(im.group(1))
                set_font(r, KO, 9, color=SLATE)
                i += 1
                continue

        if s.startswith('|') and i + 1 < n and re.match(r'^\|[\s:|-]+\|$', lines[i + 1].strip()):
            rows = [split_row(s)]
            i += 2
            while i < n and lines[i].strip().startswith('|'):
                rows.append(split_row(lines[i].strip()))
                i += 1
            table(doc, rows)
            continue

        if s.startswith('>'):
            buf = []
            while i < n and lines[i].strip().startswith('>'):
                buf.append(lines[i].strip().lstrip('>').strip())
                i += 1
            while buf and not buf[-1]:
                buf.pop()
            quote(doc, buf)
            continue

        bm = re.match(r'^[-*]\s+(.*)$', s)
        if bm:
            while i < n and re.match(r'^[-*]\s+', lines[i].strip()):
                item = re.match(r'^[-*]\s+(.*)$', lines[i].strip()).group(1)
                p = doc.add_paragraph(style='List Bullet')
                p.paragraph_format.space_after = Pt(3)
                emit_inline(p, item, size=10)
                i += 1
            continue

        nm = re.match(r'^\d+\.\s+(.*)$', s)
        if nm:
            while i < n and re.match(r'^\d+\.\s+', lines[i].strip()):
                item = re.match(r'^\d+\.\s+(.*)$', lines[i].strip()).group(1)
                p = doc.add_paragraph(style='List Number')
                p.paragraph_format.space_after = Pt(3)
                emit_inline(p, item, size=10)
                i += 1
            continue

        p = doc.add_paragraph()
        emit_inline(p, s, size=10)
        i += 1

    doc.save(OUT)
    return OUT


if __name__ == '__main__':
    path = build()
    print('saved', path, os.path.getsize(path), 'bytes')
