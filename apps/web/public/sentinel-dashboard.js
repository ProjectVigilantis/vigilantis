// 대시보드 목업 실데이터 배선 — `GET /api/v1/assets` + `GET /api/v1/incidents`.
//
// 목업이 정적 HTML이라 React 훅을 못 쓴다. 같은 오리진이라 iframe 안에서 직접 부른다.
// 요소별 배선(목업 → 계약):
//   전체 자산      = items.length
//   인터넷 개방 SG = SgSpec.open_to_world[]가 비지 않은 SG
//   위협 판정 자산 = verdict === 'THREAT'
//   미조치 인시던트 = status ∈ {ANALYZING, AWAITING_APPROVAL, ACTION_IN_PROGRESS}
//   낭비 후보      = verdict ∈ {COST_CANDIDATE, UNUSED}
//   인벤토리       = asset_type 7종(0건 포함)
//   토폴로지       = relationships[] 6종
//   인터넷 개방 표 = open_to_world + SECURED_BY 역조인 영향 EC2
//   헬스 스코어    = health_score(EC2 전용, null은 `확인 불가`로 분리)
//   판정 현황      = verdict 분포 + 판정 불가
//
// ponytail: 집계를 여기 vanilla로 다시 썼다 — `src/lib/dashboard.ts`는 화면설계서 §4.1의
// 6지표용이고 이 화면은 목업 5지표라 지표 집합이 다르다. 지표를 §4.1로 맞추기로 하면
// 이 파일을 버리고 page.tsx를 React로 포팅해 `dashboard.ts`를 재사용한다.

const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

// 표시명·색은 화면설계서 §3.2 사전을 따른다. 빨강은 위협 의미색 전용(§0.3).
const TYPE_LABEL = {
  EC2: 'EC2 인스턴스', SG: '보안 그룹', NACL: '네트워크 ACL', EBS: 'EBS 볼륨',
  AUTO_SCALING_GROUP: 'Auto Scaling 그룹', LAUNCH_TEMPLATE: '시작 템플릿', ALB_TARGET_GROUP: '대상 그룹',
};
const TYPE_ICON = {
  EC2: 'EC2', SG: 'SG', NACL: 'ACL', EBS: 'EBS',
  AUTO_SCALING_GROUP: 'ASG', LAUNCH_TEMPLATE: 'LT', ALB_TARGET_GROUP: 'TG',
};
const TYPE_ORDER = Object.keys(TYPE_LABEL);

const VERDICT = {
  THREAT: { label: '위협', color: 'var(--red)' },
  COST_CANDIDATE: { label: '최적화 후보', color: 'var(--amber)' },
  UNUSED: { label: '미사용', color: 'var(--amber)' },
  SKIP: { label: '조치 제외', color: 'var(--text-faint)' },
};

// `collection_status` 5종 표시명. 상태 칩은 GNB(`CollectionIndicator`)로 올라갔고 여기는 상단 요약줄만 쓴다.
const COLLECTION = {
  READY: { label: '연결됨', tone: 'ok' },
  COLLECTING: { label: '수집 중', tone: 'warn' },
  PARTIAL: { label: '일부만 수집됨', tone: 'warn' },
  FAILED: { label: '수집 실패', tone: 'warn' },
  NOT_COLLECTED: { label: '수집 대상 없음', tone: 'none' },
};

/** 미조치 = 사람이 아직 손대지 않은 건. `FAILED`는 흐름이 멈춰 조치로 세지 않는다(§4.1). */
const UNHANDLED = ['ANALYZING', 'AWAITING_APPROVAL', 'ACTION_IN_PROGRESS'];

const label = (a) => a.name || a.resource_id;

async function getJson(path) {
  const res = await fetch(path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return res.json();
}

function renderState(env, incidents) {
  const c = COLLECTION[env.collection_status] || { label: env.collection_status };
  // NOT_COLLECTED면 last_collected_at이 null이다(서버 불변식) — `— (KST)`로 읽히지 않게 단위를 함께 뺀다.
  const at = env.last_collected_at
    ? `${new Date(env.last_collected_at).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hour12: false })} (KST)`
    : '—';
  $('state').textContent =
    `수집 ${c.label} · 마지막 수집 ${at} · 자산 ${env.items.length}건 · 인시던트 ${incidents.length}건`;
}

function renderMetrics(env, incidents) {
  const items = env.items;
  const openSg = items.filter((a) => a.asset_type === 'SG' && (a.spec.open_to_world || []).length > 0);
  const cells = [
    { label: '전체 자산', value: items.length, delta: 'items.length', tone: '' },
    { label: '인터넷 개방 SG', value: openSg.length, delta: '0.0.0.0/0 인바운드', tone: 'warn' },
    { label: '위협 판정 자산', value: items.filter((a) => a.verdict === 'THREAT').length, delta: 'verdict = THREAT', tone: 'crit' },
    { label: '미조치 인시던트', value: incidents.filter((i) => UNHANDLED.includes(i.status)).length, delta: '분석·승인 대기·조치 중', tone: 'warn' },
    { label: '낭비 후보', value: items.filter((a) => a.verdict === 'COST_CANDIDATE' || a.verdict === 'UNUSED').length, delta: '최적화 후보 + 미사용', tone: 'ok' },
  ];
  $('metrics').innerHTML = cells
    .map(
      (c) =>
        `<div class="metric"><div class="metric-label">${esc(c.label)}</div>` +
        `<div class="metric-value ${c.tone}">${c.value}</div>` +
        `<div class="metric-delta">${esc(c.delta)}</div></div>`,
    )
    .join('');
}

function renderInventory(env) {
  // 7종을 0건까지 전부 낸다 — 빠지면 "수집이 안 된 것"과 "원래 없는 것"이 구분되지 않는다(§4.1).
  const total = env.items.length;
  $('inv').innerHTML =
    TYPE_ORDER.map((t) => {
      const n = env.items.filter((a) => a.asset_type === t).length;
      return (
        `<div class="inv-row"><div class="inv-left"><div class="inv-icon">${TYPE_ICON[t]}</div>` +
        `${esc(TYPE_LABEL[t])}</div><div class="inv-count${n ? '' : ' muted'}">${n}</div></div>`
      );
    }).join('') +
    `<div class="inv-row"><div class="inv-left">합계</div><div class="inv-count">${total}</div></div>`;
}

function renderTopology(env) {
  const byArn = new Map(env.items.map((a) => [a.arn, a]));
  const edges = env.items.flatMap((a) =>
    a.relationships.filter((r) => byArn.has(r.target_arn)).map((r) => ({ from: a.arn, to: r.target_arn, type: r.relation_type })),
  );
  if (!edges.length) {
    $('topo').innerHTML = '<div class="empty">그릴 관계가 없습니다.</div>';
    return;
  }
  $('topo-tag').textContent = `엣지 ${edges.length}`;

  // 3열 배치: 보호 계층 ─ 컴퓨트 ─ 부착·소속. 관계에 등장하는 자산만 그린다.
  const col = (t) => (t === 'SG' || t === 'NACL' ? 0 : t === 'EC2' ? 1 : 2);
  const used = new Set(edges.flatMap((e) => [e.from, e.to]));
  const cols = [[], [], []];
  for (const arn of used) cols[col(byArn.get(arn).asset_type)].push(arn);
  for (const c of cols) c.sort((x, y) => label(byArn.get(x)).localeCompare(label(byArn.get(y))));

  // 열 간격 50px — 168폭이면 20px밖에 안 남아 엣지가 노드에 눌려 안 보인다.
  const W = 150, H = 26, GAP = 9, X = [5, 205, 405];
  const rows = Math.max(...cols.map((c) => c.length));
  const height = rows * (H + GAP) + 10;
  const pos = new Map();
  cols.forEach((c, ci) => {
    // 열마다 세로 가운데 정렬 — 짧은 열이 위로 쏠리면 엣지가 대각선으로 몰린다.
    const offset = (rows - c.length) * (H + GAP) / 2;
    c.forEach((arn, ri) => pos.set(arn, { x: X[ci], y: offset + ri * (H + GAP) + 5 }));
  });

  const line = (e) => {
    const a = pos.get(e.from), b = pos.get(e.to);
    const [x1, x2] = a.x < b.x ? [a.x + W, b.x] : [a.x, b.x + W];
    return `<line x1="${x1}" y1="${a.y + H / 2}" x2="${x2}" y2="${b.y + H / 2}" stroke="#333B41" stroke-width="1" marker-end="url(#arrow)"><title>${esc(e.type)}</title></line>`;
  };
  const node = (arn) => {
    const a = byArn.get(arn), p = pos.get(arn);
    const v = VERDICT[a.verdict];
    const stroke = v ? v.color : '#2C4A66';
    const fill = a.verdict === 'THREAT' ? '#2A1E1D' : a.verdict === 'COST_CANDIDATE' || a.verdict === 'UNUSED' ? '#28251A' : '#182430';
    const text = v && a.verdict !== 'SKIP' ? v.color : '#9DC3E8';
    return (
      `<g><title>${esc(TYPE_LABEL[a.asset_type])} · ${esc(a.arn)}</title>` +
      `<rect x="${p.x}" y="${p.y}" width="${W}" height="${H}" rx="7" fill="${fill}" stroke="${stroke}"/>` +
      `<text x="${p.x + W / 2}" y="${p.y + 17}" text-anchor="middle" fill="${text}" font-family="Inter" font-size="11">${esc(label(a))}</text></g>`
    );
  };

  $('topo').innerHTML =
    `<svg width="100%" viewBox="0 0 560 ${height}" role="img"><title>자산 토폴로지</title>` +
    '<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">' +
    '<path d="M2 1L8 5L2 9" fill="none" stroke="#5C646B" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>' +
    edges.map(line).join('') +
    [...used].map(node).join('') +
    '</svg>';
}

function renderExposure(env) {
  const open = env.items.filter((a) => a.asset_type === 'SG' && (a.spec.open_to_world || []).length > 0);
  if (!open.length) {
    $('exposure').innerHTML = '<div class="empty">인터넷에 열린 보안 그룹이 없습니다.</div>';
    return;
  }
  const rows = open
    .map((sg) => {
      const ports = sg.spec.open_to_world
        .map((r) => `${r.ipv6 ? '::/0' : '0.0.0.0/0'} ${r.protocol}/${r.from_port === r.to_port ? r.from_port : `${r.from_port}-${r.to_port}`}`)
        .join(', ');
      // 영향 자산 = 이 SG를 SECURED_BY로 가리키는 EC2 역조인(§4.1).
      const affected = env.items.filter((a) =>
        a.relationships.some((r) => r.relation_type === 'SECURED_BY' && r.target_arn === sg.arn),
      ).length;
      const v = VERDICT[sg.verdict];
      return (
        `<tr><td>${esc(label(sg))}</td><td>${esc(ports)}<br><span class="hs-sub">영향 EC2 ${affected}대</span></td>` +
        `<td>${v ? `<span class="sev"><span class="sev-dot" style="background:${v.color}"></span>${esc(v.label)}</span>` : '<span class="muted">미판정</span>'}</td></tr>`
      );
    })
    .join('');
  $('exposure').innerHTML =
    `<table><thead><tr><th>보안 그룹</th><th>개방 범위</th><th>판정</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderHealth(env) {
  // health_score는 EC2 전용 0~100 정수. null은 0이 아니라 "모른다"라서 바로 그리지 않는다.
  const scored = env.items.filter((a) => a.health_score !== null).sort((a, b) => a.health_score - b.health_score);
  const unknown = env.items.length - scored.length;
  const body = scored.length
    ? scored
        .map((a) => {
          const v = VERDICT[a.verdict];
          const color = a.health_score < 5 ? 'var(--amber)' : 'var(--teal)';
          return (
            `<div class="comp-row"><div class="hs-top"><span>${esc(label(a))}` +
            `${v ? `<span class="hs-sub">${esc(v.label)}</span>` : ''}</span><b>${a.health_score}</b></div>` +
            `<div class="comp-track"><div class="comp-fill" style="width:${a.health_score}%; background:${color};"></div></div></div>`
          );
        })
        .join('')
    : '<div class="empty">— 확인 불가</div>';
  $('health').innerHTML =
    body + `<div class="stat-pair"><span class="l">확인 불가 (null)</span><span class="r">${unknown}</span></div>`;
}

function renderVerdicts(env) {
  const total = env.items.length || 1;
  const counts = Object.keys(VERDICT).map((k) => ({
    ...VERDICT[k],
    n: env.items.filter((a) => a.verdict === k).length,
  }));
  counts.push({ label: '미판정 (null)', color: 'var(--line)', n: env.items.filter((a) => a.verdict === null).length });
  // 판정에 이르지 못한 자산 — 유형과 축이 달라 구분선 아래로 뺀다(§4.1).
  const undecidable = env.items.filter(
    (a) => a.skip_reason_code === 'SKIP_INSUFFICIENT_DATA' || a.evaluation_status === 'PENDING' || a.evaluation_status === 'FAILED',
  ).length;
  $('verdicts').innerHTML =
    counts
      .map(
        (c) =>
          `<div class="comp-row"><div class="comp-top"><span>${esc(c.label)}</span><b>${c.n}</b></div>` +
          `<div class="comp-track"><div class="comp-fill" style="width:${(c.n / total) * 100}%; background:${c.color};"></div></div></div>`,
      )
      .join('') + `<div class="stat-pair"><span class="l">판정 불가</span><span class="r">${undecidable}</span></div>`;
}

async function main() {
  try {
    const [env, inc] = await Promise.all([getJson('/api/v1/assets'), getJson('/api/v1/incidents')]);
    const incidents = inc.items || [];
    renderState(env, incidents);
    renderMetrics(env, incidents);
    renderInventory(env);
    renderTopology(env);
    renderExposure(env);
    renderHealth(env);
    renderVerdicts(env);
    $('footer').textContent =
      `GET /assets · GET /incidents 집계 — 자산 ${env.items.length}건 / 인시던트 ${incidents.length}건`;
  } catch (err) {
    // 부분 렌더 상태로 두면 남은 하드코딩 값이 실데이터로 읽힌다. 실패를 화면에 남긴다.
    const bar = $('state');
    bar.className = 'state-bar err';
    bar.textContent = `데이터를 불러오지 못했습니다 — ${err.message}`;
  }
}

main();
