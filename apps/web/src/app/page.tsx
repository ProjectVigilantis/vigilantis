// DSH-001 메인 대시보드 — 레이아웃은 공개 아티팩트(`cloud-asset-dashboard`)에서 왔고,
// 값은 `public/sentinel-dashboard.js`가 `GET /assets`·`GET /incidents`로 채운다.
//
// ⚠️ **화면설계서 §4.1의 Hero/Data 2구역 구성이 아니다.** 아티팩트 레이아웃을 그대로 쓰기로 한
// 결정(2026-09-07)이라 지표 집합이 §4.1의 6지표와 다르다. 계약에 소스가 없던 3요소는 계약 있는
// 값으로 대체했다 — 과도한 권한 역할 → 미조치 인시던트, 컴플라이언스 → 낭비 후보,
// IAM 현황 → 헬스 스코어. 취약점·컴플라이언스·IAM은 SSOT MVP 관제 범위(EC2·SG + CloudWatch)
// 밖이라 만들지 않았다.
//
// 정적 HTML을 iframe으로 띄우는 이유: 아티팩트의 전역 리셋(`*{margin:0;padding:0}`)과
// `html,body` 규칙이 JSX로 옮기면 GNB·토스트까지 덮어쓴다.
export default function DashboardPage() {
  return (
    <iframe
      src="/sentinel-dashboard.html"
      title="자산 현황 대시보드"
      // 부모 <main>의 `p-6`를 되돌린다 — 아티팩트가 자기 여백(28px)만 쓰게.
      className="-m-6 flex-1 border-0"
    />
  );
}
