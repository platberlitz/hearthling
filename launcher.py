"""Stable service entry point, with checked releases and startup rollback in the data directory."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
RELEASES = DATA / 'releases'


def read(name, default=None):
    path = RELEASES / name
    return json.loads(path.read_text()) if path.is_file() else default


def write(name, value):
    RELEASES.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = RELEASES / (name + '.' + uuid.uuid4().hex)
    with temporary.open('x') as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, RELEASES / name)


def release_path(identity):
    if identity == 'installed':
        return ROOT
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{24}', identity):
        raise ValueError('Invalid programme release ID.')
    path = RELEASES / identity
    if path.is_symlink() or not (path / 'hearthling.py').is_file():
        raise ValueError('The requested programme release is missing.')
    return path


def main():
    os.umask(0o077)
    child, stopping = None, False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        active = read('active.json', {'id': 'installed', 'previous': None})
        pending = read('pending.json')
        candidate = pending['id'] if pending else active['id']
        try:
            source = release_path(candidate)
        except ValueError:
            write('status.json', {'ok': False, 'error': 'The requested release is unavailable; the previous release was kept.', 'at': time.time()})
            (RELEASES / 'pending.json').unlink(missing_ok=True)
            candidate, source, pending = active['id'], release_path(active['id']), None
        launch_id = uuid.uuid4().hex
        child = subprocess.Popen([sys.executable, str(source / 'hearthling.py')], cwd=source,
                                 env=os.environ | {'HEARTHLING_INSTALL_ROOT': str(ROOT), 'HEARTHLING_DATA_DIR': str(DATA),
                                                   'HEARTHLING_RELEASE': candidate, 'HEARTHLING_LAUNCHER': '1', 'HEARTHLING_LAUNCH_ID': launch_id})
        if pending:
            healthy = False
            for _ in range(60):
                if stopping or child.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=1) as response:
                        healthy = json.load(response).get('launch_id') == launch_id
                except Exception:
                    pass
                if healthy:
                    break
                time.sleep(1)
            if stopping:
                return child.wait()
            (RELEASES / 'pending.json').unlink(missing_ok=True)
            if healthy:
                write('active.json', {'id': candidate, 'previous': active['id']})
                write('status.json', {'ok': True, 'id': candidate, 'at': time.time()})
            else:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill()
                child.wait()
                write('status.json', {'ok': False, 'error': 'The new release did not start. The previous release was restored.', 'at': time.time()})
                continue
        code = child.wait()
        if stopping or code != 75:
            return code
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
