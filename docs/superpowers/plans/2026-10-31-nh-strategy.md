# 나무·200티큐 구현 계획

> For agentic workers: use superpowers:subagent-driven-development or superpowers:executing-plans. Independent provider, strategy, delivery work is delegated; root integrates imports and UI.

**Goal:** 나무 조회, 200티큐 판단, 실제 초기 기록, clone 실행을 하나의 Windows 앱으로 제공한다.

**Architecture:** 기존 schema_version=1을 유지한다. 계좌의 선택적 전략 필드로 진행 상태를 비공개 원장과 동기화한다. API 잔고와 가격은 로컬 캐시, 비밀값은 DPAPI, 초기 개인 데이터는 공개 Git 밖에 둔다.

**Tech Stack:** Python 3.12, tkinter/ttk, SQLite, Decimal, openpyxl, Windows DPAPI/OCR, GitHub Actions/PyInstaller.

**Spec:** ../specs/2026-10-31-nh-strategy-design.md

## Global Constraints

- 개발 세션에서는 실제 금융 주문을 보내지 않는다. 앱 LIVE는 사용자 로컬 활성화가 필요하다.
- 개인 원장과 원본 파일을 공개 저장소·CI에 게시하지 않는다.
- 숫자 추정/0 대체로 초기 데이터 오류를 숨기지 않는다.
- 나무 운영키가 없는 실제 조회 결과를 검증했다고 주장하지 않는다.
- 기존 데이터와 수동 계좌를 보존한다.

## Review Focus

- 잔고 스냅샷이 기존 체결과 중복되어 보유량이 두 배가 되지 않아야 한다.
- 기존 Google Sheet와 NH 체결의 동일 거래는 무조건 추가하지 않고 대조한다.
- 미완성/분할 미확인 일봉에서 확정적인 전략 신호가 나오지 않아야 한다.
- 익절 확인은 실제 해당 계좌 체결에 연결되며 같은 단계가 반복되지 않아야 한다.
- 오래된 CI 빌드는 최신 소스용 EXE를 덮어쓰지 않아야 한다.

## Task 1: 공식 나무 조회 확장

- [ ] 계좌 목록, 국내/미국 잔고, 미국 시세·일봉·체결 공식 명세 확인.
- [ ] providers.py와 test_providers.py에 계약·페이지 처리·오류 검사 추가.
- [ ] discover_accounts / get_holdings / overseas_trades / overseas_daily_prices 구현.
- [ ] 테스트 성공과 실제 인증 미검증 범위를 기록.

## Task 2: 초기 입력과 계좌 연결

- [ ] 실제 Google Sheet 파싱, 분류, 보류 건 사유 검사.
- [ ] initialization.py / broker_accounts.py 및 tests에 중복·보류·잔고 중복 테스트.
- [ ] 초기 입력 패키지, 기존 계좌 연결, 잔고 비교, 검토 체결 UI 통합.
- [ ] 개인 초기 파일 저장, 공개 Git에 포함되지 않았는지 검사.

## Task 3: 200티큐

- [ ] strategy.py Decimal 상태 판정/수량 제안 및 실제 체결 확인 테스트.
- [ ] strategy_ui.py 계좌별 화면·나무/CSV 일봉·수익률 입력·단계 확인.
- [ ] app.py 전략 탭 연결, smoke.py에 7번째 화면과 전략 검증 추가.
- [ ] 원문 기준과 미정 구현 선택을 docs/strategy-200tqqq.md에 설명.

## Task 4: clone 실행

- [ ] 검증된 EXE와 SHA256 manifest만 게시하는 CI/helper 구현.
- [ ] 오래된 소스/경합/변조 산출물 실패 검사.
- [ ] 버전 0.2.0, 사용 안내, 전체 검사와 코드 리뷰.
- [ ] 저장소 반영 후 Windows CI 성공, 실제 EXE Git blob 및 clone 검증.

## 진행 기록

- 공식 PDF와 실제 Google Sheet를 찾았으며 원본은 수정하지 않음.
- 공개 코드 저장소에 개인 데이터는 넣지 않는 설계를 적용.
- 분할 보정 미확인 NH 일봉은 unknown으로 명시하는 것으로 결정.
- 추가 요청: 첨부 Toss 3.96의 자동 실행 구조를 참고해 아래 작업 추가.

## Task 5: 자동 실행 계층

- [ ] 참고 소스 읽기, 원문 전략과 다른 기본값 문서화.
- [ ] automation.py: 로컬 주문 계획/의도 영속화, 중복 실행, 부분·불명·재시작 대조 테스트.
- [ ] nh_execution.py: 공식 미국 현금 지정가, 운영/모의 분리, 응답 불명 재시도 금지 테스트.
- [ ] automation_ui.py: DRY_RUN 기본, 현재 세션 LIVE 활성화, 상태·중지·대조 화면.
- [ ] 시장 달력/실제 잔고·현금/자료 확인을 앱과 연결하고 실제 체결만 원장에 반영.
