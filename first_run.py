#!/usr/bin/env python3
"""First-run setup: save the Discord identity and private credentials without echoing secrets."""
import getpass
import json
import os
from pathlib import Path
import re
import tempfile

import config


def save(destination, credentials):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(credentials, output)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def ask(label, current='', pattern=None):
    suffix = f' [{current}]' if current else ''
    while True:
        value = input(f'{label}{suffix}: ').strip() or str(current or '')
        if value and (pattern is None or re.fullmatch(pattern, value)):
            return value
        print('That does not look right; please try again.')


def main():
    root = config.INSTALL_ROOT
    destination = root / 'credentials.json'
    overrides_path = root / 'data' / 'connections.json'
    credentials = json.loads(destination.read_text()) if destination.exists() else {}
    overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    credentials.update(overrides)
    for key, label in (('discord_token', 'Discord bot token'), ('ai_api_key', 'Conversation API key')):
        suffix = ' (Enter keeps the saved value)' if credentials.get(key) else ''
        value = getpass.getpass(f'{label}{suffix}: ').strip()
        if value:
            credentials[key] = value
        if not credentials.get(key):
            raise SystemExit(f'{label} is required; nothing was saved.')
    identity = config.load(config.CONFIG_FILE)
    application = config.application_from_token(credentials['discord_token']) or identity['application_id']
    snowflake = r'[0-9]{15,21}'
    identity = {
        'application_id': int(ask('Discord application ID', application or '', snowflake)),
        'server_id': int(ask('Discord server ID', identity['server_id'] or '', snowflake)),
        'owner_id': int(ask('Your Discord user ID (the owner)', identity['owner_id'] or '', snowflake)),
    }
    credentials['ai_base_url'] = ask('Conversation API base URL', credentials.get('ai_base_url', 'https://api.openai.com/v1'),
                                     r'https?://\S+')
    credentials['ai_model'] = ask('Conversation model', credentials.get('ai_model', 'gpt-5-mini'))
    save(destination, credentials)
    if overrides_path.exists():
        save(overrides_path, overrides | {key: credentials[key] for key in ('ai_api_key', 'discord_token')})
    config.save(identity, config.CONFIG_FILE)
    print('Setup saved privately. Start the bot with: python launcher.py')


if __name__ == '__main__':
    os.umask(0o077)
    main()
