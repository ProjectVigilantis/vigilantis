// AST-001 목록 필터 — 화면설계서 v1.6 §4.2. 판정 대상 판별이 여기 있습니다.

import type { AssetItem } from '@/types/api';

/**
 * 목록 뷰에 담기는 자산인가. **기준은 `evaluation_status`이지 `resource_role`이 아니다.**
 *
 * 계약에서 두 값은 다른 축이다(`packages/schemas/api/assets.py`):
 *
 * | 축 | 값 | 뜻 |
 * | --- | --- | --- |
 * | `_PRIMARY_TYPES` | `{EC2, SG}` | `resource_role = PRIMARY`. **EBS는 여기 없다** |
 * | `_RULE_TARGET_TYPES` | `{EC2, SG, EBS}` | Rule 판정 대상. **EBS는 여기 있다** |
 *
 * `resource_role = RUNBOOK_SUPPORT`는 "판정 안 함"이 아니라 **"실행·ARN Match·토폴로지 지원"**
 * 이고, EBS는 그 안에 있으면서도 `verdict`가 나오는 판정·과금 대상이다.
 *
 * 그래서 `resource_role`로 거르면 **미연결 EBS가 목록에서 사라진다** — 비용이 계속 청구되는
 * 대표 낭비 후보이자 `RUNBOOK_EBS_DELETE_UNATTACHED`의 실행 대상이라, v1.6 회의 결정
 * ("빌링이 없는 것을 뺀다")과 정반대가 된다.
 *
 * `NOT_APPLICABLE`은 계약상 정확히 **NACL · Auto Scaling 그룹 · 시작 템플릿 · ALB 대상 그룹**
 * 4종에만 붙으므로 그 결정과 일치한다.
 */
export function isJudgedAsset(asset: Pick<AssetItem, 'evaluation_status'>): boolean {
  return asset.evaluation_status !== 'NOT_APPLICABLE';
}
