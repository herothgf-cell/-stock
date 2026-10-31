# 주식 매매일지 · Stock Journal

Windows에서 사용하는 개인 매매일지입니다. 계좌별 거래·보유량·종목별 역대 손익·배당을 관리하고, 별도의 **비공개 GitHub 저장소**로 여러 PC의 기록을 동기화합니다.

## 실행

[Releases](https://github.com/herothgf-cell/-stock/releases)에서 `StockJournal-Windows.zip`을 받고 압축을 푼 다음 `StockJournal.exe`를 실행합니다. 실행 파일에는 Python이 포함됩니다. 아직 성공한 Release가 없으면 실행 파일 배포가 완료되지 않은 상태입니다. [Windows build](https://github.com/herothgf-cell/-stock/actions/workflows/windows.yml)에서 빌드 결과를 확인할 수 있습니다.

## 기능

| 구분 | 내용 |
| --- | --- |
| 계좌 | 일반·ISA·연금저축·IRP·DC 등 별칭과 증권사 관리 |
| 매매 | 매수·매도·기초 보유 입력, 수정·삭제, CSV 내보내기 |
| 보유·손익 | 계좌 필터, 이동평균 원가, 연결 물량 원가, 매도 완료 이력, 비용·실제 배당 반영 |
| 가져오기 | 기존 엑셀, CSV, 카톡 텍스트, Windows OCR 이미지 → 검토·수정 후 저장 |
| 배당 | 실제 수령액·세금과 참고/공시 일정 분리, CSV, 보유 종목 일정, ETFcheck 링크 |
| 종목·시세 | NH 공식 국내 종목 목록, 나무 PLUG 현재가 주기 조회, 수동 가격 입력 |
| 체결 조회 | NH 공식 일별 주문 체결 어댑터, 누적 체결 검토·갱신 |
| 보관 | SQLite, 변경 전 백업, JSON 복원, 비공개 저장소 동기화 |

통화별로 따로 계산합니다. 가격이 없는 보유 종목은 평가금액을 `미조회`로 표시합니다. 실제 증권 주문을 제출하는 프로그램은 아닙니다.

## 처음 사용하는 순서

1. **설정 · 계좌**에서 계좌를 등록합니다. 기존 엑셀이 있으면 **일괄 가져오기**부터 시작할 수도 있습니다.
2. **종목 관리 → 공식 종목 목록 받기** 또는 직접 입력으로 종목을 등록합니다.
3. 매매를 입력하거나 파일·텍스트·이미지 결과를 검토한 뒤 저장합니다.
4. NH 키가 있으면 앱 설정에 입력하고 현재가·체결 조회를 사용합니다.
5. 다른 PC에서도 사용할 때는 별도 Private 저장소와 그 저장소에 한정한 GitHub 토큰을 연결합니다.

자세한 내용: [사용 가이드](docs/getting-started.md), [연결 범위와 제약](docs/limitations.md), [데이터 출처](docs/data-sources.md).

## 소스로 실행 / 개발

Windows용 Python 3.12와 tkinter를 사용합니다.

```powershell
python -m pip install -r requirements.txt
python main.py
python -m unittest discover -v
```

```powershell
# Windows에서 실행 파일 만들기
python -m pip install -r requirements-build.txt
python main.py --smoke-test --smoke-report artifacts/source-smoke.json --screenshot artifacts/window.png
python scripts/build_windows.py
```

GitHub Actions는 합성 자료로 단위 테스트, 여섯 화면 생성, EXE 빌드, 실제 EXE 실행·종료를 검사합니다. `main`의 성공한 빌드만 Release를 게시합니다. `--smoke-test`는 임시 폴더를 사용하며 기존 사용자 기록을 열지 않습니다.

코드를 다른 PC에서 clone해도 개인 기록은 포함되지 않습니다. 기본 기록 위치는 `%LOCALAPPDATA%\StockJournal`이며 개인 기록은 따로 동기화합니다. 인증키·계좌번호는 Windows 사용자별 DPAPI로 보호하고 원장과 분리합니다. 이미지·원본 엑셀·인증값은 Git에 올리지 않습니다.
