/**
 * code.js를 Figma 없이 돌려보는 스텁 하네스.
 *
 *   node check.js
 *
 * 진짜 렌더 결과는 확인할 수 없다. 여기서 잡는 건 구조적 사고다 —
 * 오타 난 헬퍼, characters보다 늦게 지정한 fontName, NaN 크기,
 * 자식 붙이기 전에 부른 resize 같은 것들.
 */

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");

const problems = [];
const allNodes = [];
const reactionLog = [];

let idSeq = 0;

function baseNode(type) {
  const node = {
    id: "n" + ++idSeq,
    type,
    name: type,
    children: [],
    parent: null,
    x: 0,
    y: 0,
    _w: null,
    _h: null,
    fills: [],
    strokes: [],
    strokeWeight: 0,
    cornerRadius: 0,
    layoutGrow: 0,
    layoutAlign: "INHERIT",
    appendChild(child) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
      child.parent = node;
      node.children.push(child);
    },
    pluginData: {},
    setPluginData(k, v) {
      node.pluginData[k] = v;
    },
    async setReactionsAsync(r) {
      node.reactions = r;
      reactionLog.push({ node, reactions: r });
    },
    resize(w, h) {
      if (!Number.isFinite(w) || !Number.isFinite(h)) {
        problems.push(`${node.name}: resize(${w}, ${h}) — 유한한 수가 아니다`);
      }
      if (w <= 0 || h <= 0) {
        problems.push(`${node.name}: resize(${w}, ${h}) — 0 이하 크기`);
      }
      node._w = w;
      node._h = h;
    },
  };
  allNodes.push(node);
  return node;
}

function autoSize(node) {
  const padX = (node.paddingLeft || 0) + (node.paddingRight || 0);
  const padY = (node.paddingTop || 0) + (node.paddingBottom || 0);
  const gaps = Math.max(0, node.children.length - 1) * (node.itemSpacing || 0);
  const vertical = node.layoutMode === "VERTICAL";
  const widths = node.children.map((c) => c.width);
  const heights = node.children.map((c) => c.height);
  if (node.layoutMode === "NONE" || !node.layoutMode) {
    return { w: padX + Math.max(0, ...widths, 0), h: padY + Math.max(0, ...heights, 0) };
  }
  return vertical
    ? { w: padX + Math.max(0, ...widths, 0), h: padY + gaps + heights.reduce((a, b) => a + b, 0) }
    : { w: padX + gaps + widths.reduce((a, b) => a + b, 0), h: padY + Math.max(0, ...heights, 0) };
}

function makeFrame() {
  const node = baseNode("FRAME");
  node.layoutMode = "NONE";
  node.itemSpacing = 0;
  node.paddingLeft = node.paddingRight = node.paddingTop = node.paddingBottom = 0;
  node.primaryAxisSizingMode = "AUTO";
  node.counterAxisSizingMode = "AUTO";
  node.counterAxisAlignItems = "MIN";
  node.primaryAxisAlignItems = "MIN";
  node.clipsContent = true;
  Object.defineProperty(node, "width", {
    get: () => (node._w != null ? node._w : autoSize(node).w),
  });
  Object.defineProperty(node, "height", {
    get: () => (node._h != null ? node._h : autoSize(node).h),
  });
  return node;
}

function makeText() {
  const node = baseNode("TEXT");
  node.fontSize = 12;
  node.textAutoResize = "WIDTH_AND_HEIGHT";
  let fontName = null;
  let characters = "";
  Object.defineProperty(node, "fontName", {
    get: () => fontName,
    set: (v) => {
      fontName = v;
      node._fontSetAt = characters === "" ? "before" : "after";
    },
  });
  Object.defineProperty(node, "characters", {
    get: () => characters,
    set: (v) => {
      if (!fontName) {
        problems.push(`TEXT("${String(v).slice(0, 20)}"): fontName 없이 characters를 지정했다`);
      }
      characters = String(v);
      node.name = "text: " + characters.slice(0, 24);
    },
  });
  Object.defineProperty(node, "width", {
    get: () => (node._w != null ? node._w : Math.max(1, characters.length * node.fontSize * 0.62)),
  });
  Object.defineProperty(node, "height", {
    get: () => {
      if (node._h != null) return node._h;
      const perLine = node.fontSize * 1.45;
      if (node._w == null) return perLine;
      const charsPerLine = Math.max(1, Math.floor(node._w / (node.fontSize * 0.62)));
      return Math.ceil(characters.length / charsPerLine) * perLine;
    },
  });
  return node;
}

/* ─────────────── figma 스텁 ─────────────── */

/**
 * 폰트 시나리오. macOS는 패밀리·스타일을 한글로 로컬라이즈해 내주기도 해서
 * (이 맥이 그렇다) 영문 이름만 가정하면 엉뚱한 굵기가 잡힌다. 둘 다 돌려본다.
 */
const FONT_SCENARIOS = {
  english: [
    { family: "Apple SD Gothic Neo", style: "Regular" },
    { family: "Apple SD Gothic Neo", style: "Bold" },
    { family: "Apple SD Gothic Neo", style: "Thin" },
    { family: "Inter", style: "Regular" },
  ],
  korean: [
    { family: "Apple SD 산돌고딕 Neo", style: "아주옅은체" },
    { family: "Apple SD 산돌고딕 Neo", style: "일반체" },
    { family: "Apple SD 산돌고딕 Neo", style: "볼드체" },
    { family: "Inter", style: "Regular" },
  ],
  interOnly: [
    { family: "Inter", style: "Regular" },
    { family: "Inter", style: "Bold" },
  ],
};
const SCENARIO = process.env.FONT_SCENARIO || "korean";

const loadedFonts = new Set();
let closeMessage = null;

const page = makeFrame();
page.name = "PAGE";
page.selection = [];
page.flowStartingPoints = [];

const figma = {
  currentPage: page,
  viewport: { scrollAndZoomIntoView() {} },
  createFrame: makeFrame,
  createText: makeText,
  async listAvailableFontsAsync() {
    return FONT_SCENARIOS[SCENARIO].map((f) => ({ fontName: f }));
  },
  async loadFontAsync(f) {
    loadedFonts.add(f.family + "/" + f.style);
  },
  closePlugin(msg) {
    closeMessage = msg;
  },
};

/* ─────────────── 실행 ─────────────── */

const source = fs.readFileSync(path.join(__dirname, "code.js"), "utf8");
const sandbox = { figma, console, Math, Object, Number, String, Array, Promise, Error, Map, RegExp };
vm.createContext(sandbox);

(async () => {
  vm.runInContext(source, sandbox, { filename: "code.js" });

  // main()이 비동기라 마이크로태스크가 다 돌 때까지 기다린다
  for (let i = 0; i < 50 && closeMessage === null; i++) {
    await new Promise((r) => setImmediate(r));
  }

  const frames = page.children.filter((n) => n.type === "FRAME");
  const texts = allNodes.filter((n) => n.type === "TEXT");

  // 폰트를 실제로 로드했는지, 그리고 굵기 두 종이 서로 다른지
  assert.ok(loadedFonts.size > 0, "loadFontAsync를 부르지 않았다");
  const picked = Array.from(loadedFonts);
  assert.equal(picked.length, 2, `[${SCENARIO}] Regular/Bold 두 종을 로드해야 한다: ${picked}`);
  // 첫 스타일로 아무거나 집으면 본문이 통째로 얇은체/볼드가 된다
  assert.ok(
    picked.some((f) => /Regular|일반체/.test(f)),
    `[${SCENARIO}] 본문용 스타일을 못 골랐다: ${picked}`,
  );

  // characters보다 fontName이 먼저인지
  for (const t of texts) {
    assert.notEqual(t._fontSetAt, "after", `fontName을 characters 뒤에 지정한 텍스트가 있다: ${t.name}`);
  }

  // 좌표·크기가 전부 유한한지
  for (const n of page.children) {
    for (const key of ["x", "y", "width", "height"]) {
      assert.ok(Number.isFinite(n[key]), `${n.name}.${key}가 유한하지 않다: ${n[key]}`);
    }
    assert.ok(n.width > 0 && n.height > 0, `${n.name}: 크기가 0 이하 (${n.width}x${n.height})`);
  }

  /**
   * 오토레이아웃 접힘 검사.
   *
   * layoutGrow=1은 "남는 공간을 나눠 갖는다"는 뜻이다. 부모의 주축이 AUTO(hug)면
   * 남는 공간이 0이라 자식이 0으로 접힌다. 실제로 이것 때문에 첫 실행에서 프레임이
   * 전부 GNB 바만 남고 사라졌다. 스텁이 크기를 content 합으로 계산해서 못 잡았으므로
   * 규칙 자체를 검사한다.
   */
  const walk = (n, fn, trail) => {
    const here = trail ? trail + " › " + n.name : n.name;
    fn(n, here);
    for (const c of n.children) walk(c, fn, here);
  };
  const label = (n) => {
    // 가장 가까운 텍스트 자식으로 위치를 특정한다
    const t = n.children.find((c) => c.type === "TEXT" && c.characters);
    const nested = n.children.map((c) => c.children.find((g) => g.type === "TEXT" && g.characters)).find(Boolean);
    const found = t || nested;
    return found ? `${n.name}("${found.characters.slice(0, 18)}")` : n.name;
  };
  walk(page, (n, trail) => {
    for (const child of n.children) {
      if (child.layoutGrow === 1 && n.primaryAxisSizingMode === "AUTO") {
        problems.push(
          `${label(n)} 안의 "${child.name}": 부모 주축이 AUTO(hug)인데 layoutGrow=1 — 0으로 접힌다\n      경로: ${trail}`,
        );
      }
      if (child.layoutAlign === "STRETCH" && n.counterAxisSizingMode === "AUTO" && n !== page) {
        problems.push(
          `${label(n)} 안의 "${child.name}": 부모 교차축이 AUTO인데 STRETCH — 늘어나지 않는다\n      경로: ${trail}`,
        );
      }
    }
  });

  // 프레임이 서로 겹치지 않는지 (배치 로직 검증)
  for (let i = 0; i < frames.length; i++) {
    for (let j = i + 1; j < frames.length; j++) {
      const a = frames[i];
      const b = frames[j];
      const overlap =
        a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
      assert.ok(!overlap, `프레임이 겹친다: "${a.name}" ↔ "${b.name}"`);
    }
  }

  /* ── 프로토타입 연결 검증 ── */
  const frameIds = new Set(frames.map((f) => f.id));
  assert.ok(reactionLog.length > 0, "프로토타입 링크가 하나도 붙지 않았다");

  for (const entry of reactionLog) {
    for (const r of entry.reactions) {
      assert.equal(r.trigger.type, "ON_CLICK", "트리거가 ON_CLICK이 아니다");
      const actions = r.actions || (r.action ? [r.action] : []);
      assert.ok(actions.length > 0, `${entry.node.name}: action이 비었다`);
      for (const a of actions) {
        // 링크가 화면 프레임이 아닌 곳을 가리키면 프로토타입에서 죽은 링크가 된다
        assert.ok(
          frameIds.has(a.destinationId),
          `${entry.node.name}: destinationId가 최상위 프레임이 아니다 (${a.destinationId})`,
        );
        assert.equal(a.navigation, "NAVIGATE");
      }
    }
  }

  // 시작점이 실제 프레임을 가리키는지
  assert.ok(page.flowStartingPoints.length >= 1, "플로우 시작점이 없다");
  for (const fp of page.flowStartingPoints) {
    assert.ok(fp.nodeId, `시작점 "${fp.name}"의 nodeId가 비었다 — key 오타일 가능성`);
    assert.ok(frameIds.has(fp.nodeId), `시작점 "${fp.name}"이 프레임을 가리키지 않는다`);
  }

  // 고립된 화면이 없는지 (도착 링크가 하나도 없는 프레임)
  const reachable = new Set();
  for (const entry of reactionLog) {
    for (const r of entry.reactions) {
      for (const a of r.actions || [r.action]) reachable.add(a.destinationId);
    }
  }
  for (const fp of page.flowStartingPoints) reachable.add(fp.nodeId);
  // catalog로 표시한 프레임은 클릭 흐름의 목적지가 아니다 (참조용 상태 모음)
  const orphans = frames
    .filter((f) => !reachable.has(f.id) && !f.pluginData.catalog)
    .map((f) => f.name);
  assert.equal(orphans.length, 0, `도착 링크가 없는 화면: ${orphans.join(", ")}`);

  /**
   * 넘침 검사.
   *
   * 화면 프레임은 높이가 고정(SCREEN_H)이고 Figma 프레임은 기본이 clipsContent=true라
   * 내용이 넘치면 아래가 잘린다. 강제 리사이즈된 값만 보면 못 잡으므로,
   * _h를 잠시 비워 내용 기반 자연 높이를 재서 비교한다.
   *
   * 스텁은 한글 텍스트 높이를 과소평가하므로 여유를 15% 둔다.
   */
  const MARGIN = 0.85;
  const overflow = [];

  // 화면 프레임뿐 아니라 크기를 고정한 모든 프레임을 훑는다.
  // 안쪽 카드·박스가 넘쳐도 Figma는 조용히 잘라낸다.
  const inner = [];
  walk(page, (n) => {
    if (n !== page && n.type === "FRAME" && (n._w != null || n._h != null)) inner.push(n);
  });
  for (const f of inner) {
    // stretch·grow로 부모가 크기를 정하는 축은 검사하지 않는다.
    // 스텁은 그 해석을 못 하므로 그대로 재면 전부 오탐이 된다.
    const pv = f.parent && f.parent.layoutMode === "VERTICAL";
    const fillsW = (pv && f.layoutAlign === "STRETCH") || (!pv && f.layoutGrow === 1);
    const fillsH = (pv && f.layoutGrow === 1) || (!pv && f.layoutAlign === "STRETCH");

    const fw = fillsW ? null : f._w;
    const fh = fillsH ? null : f._h;
    const ow = f._w, oh = f._h;
    f._w = null; f._h = null;
    const nw = f.width, nh = f.height;
    f._w = ow; f._h = oh;
    if (fh != null && nh > fh + 0.5) {
      overflow.push(`[세로] ${f.name}: 내용 ${Math.round(nh)} > 프레임 ${Math.round(fh)}`);
    }
    if (fw != null && nw > fw + 0.5) {
      overflow.push(`[가로] ${f.name}: 내용 ${Math.round(nw)} > 프레임 ${Math.round(fw)}`);
    }
  }

  for (const f of frames) {
    const forced = f._h;
    f._h = null;
    const natural = f.height;
    f._h = forced;
    f._natural = natural;
    if (natural > forced * MARGIN) {
      overflow.push(`${f.name}: 내용 ${Math.round(natural)} vs 프레임 ${forced} (여유 ${Math.round(forced * MARGIN - natural)})`);
    }
  }
  assert.equal(
    overflow.length, 0,
    "프레임 높이가 부족해 하단이 잘린다:\n  - " + overflow.join("\n  - "),
  );

  assert.equal(problems.length, 0, "API 사용 문제:\n  - " + problems.join("\n  - "));
  assert.ok(closeMessage, "closePlugin을 부르지 않았다");
  assert.ok(!closeMessage.startsWith("실패"), `플러그인이 실패했다: ${closeMessage}`);

  console.log(`✔ [${SCENARIO}] ` + closeMessage);
  console.log("✔ 폰트 선택: " + Array.from(loadedFonts).join(", "));
  console.log(`✔ 프레임 ${frames.length}개 · 노드 ${allNodes.length}개 · 텍스트 ${texts.length}개`);
  console.log("✔ 겹침 없음 · 크기 전부 유한 · fontName 순서 정상");
  console.log(`✔ 프로토타입 링크 ${reactionLog.length}개 · 모두 최상위 프레임을 가리킴`);
  console.log(`✔ 플로우 시작점 ${page.flowStartingPoints.length}개: ` +
    page.flowStartingPoints.map((f) => f.name).join(", "));
  const catalogs = frames.filter((f) => f.pluginData.catalog).map((f) => f.name);
  console.log(`✔ 모든 화면에 도착 링크 있음 (카탈로그 제외: ${catalogs.length}개)`);
  for (const f of frames) {
    console.log(
      `   ${String(Math.round(f.width)).padStart(5)}x${String(Math.round(f.height)).padEnd(4)}` +
      ` 내용 ${String(Math.round(f._natural)).padStart(4)}  ${f.name}`,
    );
  }
})().catch((e) => {
  console.error("✖ " + e.message);
  process.exit(1);
});
