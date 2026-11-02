"""Install identity: the Discord application, server and owner this install serves.

Values come from environment variables first, then config.json in the install root.
Run first_run.py to write config.json.
"""
import base64
import binascii
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INSTALL_ROOT = Path(os.environ.get('HEARTHLING_INSTALL_ROOT', ROOT))
CONFIG_FILE = INSTALL_ROOT / 'config.json'
FIELDS = {'application_id': 'HEARTHLING_APPLICATION_ID', 'server_id': 'HEARTHLING_SERVER_ID',
          'owner_id': 'HEARTHLING_OWNER_ID'}


def application_from_token(token):
    """Discord bot tokens start with the base64-encoded application ID."""
    head = (token or '').split('.', 1)[0]
    try:
        value = base64.b64decode(head + '=' * (-len(head) % 4), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return 0
    return int(value) if value.isdigit() else 0


def load(path=CONFIG_FILE):
    try:
        saved = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        saved = {}
    values = {}
    for name, variable in FIELDS.items():
        raw = os.environ.get(variable) or saved.get(name) or 0
        try:
            values[name] = int(raw)
        except (TypeError, ValueError):
            values[name] = 0
    return values


def save(values, path=CONFIG_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({name: str(values[name]) for name in FIELDS}, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def missing(values):
    return [name for name in FIELDS if not values.get(name)]


IDENTITY = load()
APPLICATION_ID = IDENTITY['application_id']
SERVER_ID = IDENTITY['server_id']
OWNER_ID = IDENTITY['owner_id']
