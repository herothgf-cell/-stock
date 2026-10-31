# 저장소를 복제해서 바로 실행하기

검증한 Windows 64비트 실행 파일을 저장소 루트의 `StockJournal.exe`로 제공합니다. Git LFS 포인터가 아닌 실제 EXE이므로 일반 `git clone`만으로 받을 수 있습니다. 실행할 때 Python을 설치하거나 ZIP을 따로 내려받을 필요가 없습니다.

## 새 PC에서 실행

Git이 설치된 Windows의 PowerShell에서 다음과 같이 실행합니다.

```powershell
git clone --depth 1 https://github.com/herothgf-cell/-stock.git stock-journal
cd stock-journal
.\StockJournal.exe
```

탐색기에서 `stock-journal` 폴더의 `StockJournal.exe`를 더블클릭해도 됩니다. 개발 이력 전체가 필요하면 `--depth 1`을 빼고 복제합니다.

`main`에 소스 변경이 올라온 직후에는 Windows 빌드가 진행 중일 수 있습니다. [Windows build](https://github.com/herothgf-cell/-stock/actions/workflows/windows.yml)의 성공한 게시 단계와 `StockJournal-build.json`의 버전을 확인합니다. 첫 게시가 끝나기 전에 복제해서 EXE이 없다면 빌드가 끝난 뒤 아래 갱신 명령을 실행합니다. 이전 EXE이 있는 경우에는 새 빌드 검증이 끝날 때까지 이전 검증 버전이 유지됩니다.

## 기존 폴더 갱신

실행 중인 앱을 종료하고 해당 폴더에서 실행합니다. Windows에서 EXE을 열어 둔 상태로 갱신하면 파일 교체가 실패할 수 있습니다.

```powershell
git pull --ff-only
.\StockJournal.exe
```

개인 원장은 기본적으로 `%LOCALAPPDATA%\StockJournal`에 저장됩니다. 프로그램 업데이트와 개인 원장 동기화는 별개이며, 다른 PC의 개인 기록을 가져오려면 앱의 비공개 저장소 동기화를 설정해야 합니다. 사용법은 [시작 가이드](getting-started.md)를 참고하세요.

소스를 직접 수정한 개발자의 폴더에서 `git pull --ff-only`가 중단되면 로컬 변경을 먼저 보관하고 분기를 확인하세요. 이 명령은 사용자 변경을 강제로 덮어쓰지 않습니다.

## EXE의 출처와 무결성 확인

EXE 옆의 `StockJournal-build.json`에는 다음 정보가 있습니다.

| 항목 | 의미 |
| --- | --- |
| `version` | 앱 버전 |
| `source_commit` | 이 EXE을 빌드한 소스의 전체 Git 커밋 SHA |
| `built_at_utc` | EXE 생성 시각(UTC) |
| `platform` | 대상 플랫폼 `windows-x64` |
| `sha256`, `size_bytes` | 실행 파일의 SHA-256과 바이트 수 |
| `verification` | 소스/EXE GUI 검사 통과 여부와 검사 보고서 해시 |

PowerShell에서 받은 EXE을 확인할 수 있습니다.

```powershell
$build = Get-Content .\StockJournal-build.json -Raw | ConvertFrom-Json
$actual = (Get-FileHash .\StockJournal.exe -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $build.sha256) { throw 'EXE의 SHA-256이 빌드 기록과 다릅니다.' }
$build | Select-Object version, source_commit, built_at_utc, platform, size_bytes
```

EXE과 manifest를 추가하는 게시 커밋은 소스 커밋 다음에 만들어지므로 `git rev-parse HEAD`와 `source_commit`은 서로 다를 수 있습니다. SHA-256 검사는 파일과 manifest가 서로 맞는지 확인하는 절차입니다. 코드 서명 인증서를 대신하지는 않습니다. 이 배포의 EXE은 별도 코드 서명을 하지 않습니다.

## 자동 빌드와 게시 방식

1. 읽기 권한의 Windows 작업에서 전체 단위 테스트를 실행합니다.
2. 임시 합성 원장으로 소스 앱의 일곱 화면과 종료를 검사합니다.
3. PyInstaller로 EXE을 새로 만들고, **그 EXE을 실제로 실행**해서 같은 화면과 종료를 검사합니다.
4. 검증된 EXE의 해시와 출처를 기록하고, 공개 문서만 허용 목록으로 묶습니다.
5. 별도 게시 작업에서 manifest와 EXE의 해시를 다시 검사합니다. 원격 `main`이 빌드한 소스와 정확히 일치할 때만 `StockJournal.exe`와 `StockJournal-build.json` 두 파일을 커밋합니다.
6. 확인 직후 다른 변경이 올라오는 경우에도 일반 fast-forward push가 덮어쓰기를 막습니다. 오래된 빌드는 건너뛰며, 최신 소스의 빌드가 새 EXE을 게시합니다.

게시 작업만 `contents: write` 권한을 사용하며 인증에는 해당 실행의 `GITHUB_TOKEN`만 씁니다. Pull request 빌드에는 게시 권한이 없습니다. 자동 게시 커밋에는 `[skip ci]`가 붙고, EXE/manifest만 바뀐 push는 경로 필터로 제외합니다. GitHub 자체도 `GITHUB_TOKEN`으로 만든 일반 push가 새 워크플로 실행을 시작하지 않도록 처리합니다. [GitHub 공식 설명](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)

저장소나 조직의 정책이 Actions의 `main` 직접 쓰기를 제한하면 게시 작업은 실패하고 기존 파일은 그대로 남습니다. 이 경우 정책에 맞는 권한 또는 분기 보호 설정이 필요하며, 워크플로는 강제 push나 별도 개인 토큰으로 제한을 우회하지 않습니다.

## 저장소 크기

실제 EXE을 Git에 저장하므로 새 실행 파일을 게시할 때마다 바이너리 이력이 누적됩니다. 전체 clone은 이전 버전의 객체도 내려받아 시간이 지날수록 커질 수 있습니다. 실행만 필요한 새 PC에는 위의 `git clone --depth 1`이 현재 이력만 받는 방법입니다. 이미 복제한 폴더에서도 계속 업데이트하면 받은 바이너리 이력은 점차 쌓입니다.

GitHub의 일반 Git 파일 제한은 100 MiB이며, 빌드 도구는 그보다 작은 95 MiB 미만의 EXE만 게시하도록 검사합니다. 이 한도에 가까워지면 실행 파일 구성이나 배포 방식을 다시 결정해야 합니다. [GitHub 대용량 파일 설명](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)

## 직접 빌드

Windows 64비트 Python 3.12 환경에서 소스 변경을 커밋한 뒤 실행합니다.

```powershell
python -m pip install -r requirements-build.txt
python -m unittest discover -v
python scripts/build_windows.py
```

이 명령은 EXE 빌드, 소스와 실제 EXE의 임시 원장 검사, manifest 생성, 공개 파일 ZIP 생성을 순서대로 수행합니다. 결과는 `dist/`에 생기며 로컬 빌드 명령 자체가 GitHub로 push하지는 않습니다. 개인 DB, 인증키, 원본 엑셀·이미지, 백업 폴더는 배포 허용 목록에 포함하지 않습니다.
