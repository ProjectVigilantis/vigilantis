"""`docs/PROJECT_PROPOSAL.md` -> 제출 양식(.docx)을 채운 Connect Day 제출본 (이슈 #335)

    python scripts/build_proposal_docx.py

**이 스크립트가 저장소에 있는 이유** — SSOT는 "본문은 `docs/PROJECT_PROPOSAL.md`로
저장소에 두고 제출용 변환본을 거기서 낸다"고 정했다. 변환 방법이 한 사람의 PC에만
있으면 그 "거기서 낸다"를 그 사람만 할 수 있다. 담당자 부재 구간(2026-09-20~09-30)에
PM 검토 지적을 반영해야 할 수도 있어 절차를 저장소에 올린다.

**양식을 새로 만들지 않고 채운다.** `docs/templates/기획서_양식.docx`를 열어
표와 문단을 채우는 방식이라 판형·머리글·표 서식이 양식 그대로 유지된다.
**본문의 절 제목과 순서를 바꾸면 이 스크립트가 채울 자리를 못 찾는다.**

**필요한 것** — `python-docx` 하나다. 앱 의존성이 아니라 문서 도구라
`pyproject.toml`에 넣지 않았다: `pip install python-docx`

**구성도** — `docs/img/architecture.svg`가 원본이고, Word에는 래스터본
`docs/img/architecture.png`를 넣는다(python-docx가 SVG를 직접 넣지 못한다).
**SVG를 고치면 PNG도 다시 구워야 한다** — 브라우저에서 SVG를 캔버스에 3배로
그려 PNG로 내보내면 된다(pandoc·LibreOffice·cairosvg 없이 되는 방법이다).
"""
import os
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, 'docs', 'PROJECT_PROPOSAL.md')
TPL = os.path.join(REPO, 'docs', 'templates', '기획서_양식.docx')
PNG = os.path.join(REPO, 'docs', 'img', 'architecture.png')
MARKET_PNG = os.path.join(REPO, 'docs', 'img', 'market.png')
# 양식 표 1의 열 폭(dxa). 고정하지 않으면 자동 맞춤이 라벨 열을 1.3cm까지 눌러 버린다.
T1_GRID = [1696, 3731, 522, 1134, 1933]
OUT = os.path.join(REPO, 'docs', 'submission', 'Vigilantis_프로젝트_기획서.docx')

KO = '맑은 고딕'
MONO = 'Consolas'
NAVY = RGBColor(0x1B, 0x3A, 0x5C)
SLATE = RGBColor(0x43, 0x56, 0x6B)
HDR_FILL = 'D9D9D9'          # 양식이 쓰는 라벨 셀 배경
BODY_PT = 10.0               # 표 1 안쪽 본문
TBL_PT = 8.5                 # 중첩 표


# ───────────────────────── 서식 헬퍼 ─────────────────────────

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


def para_border(p, side='left', sz=18, color='8FA8C4'):
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


# ───────────────────────── 인라인 파싱 ─────────────────────────

TOKEN = re.compile(
    r'(?P<br><br\s*/?>)'
    r'|(?P<link>\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)\))'
    r'|(?P<code>`[^`\n]+`)'
    r'|(?P<bold>\*\*[^*\n]+\*\*)'
    r'|(?P<ital>(?<![\*\w])\*[^*\n]+\*(?!\*))'
)


def emit_inline(p, text, size=BODY_PT, base_bold=False, color=None):
    """마크다운 인라인을 run으로 푼다. `<br>`은 줄바꿈으로 바꾼다."""
    pos = 0
    for m in TOKEN.finditer(text):
        if m.start() > pos:
            r = p.add_run(text[pos:m.start()])
            set_font(r, KO, size, base_bold, None, color)
        if m.group('br'):
            p.add_run().add_break()
        elif m.group('link'):
            r = p.add_run(m.group('ltext').replace('`', ''))
            set_font(r, KO, size, base_bold, None, color)
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


# ───────────────────────── 마크다운 분해 ─────────────────────────

def load_sections():
    """`### 제목` 단위로 본문을 나눈다. 제목이 곧 양식의 채울 자리 이름이다."""
    raw = open(SRC, encoding='utf-8').read()
    raw = re.sub(r'^<!--.*?-->[ \t]*\n', '', raw, flags=re.M)
    raw = re.sub(r'^<!--\n.*?\n-->[ \t]*\n', '', raw, flags=re.S | re.M)
    assert '<!--' not in raw, 'HTML 주석이 남았다 — Word에서 사라지므로 본문이 될 수 없다'

    out, cur = {}, None
    for line in raw.split('\n'):
        m = re.match(r'^###\s+(.*)$', line)
        if m:
            cur = m.group(1).strip()
            out[cur] = []
            continue
        if re.match(r'^##?\s+', line):       # 장 제목·문서 제목은 버린다
            cur = None
            continue
        if cur is not None:
            out[cur].append(line)
    return {k: trim(v) for k, v in out.items()}


def trim(lines):
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def split_row(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


# ───────────────────────── 블록 렌더 ─────────────────────────

def add_para(container, after=None):
    """container(셀 또는 문서)에 문단을 만든다. after가 있으면 그 뒤에 끼운다."""
    if after is not None:
        new = OxmlElement('w:p')
        after._p.addnext(new)
        return Paragraph(new, after._parent)
    return container.add_paragraph()


def render(lines, container, after=None, size=BODY_PT, tbl_size=TBL_PT):
    """마크다운 블록을 container에 그린다. after를 주면 그 문단 뒤부터 이어 붙인다."""
    i, n = 0, len(lines)
    cursor = after
    while i < n:
        s = lines[i].strip()
        if not s or s == '---':
            i += 1
            continue

        # 표
        if s.startswith('|') and i + 1 < n and re.match(r'^\|[\s:|-]+\|$', lines[i + 1].strip()):
            rows = [split_row(s)]
            i += 2
            while i < n and lines[i].strip().startswith('|'):
                rows.append(split_row(lines[i].strip()))
                i += 1
            cursor = render_table(rows, container, cursor, tbl_size)
            continue

        # 인용문
        if s.startswith('>'):
            buf = []
            while i < n and lines[i].strip().startswith('>'):
                buf.append(lines[i].strip().lstrip('>').strip())
                i += 1
            while buf and not buf[-1]:
                buf.pop()
            p = add_para(container, cursor)
            p.paragraph_format.left_indent = Cm(0.35)
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(5)
            para_border(p, 'left')
            shade(p._p.get_or_add_pPr(), 'F4F7FA')
            for j, ln in enumerate(buf):
                if j:
                    p.add_run().add_break()
                emit_inline(p, ln, size=size - 0.5, color=SLATE)
            cursor = p
            continue

        # 불릿 / 번호
        bm = re.match(r'^([-*]|\d+\.)\s+(.*)$', s)
        if bm:
            while i < n:
                m2 = re.match(r'^([-*]|\d+\.)\s+(.*)$', lines[i].strip())
                if not m2:
                    break
                p = add_para(container, cursor)
                pf = p.paragraph_format
                pf.left_indent = Cm(0.55)
                pf.first_line_indent = Cm(-0.3)
                pf.space_after = Pt(2)
                marker = '• ' if m2.group(1) in ('-', '*') else m2.group(1) + ' '
                r = p.add_run(marker)
                set_font(r, KO, size, color=SLATE)
                emit_inline(p, m2.group(2), size=size)
                cursor = p
                i += 1
            continue

        # 일반 문단
        p = add_para(container, cursor)
        p.paragraph_format.space_after = Pt(4)
        emit_inline(p, s, size=size)
        cursor = p
        i += 1
    return cursor


def render_table(rows, container, after, size):
    header = rows[0]
    has_header = any(c.strip() for c in header)
    body = rows[1:]
    ncols = len(header)

    if after is not None:
        t = container.add_table(rows=0, cols=ncols)
        after._p.addnext(t._tbl)
    else:
        t = container.add_table(rows=0, cols=ncols)
    t.style = 'Table Grid'
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    w = OxmlElement('w:tblW')
    w.set(qn('w:w'), '5000')
    w.set(qn('w:type'), 'pct')
    t._tbl.tblPr.append(w)

    def fill(cell, text, is_head):
        if is_head:
            shade(cell._tc.get_or_add_tcPr(), HDR_FILL)
        p = cell.paragraphs[0]
        p.paragraph_format.space_before = Pt(1)
        p.paragraph_format.space_after = Pt(1)
        emit_inline(p, text, size=size, base_bold=is_head)

    if has_header:
        r = t.add_row()
        for ci, txt in enumerate(header):
            fill(r.cells[ci], txt, True)
        r._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
    for row in body:
        r = t.add_row()
        for ci in range(ncols):
            fill(r.cells[ci], row[ci] if ci < len(row) else '', False)

    # 표 뒤에 빈 문단을 둬서 다음 블록이 표에 붙지 않게 한다
    sp = OxmlElement('w:p')
    t._tbl.addnext(sp)
    gap = Paragraph(sp, t._parent)
    gap.paragraph_format.space_after = Pt(2)
    for r0 in gap.runs:
        set_font(r0, KO, 4)
    return gap


# ───────────────────────── 양식 채우기 ─────────────────────────

def cell_of(table, label):
    for row in table.rows:
        if row.cells[0].text.strip().replace('\n', '') == label.replace('\n', ''):
            return row.cells[1]
    raise KeyError(f'양식 표에 «{label}» 행이 없다')


def clear_cell(cell):
    for p in list(cell.paragraphs)[1:]:
        p._p.getparent().remove(p._p)
    for t in list(cell.tables):
        t._tbl.getparent().remove(t._tbl)
    p0 = cell.paragraphs[0]
    for r in list(p0.runs):
        r._r.getparent().remove(r._r)
    return p0


def fill_cell(cell, lines, size=BODY_PT):
    p0 = clear_cell(cell)
    cursor = render(lines, cell, after=None, size=size)
    # render가 새 문단을 add_paragraph로 만들었으므로 비어 있는 첫 문단을 지운다
    if not p0.text.strip() and len(cell.paragraphs) > 1:
        p0._p.getparent().remove(p0._p)
    return cursor


def find_para(doc, text):
    for p in doc.paragraphs:
        if p.text.strip() == text:
            return p
    raise KeyError(f'양식에 «{text}» 문단이 없다')


def drop_until(start_para, stop_texts):
    """start_para 바로 뒤부터 stop 문단 직전까지의 문단을 지운다(표를 만나면 멈춘다).

    ⚠️ 문단 텍스트를 lxml `itertext()`로 읽지 않는다 — python-docx가 `w:p`·`w:r`·`w:t`
    세 계층에서 `text`를 프로퍼티로 재정의해 두어 **같은 문자열이 3번 이어 붙어 나온다.**
    그러면 정지 문단이 한 번도 매치되지 않아 문서 끝까지 지워진다(2026-09-17 실측).
    """
    el = start_para._p.getnext()
    while el is not None:
        nxt = el.getnext()
        if el.tag == qn('w:p'):
            txt = Paragraph(el, start_para._parent).text.strip()
            if txt in stop_texts:
                break
            el.getparent().remove(el)
        elif el.tag == qn('w:tbl'):
            break
        el = nxt


DOC_TITLE = 'Vigilantis 프로젝트 기획서'


def fix_headers(doc):
    """모든 섹션의 머리글을 문서 제목으로 통일한다.

    ⚠️ 양식에는 머리글이 둘이고 **본문 섹션 쪽이 「Vigilantis화면설계서」**다
    (다른 문서에서 딸려 온 것 · 2026-09-17 실측). 표지만 맞고 본문 전 페이지에
    남의 문서 이름이 찍히므로 여기서 덮어쓴다.
    """
    for s in doc.sections:
        for hdr in (s.header, s.first_page_header, s.even_page_header):
            for p in hdr.paragraphs:
                if not p.text.strip():
                    continue
                for r in list(p.runs)[1:]:
                    r._r.getparent().remove(r._r)
                if p.runs:
                    p.runs[0].text = DOC_TITLE


def polish_layout(doc):
    """읽기 흐름을 위한 줄바꿈·페이지 규칙 (2026-09-18).

    - 기본 스타일 `a`: 양식이 「한글 단어 잘림 허용」(w:wordWrap=0)이라 '라면'이 '라/면'으로
      갈렸다. 단어 단위 줄바꿈(=1)으로 바꾸고, 과부/고아 줄 제어(widowControl)를 켠다.
    - 표 행은 페이지에 걸쳐 쪼개지지 않게 한다(cantSplit).
    - 「□ 소제목」·표 바로 앞 문단·그림 문단은 다음 블록과 같은 페이지에 붙인다(keepNext).
    """
    # 1) 기본 문단 스타일
    for st in doc.styles.element.findall(qn('w:style')):
        if st.get(qn('w:default')) == '1' and st.get(qn('w:type')) == 'paragraph':
            ppr = st.find(qn('w:pPr'))
            if ppr is None:
                ppr = OxmlElement('w:pPr')
                st.append(ppr)
            for tag in ('w:wordWrap', 'w:widowControl'):
                el = ppr.find(qn(tag))
                if el is None:
                    el = OxmlElement(tag)
                    ppr.insert(0, el)
                el.set(qn('w:val'), '1')

    body = doc.element.body
    outer = doc.tables[0]._tbl          # 양식 표 1 — 행 하나가 페이지보다 길다

    # 2) 표 행 분할 금지 — 양식 표 1의 행은 제외한다. 그 행(추진배경·주요 서비스…)은
    #    한 페이지를 넘기므로 cantSplit을 걸면 Word가 행 전체를 다음 페이지로 밀어
    #    앞 페이지 절반이 빈다. 그 안의 중첩 표와 나머지 표만 건다.
    for tr in body.iter(qn('w:tr')):
        if tr.getparent() is outer:
            continue
        trpr = tr.find(qn('w:trPr'))
        if trpr is None:
            trpr = OxmlElement('w:trPr')
            tr.insert(0, trpr)
        if trpr.find(qn('w:cantSplit')) is None:
            trpr.insert(0, OxmlElement('w:cantSplit'))

    # 2-B) 유사 서비스 비교 표(양식 표 2)는 페이지에 걸치므로 머리행을 반복한다.
    #    인덱스로 찾지 않는다 — render()가 앞에 표를 끼워 넣어 doc.tables 순서가 밀린다.
    cmp_tbl = next(t for t in doc.tables if t.rows[0].cells[0].text.strip() == '비교 항목')
    tr0 = cmp_tbl._tbl.find(qn('w:tr'))
    if tr0 is not None:
        trpr = tr0.find(qn('w:trPr'))
        if trpr is None:
            trpr = OxmlElement('w:trPr')
            tr0.insert(0, trpr)
        if trpr.find(qn('w:tblHeader')) is None:
            trpr.append(OxmlElement('w:tblHeader'))

    # 3) keepNext
    def keep_next(p_el):
        ppr = p_el.find(qn('w:pPr'))
        if ppr is None:
            ppr = OxmlElement('w:pPr')
            p_el.insert(0, ppr)
        if ppr.find(qn('w:keepNext')) is None:
            ppr.insert(0, OxmlElement('w:keepNext'))

    for p_el in body.iter(qn('w:p')):
        text = ''.join(t.text or '' for t in p_el.iter(qn('w:t'))).strip()
        nxt = p_el.getnext()
        if text.startswith('□') or text.startswith('['):
            keep_next(p_el)
        elif re.match(r'^(\d+|[a-zA-Z])\.\s\S', text) and len(text) <= 40:   # 「1. SWOT 매트릭스」·「c. WO 전략 …」류
            keep_next(p_el)
        elif nxt is not None and nxt.tag == qn('w:tbl'):
            keep_next(p_el)
        elif p_el.find('.//' + qn('w:drawing')) is not None:
            keep_next(p_el)

    # 3-B) 머리행이 페이지 끝에 혼자 남지 않게 — 모든 표(양식 표 1 제외)의 첫 행 문단을 다음 행과 묶는다.
    #      양식의 SWOT·STP 표는 tblHeader가 없어 "tblHeader 있는 행"만 잡으면 빠진다(v6에서 SWOT 머리행 고아).
    for tbl in body.iter(qn('w:tbl')):
        if tbl is outer:
            continue
        tr0 = tbl.find(qn('w:tr'))
        if tr0 is None:
            continue
        for p_el in tr0.iter(qn('w:p')):
            keep_next(p_el)
        # 양식 SWOT(첫 셀 '내부')·STP(첫 셀 '구분') 표는 쪽에 걸치면 머리행을 반복하게 한다
        first = ''.join(t.text or '' for t in tr0.iter(qn('w:t'))).strip()
        if first.startswith(('내부', '구분')):      # 첫 행 텍스트는 셀들이 이어 붙는다('내부강점(S)약점(W)')
            trpr = tr0.find(qn('w:trPr'))
            if trpr is None:
                trpr = OxmlElement('w:trPr')
                tr0.insert(0, trpr)
            if trpr.find(qn('w:tblHeader')) is None:
                trpr.append(OxmlElement('w:tblHeader'))

    # 4) 양식 표 1 셀의 마지막 빈 문단(표 뒤 간격용)이 다음 페이지로 넘어가면
    #    빈 셀 조각이 한 줄 생긴다. 지울 수는 없으므로(셀은 문단으로 끝나야 한다) 1pt로 줄인다.
    def shrink(p_el):
        ppr = p_el.find(qn('w:pPr'))
        if ppr is None:
            ppr = OxmlElement('w:pPr')
            p_el.insert(0, ppr)
        for old in ppr.findall(qn('w:spacing')):
            ppr.remove(old)
        sp = OxmlElement('w:spacing')
        sp.set(qn('w:before'), '0'); sp.set(qn('w:after'), '0')
        sp.set(qn('w:line'), '20'); sp.set(qn('w:lineRule'), 'exact')
        ppr.append(sp)
        rpr = OxmlElement('w:rPr')
        sz = OxmlElement('w:sz'); sz.set(qn('w:val'), '2')
        rpr.append(sz)
        ppr.append(rpr)

    def is_empty_p(el):
        return el.tag == qn('w:p') and not ''.join(t.text or '' for t in el.iter(qn('w:t'))).strip() \
            and el.find('.//' + qn('w:drawing')) is None

    for tc in outer.iter(qn('w:tc')):
        if tc.getparent().getparent() is not outer:
            continue
        # 셀 끝의 빈 문단은 둘일 수 있다 — render_table의 간격 문단 + python-docx가 표 뒤에 붙이는 문단
        for el in reversed(list(tc)):
            if not is_empty_p(el):
                break
            shrink(el)

    # 5) 유사 서비스 비교 표(양식 표 2)는 반 쪽짜리라 페이지에 걸치면 머리행 없이 이어진다.
    #    마지막 행 빼고 전부 keepNext → 표가 통째로 다음 쪽으로 간다.
    t2_rows = cmp_tbl._tbl.findall(qn('w:tr'))
    for tr in t2_rows[:-1]:
        for p_el in tr.iter(qn('w:p')):
            keep_next(p_el)

    # 6) 문서 마지막 「□ …」 블록은 통째로 붙인다 — 끝 문단 두 줄만 다음 쪽에 남는 것을 막는다
    tops = [el for el in body if el.tag in (qn('w:p'), qn('w:tbl'))]
    last_box = None
    for i, el in enumerate(tops):
        if el.tag == qn('w:p') and ''.join(t.text or '' for t in el.iter(qn('w:t'))).strip().startswith('□'):
            last_box = i
    if last_box is not None:
        for el in tops[last_box:-1]:
            if el.tag == qn('w:p'):
                keep_next(el)


def build():
    sec = load_sections()
    doc = Document(TPL)
    fix_headers(doc)

    # ── 표지 날짜
    for p in doc.paragraphs[:6]:
        if p.text.strip() == '2026.--.--':
            for r in p.runs:
                r.text = ''
            p.runs[0].text = '2026. 10. 01.'

    t1, t2, t3, t4 = doc.tables[:4]
    lock_widths(t1, T1_GRID)

    # ── 표 1 · 프로젝트 주제
    fill_cell(cell_of(t1, '프로젝트 주제'), sec['프로젝트 주제'])

    # ── 표 1 · 팀원 (양식은 5행, 현재 4인이라 마지막 행을 지운다)
    team_rows = [r for r in t1.rows if r.cells[0].text.strip() == '팀원']
    team_md = [l for l in sec['팀 구성'] if l.strip().startswith('|')]
    body = [split_row(l) for l in team_md if not re.match(r'^\|[\s:|-]+\|$', l.strip())][1:]
    for row, person in zip(team_rows[1:], body):          # team_rows[0] = 머리행(이름/역할)
        clear_cell(row.cells[1]).add_run()
        emit_inline(row.cells[1].paragraphs[0], person[0], size=BODY_PT, base_bold=True)
        c2 = clear_cell(row.cells[2])
        emit_inline(c2, person[1], size=BODY_PT)
        p2 = row.cells[2].add_paragraph()
        emit_inline(p2, person[2], size=BODY_PT - 1.5, color=SLATE)
    for extra in team_rows[1 + len(body):]:
        extra._tr.getparent().remove(extra._tr)

    # ── 표 1 · 서술 행
    for label, key in [
        ('추진배경 및\n필요성', '추진배경 및 필요성'),
        ('목적 및\n핵심 가치', '목적 및 핵심 가치'),
        ('주요 서비스', '주요 서비스'),
        ('주요 기술', '주요 기술'),
        ('기대효과', '기대효과'),
        ('활용방안 및\n비즈니스 모델\n(BM)', '활용방안 및 비즈니스 모델(BM)'),
    ]:
        cur = fill_cell(cell_of(t1, label), sec[key])
        if key == '주요 기술':
            # 시스템 구성도는 기술 설명이 끝난 자리에 둔다
            pic = add_para(None, cur)
            pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pic.add_run().add_picture(PNG, width=Cm(12.2))
            cap = add_para(None, pic)
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            set_font(cap.add_run('[그림 2] 시스템 구성도 — 가드레일 통과는 실행이 아니라 승인 대기로 간다'),
                     KO, 8.5, color=SLATE)

    # ── §2 시장 현황 — 양식이 「사진 및 설명 추가」를 요구하는 자리다.
    #    시스템 구성도가 아니라 **시장 그림**이 와야 한다(구성도는 §주요 기술에 있다).
    anchor = find_para(doc, '[시장 현황]')
    drop_until(anchor, {'□ 유사 서비스 분석 및 비교'})
    pic = add_para(doc, anchor)
    pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pic.add_run().add_picture(MARKET_PNG, width=Cm(15.5))
    cap = add_para(doc, pic)
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font(cap.add_run('[그림 1] 국내 클라우드 시장 규모와 성장률 — 우리가 겨냥한 구간이 시장 평균보다 빠르다'),
             KO, 9, color=SLATE)
    render(sec['시장 현황'], doc, after=cap)

    # ── §2 유사 서비스 비교 표
    cmp_md = [l for l in sec['유사 서비스 분석 및 비교'] if l.strip().startswith('|')]
    cmp_rows = [split_row(l) for l in cmp_md if not re.match(r'^\|[\s:|-]+\|$', l.strip())]
    refill_grid(t2, cmp_rows)
    tail = [l for l in sec['유사 서비스 분석 및 비교'] if not l.strip().startswith('|')]
    anchor = find_para(doc, '[기존 서비스 분석 요약]')
    drop_until(anchor, {'□ SWOT 분석'})
    render(trim([l for l in tail if '기존 서비스 분석 요약' not in l]), doc, after=anchor)

    # ── §2 SWOT 매트릭스 (양식 4행 × 3열 구조에 맞춘다)
    swot_md = [l for l in sec['SWOT 분석'] if l.strip().startswith('|')]
    swot = [split_row(l) for l in swot_md if not re.match(r'^\|[\s:|-]+\|$', l.strip())]
    # swot[0]=머리, swot[1]=내부, swot[2]=외부
    grid = [['구분', swot[0][1], swot[0][2]],
            ['내부', swot[1][1], swot[1][2]],
            ['외부', swot[2][1], swot[2][2]]]
    refill_grid(t3, grid, label_col=True)

    # ── §2 SWOT 교차 전략
    anchor = find_para(doc, '2. SWOT 교차 분석 기반 실행 전략')
    drop_until(anchor, {'□ STP 전략 수립'})
    cross = []
    keep = False
    for l in sec['SWOT 분석']:
        if l.strip().startswith('**a. SO'):
            keep = True
        if keep:
            cross.append(l)
    render(trim(cross), doc, after=anchor)

    # ── §2 STP
    stp_md = [l for l in sec['STP 전략 수립'] if l.strip().startswith('|')]
    stp = [split_row(l) for l in stp_md if not re.match(r'^\|[\s:|-]+\|$', l.strip())]
    refill_grid(t4, stp, label_col=True)

    # ── §2 추진 일정 (양식에 표가 없어 새로 만든다)
    anchor = find_para(doc, '□ 추진 일정')
    drop_until(anchor, {'□ 차별성'})
    render(sec['추진 일정'], doc, after=anchor, tbl_size=8.0)

    # ── §2 차별성
    anchor = find_para(doc, '□ 차별성')
    drop_until(anchor, set())
    render(sec['차별성'], doc, after=anchor)

    polish_layout(doc)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    doc.save(OUT)
    return OUT


def lock_widths(table, grid_dxa):
    """열 폭을 dxa로 못 박는다. 자동 맞춤에 맡기면 라벨 열이 글자 폭까지 눌린다."""
    tblPr = table._tbl.tblPr
    for tag in ('w:tblLayout', 'w:tblW'):
        old = tblPr.find(qn(tag))
        if old is not None:
            tblPr.remove(old)
    lay = OxmlElement('w:tblLayout')
    lay.set(qn('w:type'), 'fixed')
    tblPr.append(lay)
    w = OxmlElement('w:tblW')
    w.set(qn('w:w'), str(sum(grid_dxa)))
    w.set(qn('w:type'), 'dxa')
    tblPr.append(w)
    table.autofit = False
    for row in table.rows:
        seen = 0
        # ⚠️ `row.cells`를 쓰면 안 된다 — 병합 셀을 gridSpan 수만큼 **반복**해 돌려주므로
        # `seen`이 그리드를 넘어가고 폭이 0dxa로 박힌다. 원소 목록을 직접 돈다.
        for tc in row._tr.findall(qn('w:tc')):
            pr = tc.get_or_add_tcPr()
            gs = pr.find(qn('w:gridSpan'))
            span = int(gs.get(qn('w:val'))) if gs is not None else 1
            width = sum(grid_dxa[seen:seen + span])
            assert width, f'열 폭 0 — seen={seen} span={span}'
            el = pr.get_or_add_tcW()   # 스키마 순서를 python-docx가 지켜 넣는다
            el.set(qn('w:w'), str(width))
            el.set(qn('w:type'), 'dxa')
            seen += span


def refill_grid(table, rows, label_col=False):
    """양식 표의 행 수를 내용에 맞추고 다시 채운다."""
    while len(table.rows) > len(rows):
        table.rows[-1]._tr.getparent().remove(table.rows[-1]._tr)
    while len(table.rows) < len(rows):
        table.add_row()
    for ri, row in enumerate(rows):
        for ci in range(len(table.columns)):
            cell = table.cell(ri, ci)
            p = clear_cell(cell)
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(1)
            head = ri == 0 or (label_col and ci == 0)
            if head:
                shade(cell._tc.get_or_add_tcPr(), HDR_FILL)
            emit_inline(p, row[ci] if ci < len(row) else '', size=TBL_PT, base_bold=head)


if __name__ == '__main__':
    path = build()
    print('saved', path, os.path.getsize(path), 'bytes')
