"""Owner-requested diagnostics, portable profiles and validated programme releases."""
import asyncio
import ast
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid
import zipfile

from pydantic import BaseModel, ConfigDict, Field

from characters import CharacterInput, LoreInput, parse_character
from features import Settings, strip_retired_limits, write_private_json
from providers import ProviderSpec


CODE_ROOT = Path(__file__).resolve().parent
INSTALL_ROOT = Path(os.environ.get('HEARTHLING_INSTALL_ROOT', CODE_ROOT))
RELEASE_FILES = {
    'hearthling.py', 'config.py', 'first_run.py', 'bots.py', 'features.py', 'conversation.py', 'memories.py', 'commands.py', 'social.py',
    'pending.py', 'characters.py', 'providers.py', 'images.py', 'expressions.py', 'people.py', 'wordle.py', 'releases.py', 'news.py',
    'maintenance.py', 'launcher.py', 'dashboard.py', 'dashboard.html', 'dashboard.css', 'dashboard.js', 'dashboard-extra.js',
    'avatar.png', 'personality.txt', 'system-prompt.md', 'appearances.py', 'web_sources.py', 'README.md', 'LICENSE', 'requirements.txt',
    'testenv.py', 'test_hearthling.py', 'test_features.py', 'test_bots.py', 'test_conversation.py', 'test_memories.py', 'test_social.py',
    'test_pending.py', 'test_parity.py', 'test_expressions.py', 'test_members.py', 'test_wordle.py', 'test_releases.py', 'test_news.py',
    'test_appearances.py', 'test_web_sources.py',
}
# The bundled launcher is used by release checks; systemd keeps using the installed launcher.
REQUIRED_FILES = RELEASE_FILES - {'README.md', 'LICENSE', 'first_run.py', 'avatar.png'}
MAX_RELEASE = 25 * 1024 * 1024


def reload_bot(bot):
    if bot.active_users:
        raise ValueError('Let the current request finish before reloading this bot.')
    row = bot.db.execute("SELECT value FROM settings WHERE name='configuration'").fetchone()
    if row:
        bot.features.settings = Settings.model_validate(strip_retired_limits(json.loads(row[0])))
    bot.system_template = (CODE_ROOT / 'system-prompt.md').read_text()
    bot.personality = (CODE_ROOT / 'personality.txt').read_text()
    bot.characters.apply()
    bot.features.apply_connection()
    bot.providers.rejected_vision.clear()
    bot.features.search_cache.clear()


def diagnostics(bot):
    stats = bot.db.execute('SELECT count(*), coalesce(sum(error IS NOT NULL),0), coalesce(sum(input_tokens),0), '
                           'coalesce(sum(output_tokens),0), coalesce(avg(seconds),0) FROM provider_requests').fetchone()
    rows = bot.db.execute('SELECT at,provider,model,seconds,input_tokens,output_tokens,error FROM provider_requests ORDER BY id DESC LIMIT 100').fetchall()
    context = bot.db.execute("SELECT value FROM settings WHERE name='last_context'").fetchone()
    log = bot.features.data_dir / 'logs' / 'requests.log'
    log_text = ''
    if log.is_file():
        with log.open('rb') as handle:
            handle.seek(max(0, log.stat().st_size - 65536))
            log_text = handle.read().decode(errors='replace')
    return {'statistics': dict(zip(('requests', 'errors', 'input_tokens', 'output_tokens', 'average_seconds'), stats)),
            'requests': [dict(zip(('at', 'provider', 'model', 'seconds', 'input_tokens', 'output_tokens', 'error'), row)) for row in rows],
            'database': bot.db.execute('PRAGMA quick_check').fetchone()[0],
            'commands': [command.name for command in bot.command_tree.get_commands(guild=__import__('discord').Object(bot.server_id))],
            'last_context': json.loads(bot.features.redact(context[0])) if context else None,
            'logs': bot.features.redact(log_text)}


class ProfileBundle(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    format: str = Field(pattern=r'^hearthling-profile-v1$')
    settings: Settings
    characters: list[CharacterInput] = Field(default_factory=list, max_length=100)
    providers: list[ProviderSpec] = Field(default_factory=list, max_length=20)
    lore: list[LoreInput] = Field(default_factory=list, max_length=500)
    memories: list[dict] = Field(default_factory=list, max_length=10000)
    timezones: dict[str, str] = Field(default_factory=dict, max_length=10000)
    reminders: list[dict] = Field(default_factory=list, max_length=10000)


def export_profile(bot):
    def rows(table, database=None):
        cursor = (database or bot.db).execute('SELECT * FROM ' + table)
        return [dict(zip([item[0] for item in cursor.description], row)) for row in cursor]
    return {'format': 'hearthling-profile-v1', 'settings': bot.features.settings.model_dump(),
            'characters': bot.characters.list(),
            'providers': [json.loads(row[0]) for row in bot.db.execute('SELECT configuration FROM providers')],
            'lore': [{key: value for key, value in row.items() if key != 'updated_at'} for row in bot.characters.lore()],
            'memories': rows('memories', bot.features.memories_db), 'timezones': dict(bot.db.execute('SELECT user_id,zone FROM timezones')),
            'reminders': rows('reminders')}


def import_profile(bot, data, *, apply=False):
    """Validate against a disposable database before touching any live configuration."""
    from hearthling import Hearthling, open_database
    from social import SocialOptions
    import tempfile
    if bot.active_users:
        raise ValueError('Let the current request finish before importing this bot’s profile.')
    bundle = ProfileBundle.model_validate(data)
    encoded = json.dumps(data)
    if bot.features.redact(encoded) != encoded:
        raise ValueError('Profile imports cannot contain saved credentials or private sign-in links.')
    with tempfile.TemporaryDirectory(dir=bot.features.data_dir) as temporary:
        db = open_database(':memory:')
        bot.db.backup(db)
        probe = Hearthling(None, db, credentials=bot.features.credentials, data_dir=Path(temporary),
                           bot_id=bot.bot_id, server_id=bot.server_id, character_name=bot.default_character_name)
        try:
            for spec in bundle.providers:
                if spec.id == 'current' or spec.id.startswith('current-fallback-'):
                    raise ValueError('The main connection and its fallback models are stored in settings, not named providers.')
                db.execute('INSERT OR REPLACE INTO providers VALUES (?,?)', (spec.id, spec.model_dump_json()))
            db.commit()
            for character in bundle.characters:
                probe.characters.save(character.model_dump())
            for lore in bundle.lore:
                if not db.execute('SELECT 1 FROM lore WHERE scope=? AND target=? AND channel_id=? AND text=?',
                                  (lore.scope, lore.target, lore.channel_id, lore.text.strip())).fetchone():
                    probe.characters.save_lore(lore.model_dump() | {'id': None})
            for fact in bundle.memories:
                author, channel, text = fact.get('author_id'), fact.get('channel_id'), fact.get('fact')
                keywords = fact.get('keywords', '')
                if (not isinstance(author, str) or not re.fullmatch(r'[0-9]{15,21}', author)
                        or not isinstance(channel, str) or (channel and not re.fullmatch(r'[0-9]{15,21}', channel))
                        or not isinstance(text, str) or not isinstance(keywords, str)):
                    raise ValueError('Every imported fact needs a Discord user ID, text and a channel ID, or a blank channel for a server-wide fact.')
                probe.features.remember(author, channel, text, keywords=keywords)
            for author, zone in bundle.timezones.items():
                if not re.fullmatch(r'[0-9]{15,21}', author):
                    raise ValueError('Every timezone needs a Discord user ID.')
                db.execute('INSERT OR REPLACE INTO timezones VALUES (?,?)', (author, SocialOptions.valid_timezone(zone)))
            for reminder in bundle.reminders:
                required = {'id', 'user_id', 'channel_id', 'guild_id', 'due_at', 'message', 'state', 'result', 'created_at'}
                if set(reminder) != required or not isinstance(reminder['message'], str) or not 1 <= len(reminder['message']) <= 1000:
                    raise ValueError('Invalid imported reminder fields.')
                if not isinstance(reminder['user_id'], str) or not re.fullmatch(r'[0-9]{15,21}', reminder['user_id']):
                    raise ValueError('Reminder user and channel IDs must be Discord IDs.')
                if not isinstance(reminder['channel_id'], str) or not (re.fullmatch(r'[0-9]{15,21}', reminder['channel_id']) or
                        (reminder['channel_id'] == 'dm' and not reminder['guild_id'] and reminder['user_id'] == str(bot.features.owner_id))):
                    raise ValueError('Use a Discord channel ID, or an owner-only DM reminder.')
                if reminder['guild_id'] not in {None, '', str(bot.server_id)} or (not reminder['guild_id'] and reminder['user_id'] != str(bot.features.owner_id)):
                    raise ValueError('A reminder belongs to another server or a non-owner DM.')
                if reminder['state'] not in {'pending', 'sending', 'sent', 'cancelled', 'uncertain'}:
                    raise ValueError('Invalid reminder state.')
                if not isinstance(reminder['due_at'], (int, float)) or not 0 < reminder['due_at'] < time.time() + 366 * 86400:
                    raise ValueError('Invalid reminder date.')
                state = 'uncertain' if reminder['state'] == 'sending' or (reminder['state'] == 'pending' and reminder['due_at'] < time.time()) else reminder['state']
                exists = db.execute('SELECT 1 FROM reminders WHERE user_id=? AND channel_id=? AND due_at=? AND message=?',
                                    (reminder['user_id'], reminder['channel_id'], reminder['due_at'], reminder['message'])).fetchone()
                if not exists:
                    db.execute('INSERT INTO reminders (user_id,channel_id,guild_id,due_at,message,state,result,created_at) VALUES (?,?,?,?,?,?,?,?)',
                               (reminder['user_id'], reminder['channel_id'], reminder['guild_id'], reminder['due_at'], reminder['message'], state,
                                json.dumps(reminder['result']) if not isinstance(reminder['result'], str) else reminder['result'], time.time()))
            db.commit()
            probe.features.update_settings(bundle.settings.model_dump(), {})
            result = {'ok': True, 'settings': len(bundle.settings.model_dump()), 'characters': len(bundle.characters),
                      'providers': len(bundle.providers), 'memories': len(bundle.memories), 'lore': len(bundle.lore), 'reminders': len(bundle.reminders)}
            if apply:
                backups = bot.features.data_dir / 'backups'
                backups.mkdir(mode=0o700, exist_ok=True)
                name = 'before-import-' + uuid.uuid4().hex + '.sqlite3'
                with sqlite3.connect(backups / name) as backup:
                    bot.db.backup(backup)
                db.backup(bot.db)
                for lore in bundle.lore:
                    if not bot.characters.lore_db.execute('SELECT 1 FROM lore WHERE scope=? AND target=? AND channel_id=? AND text=?',
                                                          (lore.scope, lore.target, lore.channel_id, lore.text.strip())).fetchone():
                        bot.characters.save_lore(lore.model_dump() | {'id': None})
                for fact in bundle.memories:
                    bot.features.remember(fact.get('author_id'), fact.get('channel_id'), fact.get('fact'), keywords=fact.get('keywords'))
                reload_bot(bot)
                result['backup'] = name
            return result
        finally:
            db.close()


def release_archive(root=CODE_ROOT):
    files = {name: (root / name).read_bytes() for name in sorted(RELEASE_FILES) if (root / name).is_file()}
    manifest = {'format': 'hearthling-release-v1', 'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, indent=2))
        for name, raw in files.items():
            archive.writestr(name, raw)
    return buffer.getvalue()


def inspect_release(raw):
    if not raw or len(raw) > MAX_RELEASE:
        raise ValueError('Choose a Hearthling release ZIP smaller than 25 MiB.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) - RELEASE_FILES - {'manifest.json'} or sum(item.file_size for item in infos) > MAX_RELEASE:
                raise ValueError('The release contains duplicate, unknown or oversized files.')
            if any(item.is_dir() or ((item.external_attr >> 16) & 0o170000) == 0o120000 for item in infos):
                raise ValueError('Release directories and symbolic links are not allowed.')
            manifest = json.loads(archive.read('manifest.json'))
            files = manifest['files']
            if manifest.get('format') != 'hearthling-release-v1' or not isinstance(files, dict) or set(files) != set(names) - {'manifest.json'} or not REQUIRED_FILES <= set(files):
                raise ValueError('The release manifest does not match its contents.')
            contents = {name: archive.read(name) for name in files}
            for name, content in contents.items():
                if hashlib.sha256(content).hexdigest() != files[name]:
                    raise ValueError('A release file does not match its checksum.')
                if name.endswith('.py'):
                    ast.parse(content, filename=name)
            requirements = contents['requirements.txt'].decode().splitlines()
            for line in requirements:
                if not line.strip() or line.startswith('#'):
                    continue
                match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)', line.strip())
                if not match or importlib.metadata.version(match[1]) != match[2]:
                    raise ValueError('Install the release’s exact dependencies before applying it. This updater does not run package installers.')
            return hashlib.sha256(raw).hexdigest()[:24], contents
    except (zipfile.BadZipFile, KeyError, UnicodeError, SyntaxError, importlib.metadata.PackageNotFoundError, TypeError) as exc:
        raise ValueError('This is not a valid, compatible Hearthling programme release.') from None


class Maintenance:
    def __init__(self, dashboard):
        self.dashboard = dashboard
        self.data = getattr(dashboard.features, 'data_dir', INSTALL_ROOT / 'data')
        self.directory = self.data / 'releases'
        self.lock = asyncio.Lock()

    def status(self):
        def read(name):
            path = self.directory / name
            return json.loads(path.read_text()) if path.is_file() else None
        return {'current': os.environ.get('HEARTHLING_RELEASE', 'installed'), 'active': read('active.json'),
                'staged': read('staged.json'), 'last_update': read('status.json'),
                'can_restart': bool(self.dashboard.manager and getattr(self.dashboard.manager, 'stop_event', None)),
                'can_update': os.environ.get('HEARTHLING_LAUNCHER') == '1'}

    async def stage(self, raw):
        async with self.lock:
            identity, contents = inspect_release(raw)
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination = self.directory / identity
            if not destination.exists():
                destination.mkdir(mode=0o700)
                try:
                    for name, content in contents.items():
                        with (destination / name).open('xb') as handle:
                            handle.write(content)
                            handle.flush()
                            os.fsync(handle.fileno())
                except BaseException:
                    shutil.rmtree(destination)
                    raise
            else:
                if any(not (destination / name).is_file() or (destination / name).read_bytes() != content for name, content in contents.items()):
                    raise ValueError('This staged release was modified; upload a new release instead.')
            checks = [name for name in sorted(contents) if name.startswith('test_') and name.endswith('.py')]
            for check in checks:
                process = await asyncio.create_subprocess_exec(
                    os.sys.executable, str(destination / check), cwd=destination,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env={key: value for key, value in os.environ.items() if not key.startswith('HEARTHLING_')})
                try:
                    output, _ = await asyncio.wait_for(process.communicate(), 120)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
                    raise ValueError(f'The {check} release check timed out; the running programme was not changed.') from None
                if process.returncode:
                    raise ValueError(f'The {check} release check failed; the running programme was not changed.')
            result = {'id': identity, 'files': len(contents), 'checks_passed': checks, 'checked_at': time.time()}
            write_private_json(self.directory / 'staged.json', result)
            return result

    def restart(self, action='restart'):
        manager = self.dashboard.manager
        if not manager or not getattr(manager, 'stop_event', None):
            raise ValueError('Programme restart is unavailable in this preview.')
        if self.lock.locked() or any(bot.active_users for bot in manager.bots.values()):
            raise ValueError('Let active requests and release checks finish before restarting.')
        status = self.status()
        if action in {'apply', 'rollback'}:
            if not status['can_update']:
                raise ValueError('Start this installation with launcher.py before applying or rolling back releases.')
            target = (status['staged'] or {}).get('id') if action == 'apply' else (status['active'] or {}).get('previous')
            if not target:
                raise ValueError('No checked release is staged.' if action == 'apply' else 'No previous release is available.')
            write_private_json(self.directory / 'pending.json', {'id': target})
        elif action != 'restart':
            raise ValueError('Unknown programme maintenance action.')
        for bot in manager.bots.values():
            bot.accepting_messages = False
        manager.exit_code = 75
        asyncio.get_running_loop().call_later(1, manager.stop_event.set)
        return {'ok': True, 'message': 'The programme is restarting. Saved data and sign-in sessions are kept.'}
