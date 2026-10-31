"""Package a public-file allowlist; never include personal data directories."""
from pathlib import Path
import subprocess
import sys
import zipfile


def main():
    if sys.platform!='win32':
        raise SystemExit('Windows에서 빌드하세요. GitHub Actions도 Windows 빌드를 제공합니다.')
    root=Path(__file__).resolve().parents[1];resource=root/'stockjournal'/'resources'/'windows_ocr.ps1'
    if not resource.is_file():
        raise SystemExit('OCR 리소스가 없습니다.')
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed','--name','StockJournal','--distpath',str(root/'dist'),'--workpath',str(root/'build'/'pyinstaller'),'--specpath',str(root/'build'),'--exclude-module','PIL','--add-data',str(resource)+';stockjournal/resources',str(root/'main.py')],cwd=root,check=True)
    with zipfile.ZipFile(root/'dist'/'StockJournal-Windows.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for path,name in [(root/'dist'/'StockJournal.exe','StockJournal.exe'),(root/'README.md','README.md'),(root/'docs'/'getting-started.md','docs/getting-started.md'),(root/'docs'/'limitations.md','docs/limitations.md')]:
            archive.write(path,name)


if __name__=='__main__':
    main()
