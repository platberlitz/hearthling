"""Separate Discord accounts sharing one owner dashboard and provider limits."""
import asyncio
import json
import logging
import shutil
from typing import Literal

import discord
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hearthling import APPLICATION_ID, Hearthling, open_database
from features import write_private_json


class BotInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=50)
    server_id: str = Field(pattern=r'^[0-9]{15,21}$')
    enabled: bool = False
    token: str = Field(default='', max_length=4096)
    bot_mode: Literal['assistant', 'chatbot'] | None = None

    @field_validator('name')
    @classmethod
    def plain_name(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 or char in '<>@' for char in value):
            raise ValueError('Use a plain character name without mentions or line breaks.')
        return value

    @field_validator('token')
    @classmethod
    def private_token(cls, value):
        value = value.strip()
        if any(char.isspace() for char in value):
            raise ValueError('Paste only the bot token, without spaces or a prefix.')
        return value


async def validate_token(token):
    if not token:
        raise ValueError('A Discord bot token is required when adding an account.')
    try:
        async with discord.Client(intents=discord.Intents.none()) as client:
            async with asyncio.timeout(45):
                await client.login(token)
                application = await client.application_info()
                return str(application.id)
    except discord.LoginFailure:
        raise ValueError('Discord rejected that bot token. Copy a current token from its developer portal.') from None
    except discord.HTTPException as exc:
        raise ValueError(f'Discord could not verify the token (HTTP {exc.status}). No account was changed.') from None
    except (OSError, asyncio.TimeoutError):
        raise ValueError('Discord could not be reached to verify the token. No account was changed.') from None


class BotManager:
    reconnect_delay = 5

    def __init__(self, primary):
        self.db, self.data_dir = primary.db, primary.features.data_dir
        self.bots = {str(primary.bot_id): primary}
        self.tasks, self.errors = {}, {}
        self.bot_chains = {}
        self.lock = asyncio.Lock()
        self.ai_slots, self.image_lock = primary.ai_slots, primary.features.image_lock
        self.expression_lock = asyncio.Lock()
        self.member_lock = asyncio.Lock()
        self.memory_lock = asyncio.Lock()
        self.dashboard = None
        self.db.execute('CREATE TABLE IF NOT EXISTS bots (bot_id TEXT PRIMARY KEY, name TEXT NOT NULL, '
                        'server_id TEXT NOT NULL, enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)))')
        self.db.execute('INSERT OR IGNORE INTO bots VALUES (?, ?, ?, 1)',
                        (str(primary.bot_id), primary.character_name, str(primary.server_id)))
        self.db.commit()
        self.profiles = {row[0]: {'id': row[0], 'name': row[1], 'server_id': row[2], 'enabled': bool(row[3])}
                         for row in self.db.execute('SELECT bot_id, name, server_id, enabled FROM bots ORDER BY rowid')}
        profile = self.profiles[str(primary.bot_id)]
        primary.default_character_name = profile['name']
        primary.characters.apply()
        primary.server_id = primary.features.server_id = int(profile['server_id'])
        primary.accepting_messages = profile['enabled']
        for identity, profile in self.profiles.items():
            if identity == str(primary.bot_id):
                continue
            BotInput.model_validate({key: value for key, value in profile.items() if key != 'id'})
            if not identity.isdigit() or not 15 <= len(identity) <= 21:
                raise ValueError('A saved bot has an invalid application ID.')
            directory = self.data_dir / 'bots' / identity
            credentials = json.loads((directory / 'credentials.json').read_text())
            if (directory / 'connections.json').exists():
                credentials.update(json.loads((directory / 'connections.json').read_text()))
            self.bots[identity] = self.make_bot(profile, open_database(directory / 'hearthling.sqlite3'), credentials, directory)
        self.share_limits()

    def make_bot(self, profile, db, credentials, directory, ai=None):
        ai = ai or AsyncOpenAI(api_key=credentials.get('ai_api_key') or 'not-connected', timeout=90, max_retries=0)
        bot = Hearthling(ai, db, credentials=credentials, data_dir=directory, bot_id=int(profile['id']),
                         server_id=int(profile['server_id']), character_name=profile['name'])
        bot.dashboard = self.dashboard
        bot.accepting_messages = profile['enabled']
        return bot

    def share_limits(self):
        peers = [bot.features for bot in self.bots.values()]
        for bot in self.bots.values():
            bot.manager = self
            bot.social.queue.notice_db = self.db
            bot.ai_slots, bot.features.image_lock, bot.features.image_peers = self.ai_slots, self.image_lock, peers
            bot.features.memories_db, bot.features.memory_peers = self.db, peers
            bot.memory.db, bot.memory.lock = self.db, self.memory_lock
            bot.characters.memories_db, bot.characters.lore_db = self.db, self.db
            bot.expressions.upload_lock = self.expression_lock
            bot.people.lock = self.member_lock

    def summaries(self):
        result = []
        for identity, profile in self.profiles.items():
            bot = self.bots[identity]
            task = self.tasks.get(identity)
            mode = bot.features.settings.bot_mode
            permissions = 8 if mode == 'assistant' else discord.Permissions(view_channel=True, send_messages=True,
                            read_message_history=True, add_reactions=True, attach_files=True).value
            result.append({**profile, 'bot_mode': mode, 'ready': bot.is_ready(), 'running': bool(task and not task.done()),
                           'in_server': bool(bot.get_guild(bot.server_id)), 'error': self.errors.get(identity),
                           'active_requests': len(bot.active_users),
                           'account_name': bot.user.name if bot.user else None,
                             'invite_url': f'https://discord.com/oauth2/authorize?client_id={identity}&permissions={permissions}&scope=bot%20applications.commands'
                                         f'&guild_id={profile["server_id"]}&disable_guild_select=true',
                           'portal_url': f'https://discord.com/developers/applications/{identity}/bot'})
        return result

    def save_profile(self, identity, data):
        profile = {'id': identity, **data.model_dump(exclude={'token', 'bot_mode'})}
        self.db.execute('INSERT OR REPLACE INTO bots VALUES (?, ?, ?, ?)',
                        (identity, profile['name'], profile['server_id'], int(profile['enabled'])))
        self.db.commit()
        self.profiles[identity] = profile
        return profile

    async def add(self, data):
        data = BotInput.model_validate(data)
        async with self.lock:
            identity = await validate_token(data.token)
            if identity in self.profiles:
                raise ValueError('That Discord account is already in this dashboard. Edit its existing entry.')
            if not identity.isdigit() or not 15 <= len(identity) <= 21:
                raise ValueError('Discord returned an invalid application ID.')
            directory = self.data_dir / 'bots' / identity
            directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.exists():
                raise ValueError('Data already exists for that account. It has not been overwritten.')
            directory.mkdir(mode=0o700)
            bot, db = None, None
            try:
                primary = self.bots[str(APPLICATION_ID)]
                credentials = primary.features.credentials | {'discord_token': data.token}
                write_private_json(directory / 'credentials.json', {'discord_token': data.token})
                db = open_database(directory / 'hearthling.sqlite3')
                profile = {'id': identity, **data.model_dump(exclude={'token', 'bot_mode'})}
                bot = self.make_bot(profile, db, credentials, directory)
                for row in primary.db.execute('SELECT * FROM providers'):
                    db.execute('INSERT INTO providers VALUES (?,?)', row)
                for row in primary.db.execute('SELECT name, markdown, provider FROM characters'):
                    db.execute('INSERT OR REPLACE INTO characters VALUES (?,?,?)', row)
                db.commit()
                settings = primary.features.settings.model_dump() | {'personality': bot.personality, 'active_character': '',
                    'spontaneous_enabled': False, 'spontaneous_channels': [], 'dm_followups_enabled': False,
                    'releases_enabled': False, 'releases_channel': '',
                    'news_enabled': False, 'news_channel': '',
                    'bot_mode': data.bot_mode or 'assistant'}
                bot.features.update_settings(settings, {key: credentials[key] for key in
                    ('ai_api_key', 'image_api_key', 'image_fallback_keys', 'brave_api_key') if credentials.get(key)})
                write_private_json(directory / 'connections.json', bot.features.credentials)
                self.save_profile(identity, data)
            except Exception:
                if bot:
                    await bot.ai.close()
                if db:
                    db.close()
                shutil.rmtree(directory)
                raise
            self.bots[identity] = bot
            self.share_limits()
            if data.enabled:
                self.start(identity)
            return identity

    async def update(self, identity, data):
        data = BotInput.model_validate(data)
        async with self.lock:
            if identity not in self.bots:
                raise ValueError('That bot account does not exist.')
            if data.token and await validate_token(data.token) != identity:
                raise ValueError('That token belongs to a different Discord account. The existing token was kept.')
            bot = self.bots[identity]
            if bot.active_users:
                raise ValueError('This bot is handling a request. Let it finish before changing its account or stopping it.')
            await self.stop(identity)
            if data.bot_mode is not None:
                bot.features.update_settings({'bot_mode': data.bot_mode}, {})
            if data.token:
                credentials = bot.features.credentials | {'discord_token': data.token}
                write_private_json(bot.features.data_dir / 'connections.json', credentials)
                bot.features.credentials = credentials
            self.save_profile(identity, data)
            bot.default_character_name = data.name
            bot.characters.apply()
            bot.server_id = bot.features.server_id = int(data.server_id)
            self.errors.pop(identity, None)
            if data.enabled:
                self.start(identity)

    def start(self, identity):
        task = self.tasks.get(identity)
        if task and not task.done():
            return
        old = self.bots[identity]
        # A fresh Discord client avoids reusing a closed HTTP connection after a pause.
        bot = self.make_bot(self.profiles[identity], old.db, old.features.credentials, old.features.data_dir, old.ai)
        self.bots[identity] = bot
        self.share_limits()
        self.errors.pop(identity, None)
        self.tasks[identity] = asyncio.create_task(self.run_bot(identity, bot), name=f'discord-{identity}')

    async def run_bot(self, identity, bot):
        delay = self.reconnect_delay
        while True:
            try:
                async with bot:
                    await bot.start(bot.features.credentials['discord_token'])
                return
            except discord.LoginFailure:
                self.errors[identity] = 'Discord rejected the saved bot token. Replace it in Bots.'
                return
            except discord.PrivilegedIntentsRequired:
                self.errors[identity] = 'Enable Server Members Intent and Message Content Intent in this bot\'s developer portal, then reconnect.'
                return
            except Exception as exc:
                # discord.py gives up on a failed first gateway handshake, so transient outages must be retried here.
                logging.exception('Bot %s: Discord connection failed; retrying in %ss', identity, delay)
                self.errors[identity] = f'Discord connection stopped ({type(exc).__name__}). Retrying in {delay}s.'
            finally:
                bot.accepting_messages = False
                if identity in self.errors:
                    logging.warning('Bot %s: %s', identity, self.errors[identity])
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
            old = bot
            bot = self.make_bot(self.profiles[identity], old.db, old.features.credentials, old.features.data_dir, old.ai)
            self.bots[identity] = bot
            self.share_limits()
            self.errors.pop(identity, None)

    async def stop(self, identity):
        self.bots[identity].accepting_messages = False
        task = self.tasks.get(identity)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        await asyncio.gather(*(self.stop(identity) for identity in self.bots))
        for identity, bot in self.bots.items():
            if identity != str(APPLICATION_ID):
                await bot.ai.close()
                bot.db.close()
