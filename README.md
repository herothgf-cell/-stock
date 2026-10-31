# 주식 매매일지 · Stock Journal

Windows에서 사용하는 개인 매매일지입니다. 계좌별 거래·보유량·종목별 역대 손익·배당을 관리하고, 별도의 **비공개 GitHub 저장소**로 여러 PC의 기록을 동기화합니다.

## clone해서 실행

검증된 **`StockJournal.exe`를 저장소 루트에 직접 보관**합니다. ZIP이나 Python 설치 없이 clone한 폴더에서 실행합니다.

```powershell
git clone --depth 1 https://github.com/herothgf-cell/-stock.git stock-journal
cd stock-journal
.\StockJournal.exe
```

업데이트는 앱을 종료하고 `git pull --ff-only`한 뒤 다시 실행하세요. 실제 기록은 사용자 데이터 폴더에 보관되므로 코드 업데이트와 별개입니다.

`StockJournal-build.json`에서 실행 파일의 버전·소스 커밋·SHA-256을 확인할 수 있습니다. 새 소스의 Windows 검사가 성공해야 EXE가 갱신됩니다. [빌드 상태](https://github.com/herothgf-cell/-stock/actions/workflows/windows.yml), [clone 실행 안내](docs/clone-run.md), [이전 배포](https://github.com/herothgf-cell/-stock/releases).

## 기능

| 구분 | 내용 |
| --- | --- |
| 계좌 | 나무 계좌 목록에서 기존 별칭 연결·추가, 다른 증권사 수동 관리, 일반·ISA·연금저축·IRP·DC 구분 |
| 매매 | 매수·매도·기초 보유 입력, 수정·삭제, CSV 내보내기 |
| 보유·손익 | 계좌 필터, 이동평균 원가, 연결 물량 원가, 매도 완료 이력, 비용·실제 배당 반영 |
| 가져오기 | 기존 엑셀, CSV, 카톡 텍스트, Windows OCR 이미지 → 검토·수정 후 저장 |
| 배당 | 실제 수령액·세금과 참고/공시 일정 분리, CSV, 보유 종목 일정, ETFcheck 링크 |
| 종목·시세 | NH 공식 국내 종목 목록, 국내·미국 현재가 주기 조회, TQQQ 일봉, 수동 가격 입력 |
| 잔고·체결 | 국내·미국 잔고 대조, 이력 없는 종목의 기초 보유 검토, 국내·미국 누적 체결, 기존 엑셀과 중복 검토 |
| 200티큐 | 5월 7일 원문 규칙, 200일 SMA·3분할·단계 익절·SPYM/SGOV 전환 계획, 계좌별 진행 상태 |
| 자동 실행 | DRY_RUN 기본, 로컬 실행 기록, 명시적 LIVE 활성화, 체결 후 후속 주문, 불명·부분 체결 차단 및 대조 |
| 초기 입력 | 개인용 초기 자료 파일의 유효 기록과 수정 필요 기록 분리, 기존 기록에 중복 없이 합치기 |
| 보관 | SQLite, 변경 전 백업, JSON 복원, 비공개 저장소 동기화 |

통화별로 따로 계산합니다. 가격이 없는 보유 종목은 평가금액을 `미조회`로 표시합니다. 매매일지·전략 판단 화면은 조회와 기록 기능입니다. **자동 실행의 LIVE는 실제 주문을 제출하므로 기본 비활성**이며, 사용자가 현재 실행에서 계좌·예산·자료를 확인해 활성화해야 합니다.

## 처음 사용하는 순서

1. 개인 초기 자료가 있으면 **설정 · 계좌 → 초기 입력 / 복원**에서 `.stockjournal.json`을 선택합니다. 기존 엑셀/CSV도 가져올 수 있습니다.
2. **종목 관리 → 공식 종목 목록 받기** 또는 직접 입력으로 종목을 등록합니다.
3. 매매를 입력하거나 파일·텍스트·이미지 결과를 검토한 뒤 저장합니다.
4. NH 키를 앱 설정에 입력하고 **나무 계좌 가져오기**에서 기존 나무 별칭에 실제 계좌를 연결합니다. 다른 증권사 계좌는 수동 관리를 유지합니다.
5. 다른 PC에서도 사용할 때는 별도 Private 저장소와 그 저장소에 한정한 GitHub 토큰을 연결합니다.

6. **200티큐** 탭에서 종가 자료와 분할 보정, 전략 예산·수익률·기존 사이클을 확인합니다. **자동 실행 · 주문 상태**에서 먼저 DRY_RUN 결과를 확인합니다.

자세한 내용: [사용 가이드](docs/getting-started.md), [200티큐 규칙](docs/strategy-200tqqq.md), [자동 실행](docs/automation.md), [연결 범위와 제약](docs/limitations.md), [데이터 출처](docs/data-sources.md).

공개 저장소에는 개인 원본·초기 기록·계좌번호·인증키를 넣지 않습니다. 초기 기록은 별도로 앱에 입력해야 하며, clone만으로 개인 기록이 복제되지는 않습니다.

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

GitHub Actions는 합성 자료로 단위 테스트, 일곱 화면 생성, EXE 빌드, 실제 EXE 실행·종료를 검사합니다. `main`의 성공한 빌드만 같은 소스에 해당하는 EXE를 게시합니다. `--smoke-test`는 임시 폴더를 사용하며 기존 사용자 기록을 열거나 실제 주문을 보내지 않습니다.

코드를 다른 PC에서 clone해도 개인 기록은 포함되지 않습니다. 기본 기록 위치는 `%LOCALAPPDATA%\StockJournal`이며 개인 기록은 따로 동기화합니다. 인증키·계좌번호는 Windows 사용자별 DPAPI로 보호하고 원장과 분리합니다. 이미지·원본 엑셀·인증값은 Git에 올리지 않습니다.
