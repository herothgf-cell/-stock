"""Build and verify a Windows EXE, then package a public-file allowlist."""
import argparse
from pathlib import Path
import subprocess
import sys
import zipfile

from windows_delivery import EXE_NAME, MANIFEST_NAME, git, seal_delivery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--build-only', action='store_true', help='Build; CI verifies the result before packaging')
    mode.add_argument('--package-only', action='store_true', help='Package an EXE with existing successful smoke reports')
    parser.add_argument('--source-commit', help='Full commit SHA, default: this checkout HEAD')
    parser.add_argument('--source-report', type=Path, default=Path('artifacts/source-smoke.json'))
    parser.add_argument('--exe-report', type=Path, default=Path('artifacts/exe-smoke.json'))
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise SystemExit('Windows에서 빌드하세요. GitHub Actions도 Windows 빌드를 제공합니다.')
    root = Path(__file__).resolve().parents[1]
    resource = root / 'stockjournal' / 'resources' / 'windows_ocr.ps1'
    if not resource.is_file():
        raise SystemExit('OCR 리소스가 없습니다.')
    if not args.package_only:
        # Stale deliverables must not survive a failed rebuild or smoke test.
        for name in (EXE_NAME, MANIFEST_NAME, 'StockJournal-Windows.zip'):
            (root / 'dist' / name).unlink(missing_ok=True)
        subprocess.run([
            sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
            '--name', 'StockJournal', '--distpath', str(root / 'dist'),
            '--workpath', str(root / 'build' / 'pyinstaller'), '--specpath', str(root / 'build'),
            '--exclude-module', 'PIL', '--collect-data', 'tzdata',
            '--add-data', str(resource) + ';stockjournal/resources',
            str(root / 'main.py'),
        ], cwd=root, check=True)
    if args.build_only:
        return
    source_report = root / args.source_report
    exe_report = root / args.exe_report
    if not args.package_only:
        for report in (source_report, exe_report):
            report.unlink(missing_ok=True)
        subprocess.run([
            sys.executable, str(root / 'main.py'), '--smoke-test', '--smoke-report', str(source_report),
        ], cwd=root, check=True, timeout=90)
        subprocess.run([
            str(root / 'dist' / EXE_NAME), '--smoke-test', '--smoke-report', str(exe_report),
        ], cwd=root, check=True, timeout=90)
    source_commit = args.source_commit or git(root, 'rev-parse', 'HEAD').stdout.strip()
    manifest = seal_delivery(root, source_commit, source_report, exe_report)
    public_files = [
        (root / 'dist' / EXE_NAME, EXE_NAME),
        (root / 'dist' / MANIFEST_NAME, MANIFEST_NAME),
        (root / 'README.md', 'README.md'),
        *[(root / 'docs' / name, 'docs/' + name) for name in (
            'clone-run.md', 'getting-started.md', 'limitations.md', 'data-sources.md',
            'strategy-200tqqq.md', 'automation.md',
        )],
    ]
    with zipfile.ZipFile(root / 'dist' / 'StockJournal-Windows.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, name in public_files:
            archive.write(path, name)
    print(f"Verified v{manifest['version']} from {source_commit}; SHA-256 {manifest['sha256']}")


if __name__=='__main__':
    main()
