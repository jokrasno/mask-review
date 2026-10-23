"""Portable source launcher; installs dependencies only with explicit --setup."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', action='store_true', help='Prepare only the app-local .venv, then start')
    parser.add_argument('--check', action='store_true', help='Check interpreter and dependencies without starting or signing in')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8791)
    parser.add_argument('--data', type=Path)
    parser.add_argument('--private', type=Path)
    parser.add_argument('--interval', type=int, default=20)
    parser.add_argument('--fixture-service', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.exit(1, 'Python 3.10 or newer is required. Nothing was installed.\n')
    if sys.platform not in ('win32', 'darwin', 'linux'):
        parser.exit(1, 'This desktop build supports Windows, macOS and Linux.\n')
    if args.port == 8766:
        parser.error('Desktop mode uses its own port; production port 8766 is reserved')
    if args.setup:
        target = ROOT / '.venv'
        runtime = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        try:
            if target.exists() and not (target / 'pyvenv.cfg').is_file():
                parser.exit(1, 'The .venv folder is not a Python virtual environment. Preserve it and choose a fresh extracted app folder.\n')
            if not runtime.is_file():
                venv.EnvBuilder(with_pip=True).create(target)
            subprocess.run([str(runtime), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(ROOT / 'requirements-desktop.txt')], check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            parser.exit(1, 'App-local setup did not finish. Local reviews are unchanged. Check internet access and Python venv support, then retry --setup.\n')
        command = [str(runtime), str(Path(__file__).resolve())]
        for flag, value in (('--port', args.port), ('--interval', args.interval), ('--data', args.data), ('--private', args.private), ('--fixture-service', args.fixture_service)):
            if value is not None:
                command.extend([flag, str(value)])
        if args.no_browser:
            command.append('--no-browser')
        if args.check:
            command.append('--check')
        os.execv(str(runtime), command)
    missing = [name for name in ('PIL',) if importlib.util.find_spec(name) is None]
    secure_dependency = sys.platform == 'win32' or importlib.util.find_spec('keyring') is not None
    if args.check:
        print(json.dumps(dict(platform=sys.platform, python=list(sys.version_info[:3]), editor_dependencies_ready=not missing,
                              secure_store_dependency_ready=secure_dependency, native_store_access_verified=False,
                              installed_anything=False), indent=2))
        return 1 if missing or not secure_dependency else 0
    if missing:
        parser.exit(1, 'Pillow is missing. Run sh desktop.sh --setup (Mac/Linux), or follow the Windows setup in DESKTOP_SETUP.md. Nothing was installed.\n')
    if not secure_dependency:
        print('Google sign-in needs the keyring dependency. Offline editing is available. Run sh desktop.sh --setup to add it in this app only.', file=sys.stderr, flush=True)
    command = [sys.executable, str(ROOT / 'desktop_server.py'), '--port', str(args.port), '--interval', str(args.interval)]
    for flag, value in (('--data', args.data), ('--private', args.private), ('--fixture-service', args.fixture_service)):
        if value is not None:
            command.extend([flag, str(value.resolve()) if isinstance(value, Path) else value])
    if not args.no_browser:
        command.append('--open')
    os.execv(sys.executable, command)


if __name__ == '__main__':
    raise SystemExit(main())
