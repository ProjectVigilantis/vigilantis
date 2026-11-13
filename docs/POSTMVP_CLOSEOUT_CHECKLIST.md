# Post-MVP 구현 종료 판정 대조표 (11/20(금)) — 1판

> **담당**: 박지현 (QA) — 이슈 [#619](https://github.com/ProjectVigilantis/vigilantis/issues/619)
> **판정 주체**: 김세혁(PM). 판정 기록은 SSOT [PROJECT_STATUS.md](PROJECT_STATUS.md)에 PM이 남긴다.
>
> **이 문서는 판정 기준을 정의하지 않는다.** 11/20(금) 판정 기준은 15주차 전환 갱신(11/16(월))에서 PM이 세우며, 이 문서는 **그 기준에 답할 때 볼 칸과 근거의 자리**다. 선례는 [MVP_CLOSEOUT_CHECKLIST.md](MVP_CLOSEOUT_CHECKLIST.md)(#422)다.
> 이슈의 완료 조건 체크박스, PR 본문, 테스트 실행 결과를 여기에 복제하지 않는다. **어느 칸이 답하는지만 가리킨다.**
> 빈칸과 「없음」은 **미확인 또는 근거 없음**이다. 채울 때는 결과나 사유를 적는다.
>
> **상태 기준**: 2026-11-13(금) 16:30 조회 · `dev` `b54d554`(PR #634 머지). 이슈 완료 조건 수는 본문 체크박스를 센 값(체크/전체)이다. 날짜가 지나면 다시 조회한다(§5 갱신 계획).

---

## 0. 판정 대상의 원천

| 항목 | 원천 | 상태 |
| --- | --- | --- |
| 11/20(금)이 무엇을 묻는 날인가 | SSOT §현재 위치 — 15주차(11/16(월)-11/20(금))는 실 계정 실측과 Post-MVP 구현 종료 판정 주 | 확정 |
| 판정 기준 | SSOT §현재 위치 15주차 판정 기준 표 | **미확정** — 11/16(월) 갱신에서 세운다. 그 전까지 이 표는 칸만 준비한다 |
| 무엇을 판정하나 | #479 완료 조건 4칸 — ① §1 T-1-T-17 완료 기준 전부 체크 ② 시연 흐름 관통(랜딩 → 로그인 → 고객 계정 연결 → 관제 · 승인 → 외부 알림 → 구독 정지 시 원격 회수) ③ 판정 기준 확정본과 결과가 SSOT에 있다 ④ §2 미착수 항목의 처분 | 확정(본문) · 체크 0/4 |
| T-n 번호와 완료 기준 | #479 본문 §1 · 단계(①②③)는 SSOT §Post-MVP 범위 | 확정 |
| 11/16(월) 뒤 실측 칸을 판정에 넣는가 | SSOT §확정 결정 로그 2026-11-06 행 — 전부 11/20(금) 판정에 넣는다 | 확정 |
| 11/13(금) 구현 마감 점검 | SSOT §현재 위치 14주차 카드 · §확정 결정 로그 2026-11-13 행 | 확정 |
| 시연 흐름 실측 기록 | #563(닫힘) 1차 · 2차 · 3차 코멘트 | 기록 완료(11/2-11/4 · 화면 흐름) |

---

## 1. T-1 - T-17

「테스트」 칸은 파일명과 함수 수, 그리고 **완료 기준에 대응하는 대표 함수명**이다. 실행 결과는 PR의 CI가 말한다. 경로를 줄여 썼다 — 접두 없는 파일은 `apps/core-api/tests/`, `services/` · `security/` · `db/`는 `apps/core-api/` 아래, `root/`는 저장소 최상위 `tests/`다.
「처분」 칸은 11/13(금) 회의 기준이다. **완료** = 이슈 완료 조건 전부 체크 · **실측 대기** = 코드는 머지, 실 계정 확인이 11/16(월) 뒤 · **체크 없음** = 닫혔지만 완료 조건이 체크되지 않음(§4).

| T | 단계 | 대상 | 이슈(완료 조건) | 머지 PR | 테스트 | 실측 · 근거 | 빈 곳 | 처분 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-1 | ① | 로그인 + 역할 | #492 열림 **4/5** · #490 닫힘 · #515 닫힘 7/7 | #499 #502 #512 #527 #553 #559 | `test_auth_login_flow.py` `test_enrolled_user_completes_and_revokes` · `test_third_totp_failure_ends_flow` / `test_auth_api.py` `test_idle_expiry_and_get_me_never_extends` · `test_logout_revokes_and_is_idempotent` / `test_auth_login_single_session.py` `test_second_login_from_another_browser_supersedes_the_first` / `test_staff_admin_api.py` `test_disable_revokes_sessions_and_management_immediately` | #563 2차(로그인 · 401 · 403) | 실제 Cognito · HTTPS 통합 검증과 OWASP 반영 항목 기록(#492 5번) | 실측 대기 — 11/16(월) 뒤 · 안성일 |
| T-2 | ② | 승인자 감사 기록 | #544 열림 **4/5** | #546 #547 #548 · 화면 #578 | `test_audit_events.py`(16) `test_user_approval_records_one_person_event` · `test_rejected_approval_leaves_no_event` · `test_rows_reject_update_and_delete_but_insert_is_allowed` / `test_audit_events_api.py`(13) `test_tenant_viewer_sees_only_its_tenant_and_never_an_operator_email` | #563 2차 8번 행 · 3차(`/logs`) | (C) 인시던트 상세와 감사 목록 화면에서 승인자 · 시각 · 대상(#544 5번) | 11/20(금)까지 · 김승철 · 김세혁 |
| T-3 | ② | 요청 속도 제한(WAF) | #484 닫힘 **0/6** · #522 열림 **2/4** | #486 #538 #549 | `root/test_provision_platform_aws.py`(132) `test_new_acl_waits_while_the_new_ip_sets_propagate` · `test_login_rate_limit_counts_only_the_login_and_reset_paths_per_ip_without_the_nat_ip` · `test_rate_limit_counts_per_ip_but_never_the_nat_ip` · `test_second_run_changes_nothing` · `test_switching_to_block_is_applied_on_rerun` | 없음 | (B) Count 관측 뒤 Block 전환과 관측 기록 · (C) 로그인 경로 한도 초과 시험 | 실측 대기 — 11/16(월) 뒤 · 김승철. #484는 체크 없음(§4) |
| T-4 | ① | 고객 계정 연결 · 원격 회수 | #485 닫힘 5/5 · #533 닫힘 10/10 · #535 닫힘 **4/5** · #536 닫힘 5/5 · #495 · #510 · #518 닫힘 | #489 #507 #516 #506 #513 #519 #543 #550 #558 #569 #575 · 화면 #503 #541 | `test_tenants_api.py` `test_create_returns_pending_tenant_with_external_id_and_onboarding_link` / `test_tenant_verification.py` `test_every_step_passes_is_ok_and_runs_in_contract_order` · `test_assume_refusal_is_assume_denied_and_stops_there` / `test_tenant_verification_api.py` `test_resume_ok_activates_and_clears_suspension` / `test_tenant_suspension_gate.py`(18) `test_start_gate_refuses_every_new_execution_but_not_auto_rollback` / `root/test_onboarding_template.py`(16) `test_role_trusts_only_the_platform_principal_with_the_external_id` | #563 3차 4번(학원 계정 연결 확인 · 데모 호스트) | 플랫폼 멤버 계정에서 학원 계정 실연결 · 정지 → 재개 | 실측 대기 — 11/16(월) 뒤 · 안성일. #535 1칸은 T-5 17번이 대신 고정(§4) |
| T-5 | ① | 테넌트 격리 회귀 테스트 | #529 닫힘 6/6 | #534 #557 #574 | `test_tenant_isolation.py`(18) `test_list_hides_other_tenants_incidents` · `test_execute_on_other_tenant_is_404_and_writes_nothing` · `test_viewer_on_other_tenant_is_403_not_404` · `test_suspended_tenant_execute_is_409_for_its_operator` · `test_unauthenticated_list_is_401` | PR #574 CI | 없음 | 완료 |
| T-6 | ② | 고객별 자동 조치 끄기 | #514 닫힘 **0/8** · #521 닫힘 **0/6** · #532 닫힘 5/5 | #520 #524 #540 | `test_tenant_auto_action.py` `test_disabled_tenant_never_reserves_an_automatic_isolation` · `test_human_approval_still_executes_on_a_disabled_tenant` · `test_deadline_that_passed_while_disabled_does_not_fire_after_re_enabling` · `test_preparation_stops_before_any_aws_step_when_disabled` / `test_incident_auto_isolation_block.py` `test_disabled_tenant_shows_the_block_and_the_gate_agrees` | 없음 | 없음 — 근거는 왼쪽 함수 | 체크 없음(§4) |
| T-7 | ② | 고객 설정 화면(최소판) | #564 닫힘 7/7 | #576 | `test_tenant_settings_api.py`(22) `test_protected_tags_omit_replace_clear_and_null` · `test_customer_operator_and_viewer_are_forbidden_before_validation` / `test_tenant_settings_runtime.py`(20) `test_newly_enabled_channel_works_without_restart` · `test_email_reaches_ready_only_after_recipient_confirmation_and_unsubscribe_is_detected` | 없음 | 없음 | 완료 |
| T-8 | ① | 중앙 계정 구조 · 플랫폼 이전 | #494 열림 **5/7** | #523 #537 #607 | `root/test_provision_platform_aws.py`(132) `test_with_a_domain_cloudfront_reaches_the_alb_over_https_only` · `test_https_listener_replaces_the_plain_listener` · `test_new_db_keeps_a_week_of_automated_backups` · `test_every_alarm_notifies_on_trouble_and_on_recovery` · `test_new_db_is_multi_az` · `test_existing_single_az_db_is_switched_to_multi_az` | 없음 | `vigilantis.dev` A 레코드 · CloudFront → ALB 구간 HTTPS 실 적용 | 실측 대기 — 11/16(월) · 김세혁 |
| T-9 | ① | 외부 알림 | #493 열림 **4/5** | #498 #500 #505 #509 #517 | `test_notification_delivery.py` `test_committed_high_intake_is_sent_once_per_configured_channel` · `test_routes_never_cross_customers_and_changed_destination_gets_no_old_events` · `test_crash_after_claim_is_recovered_as_unknown_and_retried` / `test_notification_routing.py` `test_two_tenants_route_to_own_destinations` · `test_blocked_without_fallback` | #563 2차 7번(Slack 3건) | 팀 환경의 실제 Slack · 이메일 수신 확인(#493 5번) | 실측 대기 — 11/16(월) 뒤 · 안성일 |
| T-10 | ③ | 실행 직전 재검증 · 동시 실행 제한 | #595 닫힘 **0/6** | #600 | `test_execution_revalidation.py`(20) `test_gate_refuses_when_the_latest_verdict_is_no_longer_a_candidate` · `test_rightsizing_stops_when_the_type_changed_while_waiting` · `test_approval_on_a_busy_target_is_refused_without_side_effects` · `test_recovery_of_a_rollback_initiated_origin_is_not_blocked_by_itself` · `test_concurrent_reservation_waits_for_the_target_lock_and_then_refuses` | 없음 | 없음 — 근거는 왼쪽 함수. 실행 확인 모달의 409 처리(#595 6번)는 화면이라 함수 근거를 찾지 못함 | 체크 없음(§4) |
| T-11 | ② | 차단 만료(고정 24시간) | #597 닫힘 7/7 | #603 #606 #628 | `test_nacl_deny_expiry.py`(19) `test_an_expired_block_is_released_in_one_cycle` · `test_a_block_younger_than_24_hours_is_left_alone` · `test_a_block_released_by_an_operator_is_not_picked_up` · `test_a_busy_target_defers_the_expiry` / `test_nacl_deny_expiry_localstack.py`(1) `test_an_expired_deny_entry_is_removed_from_localstack` | 없음 | 없음 | 완료 |
| T-12 | ① | 종료 인스턴스 후보 수정 | #453 닫힘 **0/8** | #483 #526 | `services/tests/test_rule_engine.py`(17) `test_non_running_state_is_not_a_candidate` · `test_state_check_precedes_insufficient_data_and_prod` · `test_ec2_and_ebs_share_the_hold_code` · `test_already_rightsized_idle_is_not_a_candidate_again` | #453 QA 확인 코멘트(10/29 · 박지현) | 없음 — 근거는 왼쪽 함수와 QA 코멘트 | 체크 없음(§4) |
| T-13 | ③ | 사이즈별 예상 절감액 | 이슈 없음 — 11/2에 문서 · 발표 자료로 줄였다(SSOT §확정 결정 로그 2026-11-02) | 없음(코드 변경 없음) | 없음 | #479 코멘트 `6571219978` | 실제 비용 수집은 #572 11번이 맡는다 | 완료(축소 범위) |
| T-14 | ① | 실제 위협 원천(GuardDuty) | #588 열림 **6/7** | #593 #615 #624 | `services/tests/test_guardduty.py`(18) `test_a_new_detector_has_every_optional_feature_off` · `test_other_accounts_and_archived_findings_are_not_read` / `security/tests/test_guardduty_finding.py`(10) `test_severity_bands` / `test_guardduty_ingress.py`(21) `test_every_finding_is_an_event_and_only_medium_and_above_become_incidents` · `test_first_read_looks_back_one_day_and_the_cursor_moves_to_the_latest_update` · `test_high_guardduty_incident_is_excluded_before_any_aws_call` / `root/test_onboarding_template.py` `test_guardduty_cannot_be_turned_off_with_this_role` | #479 코멘트 `6567717200`(조사) | 학원 계정에서 켜기와 결과 읽기 1회(#588 7번) | 실측 대기 — 11/16(월) 뒤 · 김승철 |
| T-15 | ① | 랜딩 · 처리방침 · 약관 | 이슈 없음 — 근거는 SSOT §확정 결정 로그 2026-11-06 행 | #504 #542 #573 #604 | web `legal.test.ts` — 시행일 상수 · 보호책임자 이메일 · 역할 이름 | #563 1 · 2번 행 | 약관 자산 확장 조항(#636 · 11/13(금) 회의 배정) · 도쿄 DB 복제본의 국외 이전 고지 여부(11/13(금) 회의 안건) | 완료(시행일 11/16) · 뒤의 둘은 별건 |
| T-16 | ② | 구독 현황 화면 | #528 닫힘 4/4 · #608 열림 **0/7** | #579 #602 | `test_subscription_workflow.py`(26) `test_purchase_is_pending_and_grants_no_rights` · `test_disabling_renewal_keeps_rights_until_the_exact_term_end_then_expiry_suspends_the_service` / `test_subscription_api.py`(9) `test_suspended_customer_reads_and_disables_renewal_but_cannot_buy_or_stop` / `test_subscription_scheduler.py`(7) | 없음 | #608 유료 권리 없는 고객사 정리(착수 시점 미정) | #528 완료 · #608은 §3 |
| T-17 | ① | SaaS 전환 ADR | #491 닫힘 5/5 | #511 #525 | 없음(문서) | ADR-0012 `Accepted` | 없음 | 완료 |

---

## 2. T-n 밖의 추가 기능

SSOT §Post-MVP 범위의 2단계와 §확정 결정 로그(10/29 · 11/2 · 11/6 · 11/13)에서 들어온 항목이다.

| 항목 | 이슈(완료 조건) | 머지 PR | 테스트 | 빈 곳 | 처분 |
| --- | --- | --- | --- | --- | --- |
| 도입 신청 · 고객 문의 · 운영자 알림 | #551 닫힘 · #552 열림 **7/8** · #565 열림 **13/14** | #560 #571 #609 | `test_signup_requests_api.py` `test_create_returns_202_and_stores_signup_with_notification` · `test_decision_preserves_actor_email_and_is_private` / `test_inquiries_api.py` `test_tenant_accounts_get_403_on_management_routes_even_for_own_inquiry` / `test_operator_notifications_api.py` `test_reads_are_per_user` | 화면의 실서버 연결(#552) · 목록 페이지 이동과 크기 선택(#565) | 11/16(월) 전 · 김세혁(FE 이슈 하나로 모음) |
| 플랫폼 활동 로그 | #589 열림 **3/7** | #592 #620 #617 #618 #621 #627 #634 · 열림 #630 | `test_platform_events.py`(34) `test_record_writes_one_row_with_the_request_origin` / `test_platform_events_api.py`(14) `test_manager_sees_assigned_tenants_and_only_own_tenantless_rows` / `test_workflow_platform_events.py`(28) `test_a_failed_platform_event_write_leaves_no_approval_behind` / `test_tenant_platform_events.py`(33) · `test_user_staff_platform_events.py`(17) · `test_signup_platform_events.py`(10) · `test_inquiries_platform_events.py`(10) | 로그인 · 세션 · 권한 거절 기록(PR #630 열림) · 멱등 재요청 무중복 · 실제 접속지 주소(11/16(월) 뒤) · 스키마와 FE 타입 맞춤 · 화면 | 서버 11/16(월)까지 · 김승철. 접속지 확인과 화면은 11/20(금) |
| ISMS 보고서 | #488 열림 **2/3** · #622 열림 **4/5** · #623 열림 **11/12** | #616 #629 #631 | `test_evidence_export.py`(31) `test_unit_secrets_are_masked_in_both_formats` · `test_other_tenant_operator_gets_404_not_403` / `test_report_draft.py`(18) `test_download_has_three_sections_provenance_and_blank_human_fields` · `test_default_client_is_unavailable_but_missing_target_still_404` | 근거 조회 · 내보내기 화면(#488) · 작성 모델 선정과 실제 제공자 호출(#623) | 모델 연결과 화면은 11/20(금) · 안성일 · 김세혁 |
| 위협 종류 · ISMS 작성 시나리오 | #613 열림 **2/6** | #614 | 없음(문서) | 2판(분류표 전 항목 시나리오) · 안성일 확인 · 못 채우는 칸 넘기기 · 문서 지도 등재 요청 | 2판 11/18(수) · 박지현 |
| FE에서 나온 서버 기능 11건 | #572 열림 11/11 | #609 #586 외(항목별 PR은 #572 본문) | `test_tenant_groups_api.py`(16) `test_central_builds_a_group_scope_end_to_end` | 전부 체크됐으나 이슈가 열려 있다 — 닫을지는 담당 판단 | 근거 있음 |
| ADR-0005 1차 개정 구현 | #583 열림 **14/15** | #596 #612 #632 | `ai/tests/test_finops_stage3_evaluation.py`(10) `test_no_proposal_comparison_requires_same_inputs_and_flags_pairing` · `ai/tests/test_secops_graph.py` · `ai/tests/test_secops_evaluation.py` | 대비책 그래프와 전환 스위치 제거(#583 15번) | 11/20(금) · 안성일 |
| 보호 태그 자산의 자동 격리 예외 | #598 열림 **1/5** | 없음 | 없음 | 구현 전부 | 착수 시점 미정(§3) |
| 플랫폼 이중화 | 이슈 없음 — ADR-0010 5차 개정 | 서울 #607 · 도쿄 없음 | `root/test_provision_platform_aws.py` `test_new_db_is_multi_az` · `test_existing_single_az_db_is_switched_to_multi_az` | 도쿄 DB 복제본 · 이미지와 비밀 복제 · 전환 절차 · 전환 확인 둘(로그인 · 진행 중 실행) | 11/20(금) 판정 안 · 김세혁 · 안성일 |
| AWS Marketplace 실연동 구조 | 이슈 없음 | #610 | `test_marketplace.py`(15) `test_real_entitlement_projects_before_mock_and_ends_in_suspension` · `test_operator_patch_cannot_overwrite_marketplace_projection` | 실제 상품 등록 뒤의 확인은 범위 밖 | 근거 있음(구조) |
| 유휴 자산 알림 기간 하루로 | #625 열림 **0/5** | #626 | `services/tests/test_rule_engine.py` `test_one_day_of_hourly_datapoints_is_judged` · `test_min_datapoints_boundary_is_exclusive_below` · `test_default_window_yields_enough_datapoints_for_the_threshold` | 없음 — PR은 머지됐고 체크 표시만 남았다 | 체크 없음(§4) |
| 약관 자산 확장 조항 | #636 열림 **0/5** | 없음 | 없음(약관 문구) | 결정 요청 3건의 답 · 조항 PR | 11/13(금) 회의 배정 · 박지현 |

---

## 3. 시연 흐름 관통 — #479 완료 조건 ②

| 구간 | 증빙(지금) | 빈 곳 | 담당 · 시점 |
| --- | --- | --- | --- |
| 랜딩 → 로그인 | #563 1 · 2번 행(11/2-11/4 · 데모 호스트) | 운영 도메인(`vigilantis.dev`)에서의 확인 | T-8 김세혁 · T-1 안성일 · 11/16(월) |
| 고객 계정 연결 | #563 3차 4번(학원 계정 · 데모 호스트) | 플랫폼 멤버 계정에서의 실연결 | T-4 안성일 · 11/16(월) 뒤 |
| 관제 · 승인 | #563 2차 6번(Medium 승인 → 차단 → 복원 · High 격리) | 실 고객 계정 자산에서의 1회 | 관통 1회에 포함 |
| 외부 알림 | #563 2차 7번(Slack) | 이메일 실수신 | T-9 안성일 · 11/16(월) 뒤 |
| 구독 정지 → 원격 회수 | 테스트만 — `test_tenant_suspension_gate.py` · `test_tenant_isolation.py` 10-14번 · `test_subscription_workflow.py` | 화면과 실 계정 실측 없음 | 관통 1회에 포함 |
| **한 번에 관통** | 없음 | 전 구간 | **김세혁(PM)** · 11/16(월) 뒤(SSOT §확정 결정 로그 2026-11-13). 날짜는 15주차 판정 기준에서 정한다 |

첫날 확인 순서는 #479 코멘트 `6784135478`(박지현 초안)이 앞 네 구간을 밟는다. 순서의 확정(14주차 판정 ⓖ)은 11/16(월) 갱신에 있다.

---

## 4. 닫혔지만 완료 조건이 체크되지 않은 이슈

#479 완료 조건 ①은 「T-n 완료 기준 전부 체크」다. 아래 이슈는 PR이 머지되고 닫혔지만 본문 체크박스가 비어 있다. **이 문서는 체크를 대신하지 않는다** — 체크는 각 이슈 담당의 몫이고, 판정 때 이 칸을 체크박스로 셀지 §1의 테스트 함수로 볼지는 PM이 정한다.

| 이슈 | T | 체크 | 담당 | 근거(§1 행) | 담당 답 |
| --- | --- | --- | --- | --- | --- |
| #453 | T-12 | 0/8 | 김승철 | T-12 행 함수 4개 · QA 확인 코멘트 | 미문의 |
| #484 | T-3 | 0/6 | 김승철 | T-3 행 함수 — 5 · 6번(실 적용 뒤 집계 확인 · Block 전환)은 #522로 넘어감 | 미문의 |
| #514 | T-6 | 0/8 | 김승철 | T-6 행 `test_tenant_auto_action.py` | 미문의 |
| #521 | T-6 | 0/6 | 김승철 | T-6 행 `test_incident_auto_isolation_block.py` | 미문의 |
| #595 | T-10 | 0/6 | 김세혁 | T-10 행 `test_execution_revalidation.py` | 미문의 |
| #535 | T-4 | 4/5 | 안성일 | 남은 1칸은 `test_tenant_isolation.py` 17번이 기대값으로 쓴다 | 미문의 |
| #563 | — | 4/5 | 박지현 | 남은 1칸(「다음에 또 나올 것」을 대조표로 넘김)은 이 문서 §3 「빈 곳」 칸이 받는다 | 이 문서 머지 뒤 체크 |
| #625(열림) | — | 0/5 | 김승철 | §2 유휴 자산 행 함수 3개 | 미문의 |

「담당 답」 칸은 #619 완료 조건 6번(담당에게 묻고 답 또는 미답 사실을 적는다)의 자리다.

---

## 5. 미착수 · 이월 처분 — #479 완료 조건 ④

처분(이월 / 폐기)은 PM이 #479 코멘트로 남긴다. 여기는 코멘트 번호를 받을 칸만 둔다.

| 항목 | 지금 | 처분 코멘트 |
| --- | --- | --- |
| #479 §2 추가 구현 후보 | 처분 코멘트 없음 | |
| #598 보호 태그 자산의 자동 격리 예외 | 처분 방식 확정 · 구현 착수 시점 미정 | |
| #608 유료 권리 없는 고객사 정리 | 착수 시점 미정 | |
| 메일 알림(인시던트 생성 · 연결 완료 · 구독 결제 재촉) | 담당 미정 — 11/16(월) 갱신에서 정리 | |
| 랜딩 사용 가이드 | 담당 미정 — 11/16(월) 갱신에서 정리 | |
| AI 호출을 고객 계정 Bedrock으로 옮기는 구현 | 안성일(11/13(금) 회의) | |
| 도쿄 서브 리전(파일럿 라이트) | 11/20(금) 판정 안 · 김세혁 | |

---

## 6. 집계 (11/13(금) 기준)

| 구분 | T-n | 수 |
| --- | --- | --- |
| 완료 — 이슈 완료 조건 전부 체크 또는 결정으로 닫힘 | T-5 · T-7 · T-11 · T-13 · T-15 · T-16(#528) · T-17 | 7 |
| 근거 있음 · 체크 없음(§4) | T-6 · T-10 · T-12 | 3 |
| 머지됐고 실 계정 확인이 남음(11/16(월) 뒤) | T-1 · T-3 · T-4 · T-8 · T-9 · T-14 | 6 |
| 화면이 남음 | T-2 | 1 |
| 미착수 | 없음 | 0 |

추가 기능 11행 가운데 이슈 완료 조건이 전부 체크된 것은 #572 하나다. 나머지의 남은 칸은 §2 「빈 곳」에 있다.

---

## 7. 갱신 계획

| 판 | 언제 | 무엇을 |
| --- | --- | --- |
| 1판(이 문서) | 11/17(월)까지 | 표 전체 · 11/13(금) 구현 마감 기준 |
| 갱신 | 11/19(목) 회의 전 | 11/13(금) 뒤 머지분 · 11/16-11/18 실측 코멘트 번호 · 15주차 판정 기준과의 대응 · §4 담당 답 |
| 처분 반영 | 11/20(금) 판정 뒤 | 판정 결과 · §5 처분 코멘트 번호 |

## 변경 이력

| 날짜 | 내용 |
| --- | --- |
| 2026-11-13 | 1판 초안 — `dev` `b54d554` 기준. 이슈 45개 · PR 80건의 상태와 테스트 함수명을 조회해 채웠다 |
