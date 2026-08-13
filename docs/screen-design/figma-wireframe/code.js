/**
 * Vigilantis 저충실도 와이어프레임 생성기 (Figma 플러그인)
 *
 * 기준: docs/screen-design/screen-design-v1.2.md 4장.
 * 화면 구조를 새로 정하지 않는다. 문서에 있는 것만 옮긴다.
 *
 * 빌드 단계가 없다. 순수 JS 한 파일이라 Figma에서 manifest만 불러오면 바로 돈다.
 * 저충실도 원칙: 회색조만 쓰고, 색으로 의미를 전달하지 않는다.
 */

/* ─────────────── 색 (회색조 전용) ─────────────── */

const rgb = (n) => ({ r: n / 255, g: n / 255, b: n / 255 });
const C = {
  paper: rgb(255),
  frame: rgb(190),
  bar: rgb(244),
  box: rgb(240),
  boxStrong: rgb(226),
  line: rgb(208),
  ink: rgb(51),
  mid: rgb(122),
  faint: rgb(160),
};
const fill = (c) => [{ type: "SOLID", color: c }];

/* ─────────────── 폰트 ─────────────── */

// 한글 라벨이 본문이라 한글 글리프가 있는 폰트를 먼저 찾는다.
// macOS는 폰트 패밀리·스타일 이름을 한글로 로컬라이즈해서 내주는 경우가 있어
// ("Apple SD 산돌고딕 Neo", "볼드체") 영문 이름만 비교하면 놓친다. 정규식으로 받는다.
const FONT_PREFERENCE = [
  /^pretendard/i,
  /noto\s*sans\s*kr/i,
  /apple\s*sd/i,
  /산돌고딕/,
  /spoqa/i,
  /^inter$/i,
  /^roboto$/i,
];

// 로컬라이즈된 스타일 이름까지 받는다. 못 찾으면 목록의 첫 스타일로 떨어진다.
const STYLE_REGULAR = [/^regular$/i, /^일반체?$/, /^레귤러$/, /^book$/i];
const STYLE_BOLD = [
  /^bold$/i, /^볼드체?$/,
  /^semi\s?bold$/i, /^세미\s?볼드체?$/,
  /^medium$/i, /^미디엄$/,
];

let FONT = { family: "Inter", regular: "Regular", bold: "Bold" };

function matchStyle(styles, patterns) {
  for (const p of patterns) {
    const hit = styles.find((s) => p.test(s));
    if (hit) return hit;
  }
  return null;
}

async function pickFont() {
  const available = await figma.listAvailableFontsAsync();
  const byFamily = new Map();
  for (const f of available) {
    const fam = f.fontName.family;
    if (!byFamily.has(fam)) byFamily.set(fam, []);
    byFamily.get(fam).push(f.fontName.style);
  }

  const families = Array.from(byFamily.keys());
  for (const pattern of FONT_PREFERENCE) {
    const family = families.find((f) => pattern.test(f));
    if (!family) continue;
    const styles = byFamily.get(family);
    const regular = matchStyle(styles, STYLE_REGULAR) || styles[0];
    FONT = {
      family: family,
      regular: regular,
      bold: matchStyle(styles, STYLE_BOLD) || regular,
    };
    break;
  }

  await figma.loadFontAsync({ family: FONT.family, style: FONT.regular });
  if (FONT.bold !== FONT.regular) {
    await figma.loadFontAsync({ family: FONT.family, style: FONT.bold });
  }
}

/* ─────────────── 스펙 DSL ─────────────── */
// 화면을 데이터로 적고 렌더러 하나가 그린다. 9개 화면을 손으로 그리는 것보다
// 짧고, 레이아웃을 고칠 때 한 군데만 만진다.

const col = (p, children) => Object.assign({ t: "col", children: children || [] }, p);
const row = (p, children) => Object.assign({ t: "row", children: children || [] }, p);
const txt = (s, p) => Object.assign({ t: "text", s: s }, p);
const box = (p) => Object.assign({ t: "box" }, p);
const gap = () => ({ t: "row", grow: 1, children: [] });
const rule = () => ({ t: "box", h: 1, stretch: true, plain: true });

/* ─────────────── 프로토타입 링크 ─────────────── */
// 스펙에 link: "KEY"를 달면 그 노드가 해당 화면으로 가는 핫스팟이 된다.
// 화면 프레임이 다 만들어진 뒤에 KEY를 실제 노드 id로 바꿔 연결한다.

const LINKS = [];

/* ─────────────── 렌더러 ─────────────── */

function makeText(node) {
  const t = figma.createText();
  // fontName은 characters보다 먼저 지정해야 한다
  t.fontName = { family: FONT.family, style: node.bold ? FONT.bold : FONT.regular };
  t.characters = node.s;
  t.fontSize = node.size || 15;
  t.fills = fill(node.color || C.ink);
  if (node.w) {
    t.textAutoResize = "HEIGHT";
    t.resize(node.w, t.height);
  }
  return t;
}

function makeBox(node) {
  const f = figma.createFrame();
  f.name = node.name || (node.label ? "box: " + node.label : "box");
  f.layoutMode = "HORIZONTAL";
  f.primaryAxisAlignItems = "CENTER";
  f.counterAxisAlignItems = "CENTER";
  f.primaryAxisSizingMode = "FIXED";
  f.counterAxisSizingMode = "FIXED";
  f.fills = fill(node.plain ? C.line : node.strong ? C.boxStrong : C.box);
  if (!node.plain) {
    f.strokes = fill(C.line);
    f.strokeWeight = 1;
  }
  f.cornerRadius = node.plain ? 0 : 3;
  if (node.label) f.appendChild(makeText({ s: node.label, size: node.size || 14, color: C.mid }));
  f.resize(Math.max(node.w || 80, 1), Math.max(node.h || 24, 1));
  return f;
}

/**
 * 부모가 "채우라"고 지시한 축은 반드시 FIXED여야 한다.
 * AUTO(hug)로 두면 Figma가 자식을 0으로 접는다 — 프레임이 통째로 사라지는 원인.
 */
function setAxisFixed(f, vertical, axis) {
  if (axis === "w") {
    if (vertical) f.counterAxisSizingMode = "FIXED";
    else f.primaryAxisSizingMode = "FIXED";
  } else {
    if (vertical) f.primaryAxisSizingMode = "FIXED";
    else f.counterAxisSizingMode = "FIXED";
  }
}

function makeGroupFrame(node, parentVertical) {
  const f = figma.createFrame();
  const vertical = node.t === "col";
  f.name = node.name || node.t;
  f.layoutMode = vertical ? "VERTICAL" : "HORIZONTAL";
  f.itemSpacing = node.gap || 0;
  const padX = node.padX != null ? node.padX : node.pad || 0;
  const padY = node.padY != null ? node.padY : node.pad || 0;
  f.paddingLeft = f.paddingRight = padX;
  f.paddingTop = f.paddingBottom = padY;
  f.primaryAxisSizingMode = "AUTO";
  f.counterAxisSizingMode = "AUTO";
  f.counterAxisAlignItems = node.align || "MIN";
  f.fills = node.fill ? fill(node.fill) : [];
  if (node.stroke) {
    f.strokes = fill(node.stroke);
    f.strokeWeight = 1;
  }
  if (node.radius) f.cornerRadius = node.radius;
  if (node.noClip) f.clipsContent = false;

  for (const child of node.children) f.appendChild(render(child, vertical));

  // 크기 고정은 자식을 붙인 뒤에. 세로 프레임은 primary=높이, counter=너비다.
  if (node.w != null) {
    if (vertical) f.counterAxisSizingMode = "FIXED";
    else f.primaryAxisSizingMode = "FIXED";
  }
  if (node.h != null) {
    if (vertical) f.primaryAxisSizingMode = "FIXED";
    else f.counterAxisSizingMode = "FIXED";
  }
  if (node.w != null || node.h != null) {
    f.resize(node.w != null ? node.w : f.width, node.h != null ? node.h : f.height);
  }

  // stretch는 부모의 교차축을, grow는 부모의 주축을 채운다.
  // 채워지는 축이 이 프레임 기준으로 어느 축인지 계산해 FIXED로 바꾼다.
  if (parentVertical != null) {
    if (node.stretch) setAxisFixed(f, vertical, parentVertical ? "w" : "h");
    if (node.grow) setAxisFixed(f, vertical, parentVertical ? "h" : "w");
  }
  return f;
}

function render(node, parentVertical) {
  let n;
  if (node.t === "text") n = makeText(node);
  else if (node.t === "box") n = makeBox(node);
  else n = makeGroupFrame(node, parentVertical);

  if (node.grow) n.layoutGrow = 1;
  if (node.stretch) n.layoutAlign = "STRETCH";
  if (node.link) LINKS.push({ node: n, target: node.link });
  return n;
}

/* ─────────────── 반복 요소 ─────────────── */

const gnb = (active) =>
  row({ h: 62, padX: 18, gap: 22, align: "CENTER", fill: C.bar, stretch: true, name: "GNB" }, [
    txt("VIGILANTIS", { bold: true, size: 15, link: "DSH" }),
    txt("대시보드", { size: 15, color: active === "dash" ? C.ink : C.faint, bold: active === "dash", link: "DSH" }),
    txt("자산", { size: 15, color: active === "asset" ? C.ink : C.faint, bold: active === "asset", link: "AST_LIST" }),
    txt("인시던트", { size: 15, color: active === "inc" ? C.ink : C.faint, bold: active === "inc", link: "INC_LIST" }),
    txt("승인함", { size: 15, color: C.faint, link: "APR" }),
    txt("실행 이력", { size: 15, color: C.faint, link: "HIS" }),
    gap(),
    txt("● 실시간 연결됨", { size: 14, color: C.mid }),
  ]);

const card = (title, aside, children, p) =>
  col(
    Object.assign({ pad: 14, gap: 12, fill: C.paper, stroke: C.line, radius: 4 }, p || {}),
    [
      row({ stretch: true, align: "CENTER", gap: 8 }, [
        txt(title, { bold: true, size: 15 }),
        gap(),
        aside ? txt(aside, { size: 13, color: C.faint }) : gap(),
      ]),
    ].concat(children),
  );

const chip = (label, w) =>
  box({ w: w || Math.max(60, label.length * 9 + 24), h: 24, label: label, size: 13 });
const field = (label, value) =>
  row({ gap: 10, align: "CENTER" }, [
    txt(label, { size: 14, color: C.faint, w: 92 }),
    txt(value, { size: 14 }),
  ]);

// 16:9 (1920x1080). 발표·모니터가 전부 16:9라 레터박스 없이 꽉 찬다.
// 타깃 브라우저 뷰포트 기준이며 보는 사람 모니터에 맞추지 않는다.
// 높이를 고정해야 안쪽 grow가 나눠 가질 공간이 생긴다. hug로 두면 전부 접힌다.
const SCREEN_W = 1920;
const SCREEN_H = 1080;

// clipsContent를 끈다. 내용이 넘치면 잘라 숨기는 대신 삐져나오게 둔다 —
// 와이어프레임에서 조용히 사라지는 것보다 눈에 보이는 편이 낫다.
const screen = (id, name, note, body) =>
  col({ w: SCREEN_W, h: SCREEN_H, fill: C.paper, stroke: C.frame, noClip: true, name: id + " " + name, gap: 0 }, [
    gnb(note),
    col({ pad: 18, gap: 14, stretch: true, grow: 1 }, body),
  ]);

/* ─────────────── 화면 정의 ─────────────── */

function screens() {
  const list = [];

  /* DSH-001 */
  list.push({
    key: "DSH",
    id: "DSH-001",
    name: "메인 대시보드",
    node: screen("DSH-001", "메인 대시보드", "dash", [
      /* 1행 — 시그널 · 헬스 · 즉시 조치 가능 */
      row({ gap: 12, stretch: true, h: 186 }, [
        card("상시 관제 시그널", null, [
          txt("● All Systems Operational", { bold: true, size: 20 }),
          row({ gap: 22, stretch: true }, [
            miniStat("관제 자산", "00"),
            miniStat("미조치 인시던트", "00"),
            miniStat("낭비 후보", "00"),
            miniStat("최근 수집", "hh:mm:ss"),
          ]),
        ], { grow: 1, stretch: true }),
        card("헬스 스코어", "확인 불가", [
          box({ w: 145, h: 59, label: "게이지 → —" }),
        ], { w: 335, align: "CENTER", stretch: true }),
        card("즉시 조치 가능", null, [
          countRow("추천 실행", "00", "action.mode = RECOMMENDED"),
          countRow("차단 해제", "00", "rollback_available = true"),
        ], { w: 440, stretch: true, link: "INC_LIST" }),
      ]),

      /* 2행 — 자산 토폴로지 · 보안 위협 맵 · 처리 대기 인시던트 */
      row({ gap: 12, stretch: true, grow: 1 }, [
        card("자산 통계", "최근 14일", [
          row({ gap: 12, stretch: true }, [
            col({ gap: 8, grow: 1, align: "CENTER" }, [
              box({ w: 116, h: 116, label: "자원 유형 도넛" }),
              txt("EC2 · SG · EBS · ALB · EIP", { size: 12, color: C.faint }),
            ]),
            col({ gap: 8, grow: 1 }, [
              txt("CPU 사용률 분포", { size: 13, bold: true }),
              box({ h: 92, stretch: true, label: "막대 (0-5% / 5-20% / 20%+)" }),
              txt("5% 미만이 Idle 판정 후보", { size: 12, color: C.faint }),
            ]),
          ]),
          rule(),
          row({ gap: 12, stretch: true, align: "CENTER" }, [
            txt("리전 · AZ 분포", { size: 13, bold: true }),
            gap(),
            txt("ap-northeast-2  AZ-a / AZ-c", { size: 12, color: C.faint }),
          ]),
          box({ h: 44, stretch: true, label: "가로 막대 — AZ별 자산 수" }),
        ], { grow: 1, stretch: true, link: "AST_LIST" }),

        card("보안 위협 맵", "공격 경로", [
          col({ gap: 6, stretch: true, grow: 1, pad: 8, fill: C.box, radius: 3, align: "CENTER" }, [
            box({ w: 299, h: 43, label: "외부 Source IP · 차단됨", strong: true, link: "INC_B" }),
            txt("│", { size: 13, color: C.faint }),
            box({ w: 299, h: 43, label: "SG · 선제 차단됨", strong: true, link: "INC_B" }),
            txt("│  ATTACHED_TO", { size: 12, color: C.faint }),
            box({ w: 299, h: 43, label: "EC2 · 영향 자산", link: "AST_DRAWER" }),
            gap(),
            box({ h: 27, stretch: true, label: "● 차단 소스  ● 위협 대상", strong: true }),
          ]),
        ], { grow: 1, stretch: true, link: "INC_LIST" }),

        card("처리 대기 인시던트", "전체 →", [
          incidentRow("보안", "위협 제목", "높음 → 중간", true, "INC_B"),
          incidentRow("최적화", "최적화 제목", "RUNBOOK_ID", false, "INC_A"),
          incidentRow("보안", "위협 제목", "높음 → 평가 중", true, "INC_B"),
          gap(),
        ], { w: 669, stretch: true }),
      ]),

      /* 3행 — 집계 */
      row({ gap: 12, stretch: true, h: 292 }, [
        card("자산 분포 · Rule 판정", null, [
          row({ gap: 12, stretch: true, grow: 1 }, [
            box({ w: 98, h: 100, label: "도넛" }),
            col({ gap: 3, grow: 1 }, [
              countRow("EC2", "00"),
              countRow("Security Group", "00"),
              countRow("EBS · ALB · EIP", "00"),
              countRow("낭비 후보", "00"),
            ]),
          ]),
        ], { grow: 1, stretch: true, link: "AST_LIST" }),

        card("위험도 현황", null, [
          countRow("초기 HIGH", "00"),
          countRow("정밀 MEDIUM", "00"),
          countRow("평가 대기", "00"),
          countRow("정밀평가 실패", "00"),
        ], { grow: 1, stretch: true }),


        card("수집 · 실행", null, [
          countRow("최근 수집", "hh:mm:ss"),
          countRow("가장 오래됨", "hh:mm:ss"),
          countRow("진행 중 실행", "00"),
          countRow("분석 진행 중", "00"),
        ], { grow: 1, stretch: true }),
      ]),
    ]),
  });

  /* AST-001 목록 */
  list.push({
    key: "AST_LIST",
    id: "AST-001",
    name: "자산 관제 — 목록 뷰",
    node: screen("AST-001", "자산 관제 · 목록", "asset", [
      assetToolbar("목록", "AST_TOPO"),
      col({ stretch: true, grow: 1, fill: C.paper, stroke: C.line, radius: 4 }, [
        tableHeader(["이름", "유형", "리전", "상태", "Rule 판정", "헬스", "인시던트", "수집"]),
        tableRow("AST_DRAWER"), tableRow("AST_DRAWER"), tableRow("AST_DRAWER"),
        tableRow("AST_DRAWER"), tableRow("AST_DRAWER"), tableRow("AST_DRAWER"),
        gap(),
      ]),
    ]),
  });

  /* AST-001 토폴로지 */
  list.push({
    key: "AST_TOPO",
    id: "AST-001",
    name: "자산 관제 — 토폴로지 뷰",
    node: screen("AST-001", "자산 관제 · 토폴로지", "asset", [
      assetToolbar("토폴로지", "AST_LIST"),
      col({ stretch: true, grow: 1, fill: C.box, stroke: C.line, radius: 4, pad: 20, gap: 18 }, [
        trafficHeader(),
        rule(),
        topoFlow("AZ-a", "EC2 · Idle", "EBS · 연결됨"),
        topoFlow("AZ-c", "EC2 · 정상", "EBS · 연결됨"),
        topoFlow("AZ-a", "EC2 · 지표부족", "EBS · 연결됨"),
        rule(),
        row({ gap: 16, align: "CENTER" }, [
          txt("트래픽 경로 밖", { size: 13, color: C.faint }),
          box({ w: 176, h: 62, label: "SG · 미사용" }),
          box({ w: 176, h: 62, label: "EBS · 미연결" }),
          box({ w: 176, h: 62, label: "EIP · 미연결" }),
          txt("비용만 발생", { size: 12, color: C.faint }),
        ]),
        gap(),
        box({ w: 760, h: 35, strong: true,
          label: "▶ 트래픽 방향  ·  낭비 후보(주황)  ·  보안 대상(빨강)  ·  SG는 EC2에 결합" }),
      ]),
    ]),
  });

  /* AST-002 */
  list.push({
    key: "AST_DRAWER",
    id: "AST-002",
    name: "자산 상세 패널 (Drawer)",
    node: screen("AST-002", "자산 상세 패널", "asset", [
      row({ gap: 0, stretch: true, grow: 1 }, [
        col({ grow: 1, gap: 10, stretch: true }, [
          assetToolbar("목록", "AST_TOPO"),
          col({ stretch: true, grow: 1, fill: C.paper, stroke: C.line, radius: 4 }, [
            tableHeader(["이름", "유형", "리전", "상태", "Rule 판정", "헬스"]),
            tableRow("AST_DRAWER"), tableRow("AST_DRAWER"), tableRow("AST_DRAWER"),
            gap(),
          ]),
        ]),
        col({ w: 599, gap: 0, fill: C.paper, stroke: C.line, stretch: true }, [
          col({ pad: 14, gap: 6, stretch: true }, [
            row({ stretch: true, align: "CENTER" }, [txt("자산 이름", { bold: true, size: 17 }), gap(), txt("✕", { size: 15, color: C.faint })]),
            txt("EC2 · 리전 · 계정", { size: 13, color: C.faint }),
          ]),
          rule(),
          col({ pad: 14, gap: 8, stretch: true }, [
            field("헬스", "— (확인 불가)"),
            field("스펙", "key = value"),
            field("ARN", "arn:aws:..."),
            field("수집", "yyyy-mm-dd hh:mm"),
          ]),
          rule(),
          col({ pad: 14, gap: 8, stretch: true }, [
            txt("RULE 판정", { size: 13, bold: true, color: C.faint }),
            row({ gap: 8, align: "CENTER" }, [chip("판정 배지", 70), txt("evaluated_at", { size: 13, color: C.faint })]),
            txt("· reason_code", { size: 13, color: C.mid }),
            txt("· reason_code", { size: 13, color: C.mid }),
          ]),
          rule(),
          col({ pad: 14, gap: 8, stretch: true }, [
            txt("연결된 인시던트", { size: 13, bold: true, color: C.faint }),
            box({ h: 54, stretch: true, label: "인시던트 링크 → INC-002", link: "INC_A" }),
          ]),
          gap(),
        ]),
      ]),
    ]),
  });

  /* INC-001 */
  list.push({
    key: "INC_LIST",
    id: "INC-001",
    name: "인시던트 목록",
    node: screen("INC-001", "인시던트 목록", "inc", [
      row({ stretch: true, align: "CENTER", gap: 10 }, [
        txt("인시던트", { bold: true, size: 20 }),
        gap(),
        txt("updated_at 내림차순 · 미조치 보안 상단 고정", { size: 13, color: C.faint }),
      ]),
      col({ gap: 10, stretch: true, grow: 1 }, [
        incidentRow("보안", "위협 제목", "높음 → 중간", true, "INC_B"),
        incidentRow("보안", "위협 제목", "높음 → 평가 중", true, "INC_B"),
        incidentRow("최적화", "최적화 제목", "RUNBOOK_ID", false, "INC_A"),
        incidentRow("최적화", "탐지만 (조치 없음)", "조치 없음", false, "INC_A"),
        gap(),
      ]),
    ]),
  });

  /* INC-002 A */
  list.push({
    key: "INC_A",
    id: "INC-002",
    name: "인시던트 상세 — A 최적화",
    node: screen("INC-002", "인시던트 상세 · A", "inc", [
      detailHeader("최적화", "최적화 진단 제목", "미조치"),
      row({ gap: 14, stretch: true, grow: 1 }, [
        col({ grow: 1, gap: 14, stretch: true }, [
          card("판단 근거", "decision_summary[3]", [
            summaryLine("1"), summaryLine("2"), summaryLine("3"),
          ], { stretch: true }),
          card("근거 데이터", "evidence[]", [
            box({ h: 59, stretch: true, label: "evidence_id · type · 요약 · observed_at" }),
          ], { stretch: true }),
          card("대상 자산", null, [field("자산", "asset name"), field("ARN", "arn:aws:...")], { stretch: true }),
          gap(),
        ]),
        col({ w: 704, gap: 14, stretch: true }, [
          card("절감 예상", "AI 산출", [
            row({ gap: 10, align: "CENTER", stretch: true }, [
              txt("월 $000", { bold: true, size: 28 }),
              box({ w: 82, h: 30, label: "00% 절감", strong: true }),
            ]),
            txt("현재 월 $000 → 적용 후 월 $000", { size: 13, color: C.faint }),
          ], { stretch: true }),

          card("제안 조치 패키지", "런북 조합", [
            runbookRow("RUNBOOK_EC2_RIGHTSIZING", "스펙 조정", false),
            runbookRow("RUNBOOK_EC2_ENABLE_AUTOSCALING", "ASG 전환", false),
            runbookRow("RUNBOOK_EBS_DELETE_UNATTACHED", "미연결 EBS 삭제", true),
            box({ h: 46, stretch: true, label: "실행 전 Guardrail 4단계 · 파괴적 조치는 사전 백업" }),
            row({ stretch: true }, [
              gap(),
              box({ w: 169, h: 43, label: "선택 조치 실행", strong: true, link: "ACT_MODAL_A" }),
            ]),
          ], { stretch: true }),

          card("실행 이력", null, [
            txt("execution_id = null · 아직 실행되지 않음", { size: 13, color: C.faint }),
          ], { stretch: true }),
          gap(),
        ]),
      ]),
    ]),
  });

  /* INC-002 B */
  list.push({
    key: "INC_B",
    id: "INC-002",
    name: "인시던트 상세 — B 보안",
    node: screen("INC-002", "인시던트 상세 · B", "inc", [
      detailHeader("보안", "보안 인시던트 제목", "조치됨"),
      row({ gap: 14, stretch: true, grow: 1 }, [
        col({ grow: 1, gap: 14, stretch: true }, [
          card("위험도 판정", "initial_risk · reviewed_risk (병렬 유지)", [
            row({ gap: 12, stretch: true }, [
              riskCard("초기 판정 (규칙)"),
              riskCard("정밀 평가 (AI)"),
            ]),
            box({ h: 43, stretch: true, label: "안내: 정밀 평가가 초기 판정을 대체하지 않음 · 자동 해제 없음" }),
          ], { stretch: true }),
          card("판단 근거", "decision_summary[3]", [
            summaryLine("1"), summaryLine("2"), summaryLine("3"),
          ], { stretch: true }),
          card("근거 데이터", "evidence[]", [
            box({ h: 59, stretch: true, label: "evidence_id · type · 요약" }),
            box({ h: 40, stretch: true, strong: true,
              label: "공격 타임라인 보기 → INC-003", link: "XAI" }),
          ], { stretch: true }),
          gap(),
        ]),
        col({ w: 669, gap: 14, stretch: true }, [
          card("수행된 조치", "선제 격리됨", [
            isolationRow("SG 교체", "sg-isolation (규칙 0)"),
            isolationRow("ALB 이탈", "Target Group Deregister"),
            isolationRow("NACL", "공격 IP DENY 규칙 추가"),
            rule(),
            txt("복구 정보 (조치 직전 백업)", { size: 13, bold: true, color: C.faint }),
            txt("· 원본 SG 규칙 JSON   · TG 매핑   · NACL 인덱스", { size: 13, color: C.mid }),
            row({ stretch: true, gap: 8 }, [
              gap(),
              box({ w: 111, h: 43, label: "격리 유지", link: "INC_LIST" }),
              box({ w: 127, h: 43, label: "격리 해제", strong: true, link: "ACT_MODAL_B" }),
            ]),
          ], { stretch: true }),

          card("근본 조치 제안", "AI 생성 패키지", [
            runbookRow("RUNBOOK_IAM_KEY_ROTATE", "유출 Key 회전", false),
            runbookRow("RUNBOOK_SSM_MIGRATE", "SSM 전환 (Terraform PR)", false),
            runbookRow("RUNBOOK_EC2_REDEPLOY_CLEAN_AMI", "클린 AMI 재배포", true),
            row({ stretch: true }, [
              gap(),
              box({ w: 169, h: 40, label: "승인 요청", strong: true, link: "APR" }),
            ]),
          ], { stretch: true }),

          gap(),
        ]),
      ]),
    ]),
  });

  /* ACT-001 A */
  list.push({
    key: "ACT_MODAL_A",
    id: "ACT-001",
    name: "실행 확인 모달 — A 실행",
    node: modalScreen("최적화", "선택한 조치를 실행합니다", [
      field("대상", "demo-api"),
      txt("실행할 런북 (3건 중 선택)", { size: 13, bold: true, color: C.faint }),
      runbookRow("RUNBOOK_EC2_RIGHTSIZING", "스펙 조정  t3.xlarge → t3.large", false),
      runbookRow("RUNBOOK_EC2_ENABLE_AUTOSCALING", "ASG 전환  min 1 / max 4", false),
      runbookRow("RUNBOOK_EBS_DELETE_UNATTACHED", "미연결 EBS 삭제", true),
      box({ h: 65, stretch: true, strong: true,
        label: "⚠ 파괴적 조치 포함 — EBS 삭제 직전 최종 스냅샷을 강제 생성합니다" }),
      field("절감 예상", "월 $000 (00%)"),
      box({ h: 46, stretch: true, label: "Guardrail 4단계 → 승인 → 실행" }),
      txt("idempotency_key  (모달이 열릴 때 1회 생성 · 고정)", { size: 12, color: C.faint }),
    ], ["취소", "승인 요청"], "A 실행", "INC_A"),
  });

  /* ACT-001 B */
  list.push({
    key: "ACT_MODAL_B",
    id: "ACT-001",
    name: "실행 확인 모달 — B 해제",
    node: modalScreen("보안", "선제 차단을 해제합니다", [
      field("대상", "EC2 / SG / ALB"),
      txt("되돌릴 항목 (저장된 복구 정보)", { size: 13, bold: true, color: C.faint }),
      isolationRow("SG", "원본 규칙 JSON으로 복원"),
      isolationRow("ALB", "Target Group 재등록"),
      isolationRow("NACL", "DENY 규칙 제거"),
      field("원본 실행", "execution_id"),
      box({ h: 59, stretch: true, label: "경고: 해제 시 해당 자산이 다시 트래픽을 받습니다 · 원본 격리 이력 보존" }),
      txt("idempotency_key  (모달이 열릴 때 1회 생성 · 고정)", { size: 12, color: C.faint }),
    ], ["취소", "해제 실행"], "B 해제", "INC_B"),
  });

  /* APR-001 승인함 */
  list.push({
    key: "APR",
    id: "APR-001",
    name: "승인함 (Human-in-the-Loop)",
    node: screen("APR-001", "승인함", "inc", [
      row({ stretch: true, align: "CENTER", gap: 10 }, [
        txt("승인함", { bold: true, size: 20 }),
        chip("대기 00", 62),
        gap(),
        box({ w: 127, h: 38, label: "유형 전체 ▾" }),
        txt("요청 시각 오름차순 · 오래된 건 상단", { size: 13, color: C.faint }),
      ]),
      col({ gap: 12, stretch: true, grow: 1 }, [
        approvalCard("최적화", "Idle EC2 최적화 패키지", "월 $000 (00%) 절감", 3),
        approvalCard("보안", "침해 EC2 근본 조치 패키지", "위험도 높음 · 격리 중", 3),
        approvalCard("최적화", "미연결 EBS 정리", "월 $000 절감 · 파괴적 포함", 1),
        gap(),
        box({ h: 40, stretch: true, label: "승인 시 Guardrail 4단계 → 실행. 반려 시 EXECUTION_BLOCKED로 종료" }),
      ]),
    ]),
  });

  /* INC-003 공격 타임라인 */
  list.push({
    key: "XAI",
    id: "INC-003",
    name: "공격 타임라인 (XAI)",
    node: screen("INC-003", "공격 타임라인", "inc", [
      row({ stretch: true, align: "CENTER", gap: 10 }, [
        txt("← 인시던트 상세", { size: 14, color: C.faint, link: "INC_B" }),
        gap(),
        txt("CloudTrail · OS Auth Log · WAF Log 재구성", { size: 13, color: C.faint }),
      ]),
      row({ gap: 12, stretch: true, grow: 1 }, [
        card("공격 시나리오 타임라인", "시간순", [
          attackStep("00:00", "WAF", "다수 요청 유입 · 룰 매칭"),
          attackStep("00:02", "OS Auth", "SSH 인증 실패 반복"),
          attackStep("00:04", "CloudTrail", "IAM Key 사용 · 비정상 리전"),
          attackStep("00:05", "GuardDuty", "C&C 통신 탐지"),
          gap(),
        ], { grow: 1, stretch: true }),
        col({ w: 738, gap: 12, stretch: true }, [
          card("AI 추론 (CoT)", "판단 근거 3줄", [
            summaryLine("1"), summaryLine("2"), summaryLine("3"),
            rule(),
            box({ h: 76, stretch: true, strong: true,
              label: "근본 원인: 0.0.0.0/0 SSH 개방 → 자격증명 유출 → IAM Key 탈취" }),
          ], { stretch: true }),
          card("근거 데이터", "Evidence ID", [
            box({ h: 43, stretch: true, label: "evd-000 · CLOUDTRAIL" }),
            box({ h: 43, stretch: true, label: "evd-000 · WAF_LOG" }),
            box({ h: 43, stretch: true, label: "evd-000 · OS_AUTH_LOG" }),
          ], { stretch: true }),
          gap(),
        ]),
      ]),
    ]),
  });

  /* HIS-001 감사 로그 */
  list.push({
    key: "HIS",
    id: "HIS-001",
    name: "실행 이력 · 감사 로그",
    node: screen("HIS-001", "실행 이력 · 감사 로그", "inc", [
      row({ stretch: true, align: "CENTER", gap: 10 }, [
        txt("실행 이력 · 감사 로그", { bold: true, size: 20 }),
        box({ w: 172, h: 38, label: "기간 최근 7일 ▾" }),
        box({ w: 127, h: 38, label: "결과 전체 ▾" }),
        gap(),
        box({ w: 100, h: 38, label: "CSV" }),
        box({ w: 100, h: 38, label: "JSON" }),
      ]),
      col({ stretch: true, grow: 1, fill: C.paper, stroke: C.line, radius: 4 }, [
        tableHeader(["시각", "유형", "대상", "Runbook", "승인자", "결과", "Request ID"]),
        tableRow(), tableRow(), tableRow(), tableRow(), tableRow(), tableRow(),
        gap(),
      ]),
      box({ h: 40, stretch: true, label: "감사 항목: Boto3 Request ID · 승인자 ID · AI 추론 보고서 · Evidence ID" }),
    ]),
  });

  /* ACT-002 */
  list.push({
    key: "ACT_PANEL",
    id: "ACT-002",
    name: "실행 상태 패널",
    node: screen("ACT-002", "실행 상태 패널", "inc", [
      detailHeader("최적화", "인시던트 제목", "조치 진행 중"),
      card("실행 상태", "execution_id", [
        row({ gap: 0, stretch: true, align: "CENTER" }, [
          phaseStep("예약"), phaseStep("검증"), phaseStep("승인"),
          phaseStep("백업"), phaseStep("실행"), phaseStep("점검"), phaseStep("완료"),
        ]),
        txt("현재 단계 메시지 (서버 전송)", { size: 14, color: C.mid }),
        row({ stretch: true }, [gap(), box({ w: 148, h: 35, label: "감사 이력 보기", link: "HIS" })]),
      ], { stretch: true }),
      row({ gap: 14, stretch: true, grow: 1 }, [
        card("Guardrail 검증", "4단계", [
          row({ gap: 20, stretch: true }, [
            col({ gap: 8, grow: 1 }, [txt("□ 1. Schema Check", { size: 14, color: C.mid }), txt("□ 2. Action Whitelist", { size: 14, color: C.mid })]),
            col({ gap: 8, grow: 1 }, [txt("□ 3. ARN Match", { size: 14, color: C.mid }), txt("□ 4. AWS Dry-Run", { size: 14, color: C.mid })]),
          ]),
          ], { grow: 1, stretch: true }),
        card("진행 기록", "수신 메시지 누적", [
          timelineRow(), timelineRow(), timelineRow(), timelineRow(),
          gap(),
        ], { grow: 1, stretch: true }),
      ]),
    ]),
  });

  /* ACT-002 상태 6종 */
  list.push({
    key: "ACT_STATES",
    catalog: true,
    id: "ACT-002",
    name: "실행 상태 6종",
    node: screen("ACT-002", "실행 상태 6종", "inc", [
      txt("실행 상태별 화면 표현", { bold: true, size: 17 }),
      row({ gap: 12, stretch: true, grow: 1 }, [
        stateCard("진행 중", "IN_PROGRESS", "진행 바 + 스피너 · 실행 버튼 비활성", "비최종"),
        stateCard("성공", "SUCCESS", "조치가 완료됐습니다", "최종"),
        stateCard("실패", "FAILED", "AWS 변경 없음 · error_code 표시", "최종"),
      ]),
      row({ gap: 12, stretch: true, grow: 1 }, [
        stateCard("승인 대기", "PENDING_APPROVAL", "관제자 One-Click 승인 대기 · APR-001", "비최종"),
        stateCard("실행 차단", "EXECUTION_BLOCKED", "Guardrail 실패 또는 승인 반려", "최종"),
        stateCard("반려", "REJECTED", "관제자가 조치를 반려함", "최종"),
      ]),
      row({ gap: 12, stretch: true, grow: 1 }, [
        stateCard("복구 시작", "ROLLBACK_INITIATED", "자동 복구 진행 · 계속 수신", "비최종"),
        stateCard("복구 완료", "ROLLED_BACK", "이전 상태로 복구", "최종 · 추가 제안 (확인 #2)"),
        stateCard("복구 실패", "ROLLBACK_FAILED", "수동 확인 필요 · 자동 재시도 없음", "최종 · 추가 제안 (확인 #2)"),
      ]),
    ]),
  });

  /* CMN-001 */
  list.push({
    key: "CMN_TOAST",
    id: "CMN-001",
    name: "실시간 알림 · 연결 상태",
    node: screen("CMN-001", "실시간 알림 · 연결 상태", "dash", [
      row({ gap: 14, stretch: true, h: 256 }, [
        card("연결 인디케이터 (GNB 우측)", "실시간 연결은 전역 1개가 소유", [
          row({ gap: 26, align: "CENTER", stretch: true }, [
            box({ w: 158, h: 32, label: "● 실시간 연결됨" }),
            box({ w: 145, h: 32, label: "◐ 재연결 중…" }),
            box({ w: 198, h: 32, label: "○ 연결 끊김  [재연결]" }),
          ]),
          box({ h: 59, stretch: true, label: "백오프 1s→2s→4s→8s→최대 30s · 재연결 시 현재 상태를 다시 받아 교체" }),
        ], { grow: 1, stretch: true }),
        card("Toast 정책", null, [
          txt("· SECURITY_INCIDENT_UPDATED → 항상 표시, 8초", { size: 13, color: C.mid }),
          txt("· ACTION_EXECUTION_UPDATED → 최종 상태에서만", { size: 13, color: C.mid }),
          txt("· 동일 incident_id는 최신 1건으로 대체", { size: 13, color: C.mid }),
          txt("· 최대 3개 스택 · 상태 일괄 수신은 Toast 없음", { size: 13, color: C.mid }),
        ], { w: 581, stretch: true }),
      ]),
      row({ gap: 14, stretch: true, grow: 1 }, [
        card("이벤트 종류별 처리", "실시간 이벤트 종류", [
          tableHeader(["event_type", "Toast", "화면 처리"]),
          tableRow(), tableRow(), tableRow(), tableRow(),
          gap(),
        ], { grow: 1, stretch: true }),
        col({ w: 581, gap: 10, stretch: true }, [
          txt("Toast 스택 — 위치 결정 대기", { size: 13, bold: true, color: C.faint }),
          col({ pad: 12, gap: 8, stretch: true, fill: C.paper, stroke: C.frame, radius: 4, link: "INC_B" }, [
            row({ gap: 8, align: "CENTER", stretch: true }, [chip("보안", 46), txt("보안 위협 감지", { bold: true, size: 15 })]),
            box({ h: 30, stretch: true, plain: true }),
            row({ stretch: true }, [gap(), box({ w: 100, h: 35, label: "상세 보기", strong: true, link: "INC_B" })]),
          ]),
          col({ pad: 12, gap: 8, stretch: true, fill: C.paper, stroke: C.line, radius: 4 }, [
            row({ gap: 8, align: "CENTER", stretch: true }, [chip("완료", 46), txt("조치가 완료됐습니다", { bold: true, size: 15 })]),
            box({ h: 30, stretch: true, plain: true }),
          ]),
          col({ pad: 12, gap: 8, stretch: true, fill: C.paper, stroke: C.line, radius: 4 }, [
            row({ gap: 8, align: "CENTER", stretch: true }, [chip("복구", 46), txt("복구를 시작했습니다", { bold: true, size: 15 })]),
            box({ h: 30, stretch: true, plain: true }),
          ]),
          gap(),
        ]),
      ]),
    ]),
  });

  /* CMN-002 */
  list.push({
    key: "CMN",
    catalog: true,
    id: "CMN-002",
    name: "로딩 · 빈 · 오류 상태",
    node: screen("CMN-002", "로딩 · 빈 · 오류 상태", null, [
      txt("공통 상태 (error.code 기준 분기 · request_id 항상 복사 가능)", { bold: true, size: 16 }),
      row({ gap: 12, stretch: true, grow: 1 }, [
        col({ grow: 1, gap: 8, pad: 14, fill: C.paper, stroke: C.line, radius: 4, stretch: true }, [
          txt("로딩 — skeleton", { size: 13, bold: true, color: C.faint }),
          box({ h: 14, stretch: true, plain: true }),
          box({ h: 14, w: 352, plain: true }),
          box({ h: 14, w: 198, plain: true }),
          gap(),
          txt("레이아웃 형태 유지 · 전체 스피너 금지", { size: 12, color: C.faint }),
        ]),
        stateCard("빈 상태 — 자산", "items = []", "관제 중인 자산이 없습니다", ""),
        stateCard("빈 상태 — 인시던트", "정상 신호", "처리할 인시던트가 없습니다 (오류 아님)", ""),
      ]),
      row({ gap: 12, stretch: true, grow: 1 }, [
        stateCard("404", "INCIDENT_NOT_FOUND", "요청한 Incident 없음", "목록으로"),
        stateCard("503", "STATE_STORE_UNAVAILABLE", "상태를 읽을 수 없습니다", "다시 시도"),
        stateCard("500", "INTERNAL_ERROR", "예상하지 못한 오류 · request_id 표시", "다시 시도"),
      ]),
    ]),
  });

  return list;
}

/* ─────────────── 조각 ─────────────── */

function miniStat(label, value) {
  return col({ gap: 4 }, [
    txt(label, { size: 13, color: C.faint }),
    txt(value, { bold: true, size: 18 }),
  ]);
}

function countRow(label, value, note) {
  return col({ gap: 3, stretch: true }, [
    row({ stretch: true, align: "CENTER" }, [
      txt(label, { size: 14, color: C.mid }),
      gap(),
      txt(value, { bold: true, size: 16 }),
    ]),
    note ? txt(note, { size: 12, color: C.faint }) : rule(),
  ]);
}

function topoTier(a, b, c) {
  return row({ gap: 10, align: "CENTER" }, [
    box({ w: 127, h: 59, label: a, link: "AST_DRAWER" }),
    box({ w: 26, h: 1, plain: true }),
    box({ w: 174, h: 59, label: b, link: "AST_DRAWER" }),
    box({ w: 26, h: 1, plain: true }),
    box({ w: 153, h: 59, label: c, link: "AST_DRAWER" }),
  ]);
}

function topoMini(a, b) {
  return row({ gap: 12, align: "CENTER" }, [
    box({ w: 156, h: 54, label: a, link: "AST_DRAWER" }),
    box({ w: 34, h: 1, plain: true }),
    box({ w: 156, h: 54, label: b, link: "AST_DRAWER" }),
  ]);
}

function approvalCard(kind, title, meta, count) {
  return row({ stretch: true, pad: 12, gap: 10, align: "CENTER",
               fill: C.paper, stroke: C.line, radius: 4 }, [
    chip(kind, 52),
    col({ gap: 5, grow: 1 }, [
      txt(title, { bold: true, size: 16 }),
      txt(meta + "  ·  런북 " + count + "건  ·  요청자 000", { size: 13, color: C.faint }),
    ]),
    box({ w: 87, h: 38, label: "상세", link: "INC_A" }),
    box({ w: 74, h: 38, label: "반려" }),
    box({ w: 87, h: 38, label: "승인", strong: true, link: "ACT_PANEL" }),
  ]);
}

function attackStep(time, source, detail) {
  return row({ stretch: true, gap: 10, align: "CENTER" }, [
    txt(time, { size: 13, color: C.faint }),
    box({ w: 13, h: 14 }),
    chip(source),
    txt(detail, { size: 14, color: C.mid, grow: 1 }),
  ]);
}

function runbookRow(id, label, destructive) {
  return row({ stretch: true, gap: 8, align: "CENTER", pad: 8, fill: C.paper, stroke: C.line, radius: 3 }, [
    box({ w: 18, h: 19 }),
    col({ gap: 3, grow: 1 }, [
      txt(label, { size: 14, bold: true }),
      txt(id, { size: 12, color: C.faint }),
    ]),
    destructive ? chip("파괴적", 46) : txt("", { size: 12 }),
  ]);
}

function isolationRow(kind, detail) {
  return row({ stretch: true, gap: 8, align: "CENTER" }, [
    chip(kind, 62),
    txt(detail, { size: 13, color: C.mid, grow: 1 }),
    box({ w: 58, h: 22, label: "완료", size: 12 }),
  ]);
}

function assetToolbar(active, toggleLink) {
  return row({ stretch: true, align: "CENTER", gap: 10 }, [
    txt("자산 관제", { bold: true, size: 20 }),
    box({ w: 158, h: 38, label: active === "목록" ? "[목록] 토폴로지" : "목록 [토폴로지]", link: toggleLink }),
    box({ w: 121, h: 38, label: "유형 ▾" }),
    box({ w: 158, h: 38, label: "리전 ▾" }),
    txt("□ 낭비 후보만", { size: 14, color: C.mid }),
    gap(),
    txt("⟳ generated_at", { size: 13, color: C.faint }),
  ]);
}

function tableHeader(cols) {
  return col({ stretch: true, gap: 0 }, [
    row({ stretch: true, pad: 10, gap: 0, align: "CENTER" },
      cols.map((c) => txt(c, { size: 13, color: C.faint, grow: 1 }))),
    rule(),
  ]);
}

function tableRow(link) {
  return col({ stretch: true, gap: 0 }, [
    row({ stretch: true, padX: 10, padY: 11, gap: 0, align: "CENTER", link: link }, [
      box({ w: 116, h: 15, plain: true }),
      gap(),
      box({ w: 79, h: 15, plain: true }),
      gap(),
      box({ w: 98, h: 15, plain: true }),
      gap(),
      chip("배지", 56),
      gap(),
      chip("판정", 74),
      gap(),
      box({ w: 29, h: 15, plain: true }),
      gap(),
      chip("유형", 50),
      gap(),
      box({ w: 71, h: 15, plain: true }),
    ]),
    rule(),
  ]);
}

function trafficHeader() {
  return row({ gap: 12, align: "CENTER", stretch: true }, [
    box({ w: 132, h: 46, label: "외부 요청", strong: true }),
    txt("▶", { size: 16, color: C.mid }),
    box({ w: 104, h: 46, label: "WAF" }),
    txt("▶", { size: 16, color: C.mid }),
    box({ w: 104, h: 46, label: "ALB" }),
    txt("▶  AZ별 EC2로 분산", { size: 13, color: C.faint }),
    gap(),
    box({ w: 172, h: 28, label: "트래픽 유입 방향", strong: true }),
  ]);
}

function topoFlow(az, ec2, ebs) {
  return row({ gap: 12, align: "CENTER" }, [
    chip(az),
    box({ w: 104, h: 62, label: "ALB 타겟", link: "AST_DRAWER" }),
    txt("▶", { size: 15, color: C.mid }),
    box({ w: 210, h: 62, label: ec2, link: "AST_DRAWER" }),
    txt("▶", { size: 15, color: C.mid }),
    box({ w: 186, h: 62, label: ebs, link: "AST_DRAWER" }),
    box({ w: 96, h: 28, label: "SG", size: 12 }),
  ]);
}

function topoRow(a, b, label) {
  return row({ gap: 12, align: "CENTER" }, [
    box({ w: 185, h: 76, label: a, link: "AST_DRAWER" }),
    box({ w: 79, h: 1, plain: true }),
    txt(label, { size: 12, color: C.faint }),
    box({ w: 79, h: 1, plain: true }),
    box({ w: 185, h: 76, label: b, link: "AST_DRAWER" }),
  ]);
}

function incidentRow(kind, title, meta, isSecurity, link) {
  return row(
    { stretch: true, pad: 12, gap: 10, align: "CENTER", fill: C.paper, stroke: C.line, radius: 4, link: link },
    [
      box({ w: 5, h: 46, plain: true, strong: isSecurity }),
      chip(kind, 52),
      col({ gap: 5, grow: 1 }, [
        txt(title, { bold: true, size: 15 }),
        txt(meta, { size: 13, color: C.faint }),
      ]),
      chip("상태", 52),
      txt("MM-DD hh:mm", { size: 13, color: C.faint }),
    ],
  );
}

function detailHeader(kind, title, status) {
  return col({ gap: 8, stretch: true }, [
    txt("← 인시던트", { size: 14, color: C.faint, link: "INC_LIST" }),
    row({ stretch: true, align: "CENTER", gap: 10 }, [
      chip(kind, 52),
      txt(title, { bold: true, size: 20 }),
      chip(status, 60),
      gap(),
      txt("incident_id · 생성 · 갱신", { size: 13, color: C.faint }),
    ]),
  ]);
}

function summaryLine(n) {
  return row({ gap: 10, align: "CENTER", stretch: true }, [
    box({ w: 24, h: 24, label: n, size: 12 }),
    box({ h: 15, grow: 1, plain: true }),
  ]);
}

function riskCard(label) {
  return col({ grow: 1, gap: 8, pad: 12, fill: C.paper, stroke: C.line, radius: 4 }, [
    txt(label, { size: 13, color: C.faint }),
    txt("등급", { bold: true, size: 22 }),
    txt("reason_codes", { size: 12, color: C.faint }),
    txt("evaluated_at", { size: 12, color: C.faint }),
  ]);
}

function phaseStep(label) {
  return col({ grow: 1, gap: 6, align: "CENTER" }, [
    box({ w: 24, h: 24 }),
    txt(label, { size: 13, color: C.mid }),
  ]);
}

function timelineRow() {
  return row({ gap: 10, align: "CENTER", stretch: true }, [
    txt("hh:mm:ss", { size: 12, color: C.faint }),
    box({ w: 8, h: 8 }),
    box({ h: 14, grow: 1, plain: true }),
  ]);
}

function stateCard(title, code, body, footer) {
  return col({ grow: 1, gap: 10, pad: 14, fill: C.paper, stroke: C.line, radius: 4, stretch: true }, [
    row({ stretch: true, align: "CENTER" }, [
      txt(title, { size: 13, bold: true, color: C.faint }),
      gap(),
      txt(code, { size: 12, color: C.faint }),
    ]),
    gap(),
    col({ gap: 8, align: "CENTER", stretch: true }, [
      box({ w: 40, h: 40, label: "" }),
      txt(body, { size: 14, w: 369 }),
    ]),
    gap(),
    footer ? txt(footer, { size: 12, color: C.faint }) : txt("", { size: 12 }),
  ]);
}

function modalScreen(kind, title, body, buttons, variant, backLink) {
  return screen("ACT-001", "실행 확인 모달 · " + variant, "inc", [
    txt("배경: INC-002 상세 (딤 처리)", { size: 13, color: C.faint }),
    box({ h: 84, stretch: true, label: "배경 화면 (조작 불가)" }),
    row({ stretch: true }, [
      gap(),
      col({ w: 757, gap: 0, fill: C.paper, stroke: C.frame, radius: 5 }, [
        row({ pad: 14, gap: 10, align: "CENTER", stretch: true, fill: C.bar }, [
          chip(kind, 52),
          txt(title, { bold: true, size: 16 }),
        ]),
        rule(),
        col({ pad: 16, gap: 12, stretch: true }, body),
        rule(),
        row({ pad: 12, gap: 8, stretch: true, fill: C.bar }, [
          gap(),
          box({ w: 87, h: 40, label: buttons[0], link: backLink }),
          box({ w: 121, h: 40, label: buttons[1], strong: true, link: "ACT_PANEL" }),
        ]),
      ]),
      gap(),
    ]),
    gap(),
  ]);
}

/* ─────────────── 프로토타입 연결 ─────────────── */

/**
 * 스펙의 link 키를 실제 프레임 id로 바꿔 클릭 인터랙션을 붙인다.
 *
 * manifest가 documentAccess: "dynamic-page"라 동기 reactions setter는 막혀 있다.
 * setReactionsAsync를 쓰고, 구버전 API 대비로만 setter로 떨어진다.
 */
async function connect(keyToId) {
  const dangling = [];
  let linked = 0;

  for (const link of LINKS) {
    const destinationId = keyToId[link.target];
    if (!destinationId) {
      dangling.push(link.target);
      continue;
    }
    const action = {
      type: "NODE",
      destinationId: destinationId,
      navigation: "NAVIGATE",
      transition: null,
      preserveScrollPosition: false,
    };
    const reactions = [{ trigger: { type: "ON_CLICK" }, actions: [action] }];
    try {
      if (link.node.setReactionsAsync) await link.node.setReactionsAsync(reactions);
      else link.node.reactions = reactions;
      linked++;
    } catch (e) {
      // 구버전은 actions 배열 대신 단수 action만 받는다
      try {
        const legacy = [{ trigger: { type: "ON_CLICK" }, action: action }];
        if (link.node.setReactionsAsync) await link.node.setReactionsAsync(legacy);
        else link.node.reactions = legacy;
        linked++;
      } catch (e2) {
        dangling.push(link.target + " (" + e2.message + ")");
      }
    }
  }
  return { linked: linked, dangling: dangling };
}

/* ─────────────── 배치 ─────────────── */

const COLS = 3;
const GAP_X = 90;
const GAP_Y = 120;

async function main() {
  await pickFont();

  const defs = screens();
  const nodes = [];
  let x = 0;
  let y = 0;
  let rowHeight = 0;

  const keyToId = {};

  for (let i = 0; i < defs.length; i++) {
    const def = defs[i];

    const caption = makeText({
      s: def.id + "  " + def.name,
      size: 20,
      bold: true,
      color: C.mid,
    });

    const frame = render(def.node);

    if (i % COLS === 0 && i > 0) {
      x = 0;
      y += rowHeight + GAP_Y;
      rowHeight = 0;
    }

    caption.x = x;
    caption.y = y;
    frame.x = x;
    frame.y = y + caption.height + 10;

    figma.currentPage.appendChild(caption);
    figma.currentPage.appendChild(frame);
    nodes.push(caption, frame);
    if (def.key) keyToId[def.key] = frame.id;
    frame.setPluginData("wireframeKey", def.key || "");
    frame.setPluginData("catalog", def.catalog ? "1" : "");

    const total = caption.height + 10 + frame.height;
    if (total > rowHeight) rowHeight = total;
    x += frame.width + GAP_X;
  }

  const result = await connect(keyToId);

  // 프로토타입 시작점. 설계서 2장의 A·B 두 흐름을 각각 시작점으로 둔다.
  figma.currentPage.flowStartingPoints = [
    { nodeId: keyToId.DSH, name: "흐름 A — 자산 최적화" },
    { nodeId: keyToId.CMN_TOAST, name: "흐름 B — 보안 위협 (Toast 수신)" },
  ];

  figma.currentPage.selection = nodes;
  figma.viewport.scrollAndZoomIntoView(nodes);

  let msg =
    "와이어프레임 " + defs.length + "개 · 프로토타입 링크 " + result.linked + "개 완료" +
    " (폰트: " + FONT.family + ")";
  if (result.dangling.length) {
    msg += " / 연결 실패: " + result.dangling.join(", ");
  }
  figma.closePlugin(msg);
}

main().catch((e) => {
  figma.closePlugin("실패: " + e.message);
});
