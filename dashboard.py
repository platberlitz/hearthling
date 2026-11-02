"""Owner-only settings served on localhost, behind the existing HTTPS proxy."""
import asyncio
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from types import SimpleNamespace
import uuid
from urllib.parse import urlsplit

from aiohttp import web
import discord
from openai import APIError
from pydantic import BaseModel, ConfigDict, Field

from features import FeatureError, Settings, host_status, write_private_json
from characters import CharacterInput, LoreInput, parse_character
from images import BUILTIN_IMAGE_MODELS, ImageSpec
from maintenance import Maintenance, diagnostics, export_profile, import_profile, release_archive, reload_bot, MAX_RELEASE


ROOT = Path(__file__).resolve().parent
PUBLIC_URL = os.environ.get('HEARTHLING_PUBLIC_URL', 'http://127.0.0.1:8787/')
COOKIE = 'hearthling_session'


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    author_id: str = Field(pattern=r'^[0-9]{15,21}$')
    channel_id: str = Field(pattern=r'^(|[0-9]{15,21})$')
    fact: str = Field(min_length=1, max_length=400)
    keywords: str = Field(default='', max_length=400)
    memory_id: int | None = Field(default=None, ge=1)


class ContextInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    author_id: str = Field(pattern=r'^[0-9]{15,21}$')
    channel_id: str = Field(pattern=r'^[0-9]{15,21}$')
    message: str = Field(default='Hello.', max_length=4000)


class ReminderInput(ContextInput):
    due_at: str = Field(min_length=10, max_length=100)
    delivery: str = Field(default='here', pattern=r'^(here|dm)$')


class TimezoneInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    author_id: str = Field(pattern=r'^[0-9]{15,21}$')
    zone: str = Field(default='', max_length=100)


class Dashboard:
    def __init__(self, bot, *, public_url=PUBLIC_URL, port=8787, manager=None):
        self.bot, self.features, self.db = bot, bot.features, bot.db
        self.manager = manager
        self.public_url, self.port = public_url, port
        address = urlsplit(public_url)
        self.origin = f'{address.scheme}://{address.netloc}'
        self.cookie_path, self.secure = address.path or '/', address.scheme == 'https'
        self.runner = None
        self.link_requested_at = 0.0
        self.maintenance = Maintenance(self)
        self.db.execute('CREATE TABLE IF NOT EXISTS web_sessions (token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, expires_at INTEGER NOT NULL)')
        self.db.commit()
        self.app = web.Application(middlewares=[self.guard], client_max_size=MAX_RELEASE)
        self.app.add_routes([
            web.get('/', self.index), web.get('/dashboard.css', self.asset), web.get('/dashboard.js', self.asset),
            web.get('/avatar.png', self.asset), web.get('/dashboard-extra.js', self.asset), web.get('/health', self.health),
            web.post('/api/login', self.login), web.post('/api/logout', self.logout),
            web.post('/api/request-link', self.request_link),
            web.get('/api/state', self.state), web.get('/api/status', self.status),
            web.get('/api/expressions', self.expressions),
            web.post('/api/settings', self.settings), web.get('/api/memories', self.memories),
            web.post('/api/memories', self.save_memory), web.delete('/api/memories/{identity}', self.delete_memory),
            web.post('/api/check/{kind}', self.check), web.get('/api/pictures/{identity}', self.picture),
            web.post('/api/bots', self.add_bot), web.post('/api/bots/{identity}', self.update_bot),
            web.get('/api/characters', self.characters), web.post('/api/characters', self.save_character),
            web.post('/api/characters/activate', self.activate_character), web.post('/api/characters/preview', self.preview_character),
            web.delete('/api/characters/{name}', self.delete_character),
            web.get('/api/lore', self.lore), web.post('/api/lore', self.save_lore), web.delete('/api/lore/{identity}', self.delete_lore),
            web.get('/api/reminders', self.reminders), web.post('/api/reminders', self.save_reminder),
            web.delete('/api/reminders/{identity}', self.cancel_reminder),
            web.post('/api/timezones', self.save_timezone),
            web.post('/api/memories/clean', self.clean_memories), web.post('/api/history/clear', self.clear_history),
            web.post('/api/context', self.context), web.post('/api/social/stop', self.stop_interactions),
            web.get('/api/providers', self.providers), web.post('/api/providers', self.save_provider),
            web.delete('/api/providers/{identity}', self.delete_provider), web.post('/api/providers/{identity}/check', self.check_provider),
            web.post('/api/image-providers', self.save_image_provider), web.delete('/api/image-providers/{identity}', self.delete_image_provider),
            web.get('/api/diagnostics', self.diagnostics), web.post('/api/commands/sync', self.sync_commands),
            web.get('/api/export', self.export), web.post('/api/import', self.import_data),
            web.get('/api/maintenance', self.maintenance_status), web.post('/api/maintenance/{action}', self.maintain),
            web.get('/api/release', self.export_release), web.post('/api/release', self.stage_release),
        ])

    @web.middleware
    async def guard(self, request, handler):
        try:
            if request.method not in {'GET', 'HEAD'}:
                if request.path != '/api/release':
                    request = request.clone(client_max_size=2 * 1024 * 1024)
                if request.headers.get('Origin') != self.origin:
                    raise web.HTTPForbidden(reason='Open this dashboard from its own address before making changes.')
                if request.path == '/api/release' and request.content_type == 'application/zip':
                    pass
                elif request.content_type != 'application/json':
                    raise web.HTTPUnsupportedMediaType(reason='Send changes as JSON.')
                if request.path != '/api/release' and request.content_length and request.content_length > 2 * 1024 * 1024:
                    raise web.HTTPRequestEntityTooLarge(max_size=2 * 1024 * 1024, actual_size=request.content_length)
            public = {'/', '/dashboard.css', '/dashboard.js', '/dashboard-extra.js', '/avatar.png', '/health',
                      '/api/login', '/api/request-link'}
            if request.path not in public:
                digest = self.digest(request.cookies.get(COOKIE, ''))
                session = self.db.execute("SELECT 1 FROM web_sessions WHERE token_hash=? AND kind='session' AND expires_at>?",
                                          (digest, int(time.time()))).fetchone()
                if not session:
                    raise web.HTTPUnauthorized(reason='No dashboard session in this browser yet. Send yourself a link below, or ask a running bot in Discord.')
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({'error': exc.reason}, status=exc.status)
        except (ValueError, PermissionError, sqlite3.IntegrityError) as exc:
            self.db.rollback()
            bot = request.get('selected_bot', self.bot)
            bot.db.rollback()
            message = 'That memory already exists in this conversation.' if isinstance(exc, sqlite3.IntegrityError) else bot.features.clean_error(exc)
            response = web.json_response({'error': message}, status=400)
        except Exception as exc:
            logging.error('Dashboard request failed: %s', type(exc).__name__, exc_info=True)
            response = web.json_response({'error': 'The request could not be completed. Your unsaved edits are still here.'}, status=500)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https://cdn.discordapp.com https://media.discordapp.net; "
                                     "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })
        return response

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def issue_link(self, owner_id):
        if owner_id != self.features.owner_id:
            raise PermissionError('Only the configured owner can sign in.')
        now = int(time.time())
        self.db.execute("DELETE FROM web_sessions WHERE expires_at<=? OR kind='link'", (now,))
        token = secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO web_sessions VALUES (?, ?, ?)', (self.digest(token), 'link', now + 600))
        self.db.commit()
        return self.public_url + '#login=' + token

    async def send_link(self, author):
        link = self.issue_link(author.id)
        try:
            dm = await author.create_dm()
            await dm.send('Your dashboard sign-in link. It works once and expires in ten minutes; keep it private.\n' + link,
                          allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
        except discord.HTTPException:
            self.db.execute("DELETE FROM web_sessions WHERE kind='link'")
            self.db.commit()
            raise ValueError('Allow direct messages from this server, then ask for the dashboard again.') from None

    def connected_bot(self):
        bots = list(self.manager.bots.values()) if self.manager else [self.bot]
        primary = str(self.bot.bot_id)
        ready = [bot for bot in bots if bot.is_ready()]
        return next((bot for bot in ready if str(bot.bot_id) == primary), ready[0] if ready else None)

    async def owner_user(self):
        bot = self.connected_bot()
        if bot is None:
            return None
        user = bot.get_user(self.features.owner_id)
        if user is None:
            try:
                user = await bot.fetch_user(self.features.owner_id)
            except discord.HTTPException:
                return None
        return user

    async def request_link(self, request):
        if time.monotonic() - self.link_requested_at < 60:
            raise web.HTTPTooManyRequests(reason='I sent a link less than a minute ago. Check your Discord DMs.')
        user = await self.owner_user()
        if user is None:
            raise ValueError('I could not reach your Discord account just now. Ask me for the dashboard in the server instead.')
        await self.send_link(user)
        self.link_requested_at = time.monotonic()
        return web.json_response({'ok': True})

    async def login(self, request):
        data = await request.json()
        token = data.get('token') if isinstance(data, dict) else None
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            raise web.HTTPUnauthorized(reason='This sign-in link is invalid. Send yourself a new one below.')
        now = int(time.time())
        cursor = self.db.execute("DELETE FROM web_sessions WHERE token_hash=? AND kind='link' AND expires_at>?",
                                 (self.digest(token), now))
        if not cursor.rowcount:
            self.db.commit()
            raise web.HTTPUnauthorized(reason='This link was already used or has expired. Send yourself a new one below.')
        session = secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO web_sessions VALUES (?, ?, ?)', (self.digest(session), 'session', now + 7 * 86400))
        self.db.commit()
        response = web.json_response({'ok': True})
        response.set_cookie(COOKIE, session, max_age=7 * 86400, path=self.cookie_path,
                            httponly=True, secure=self.secure, samesite='Strict')
        return response

    async def logout(self, request):
        self.db.execute('DELETE FROM web_sessions WHERE token_hash=?', (self.digest(request.cookies.get(COOKIE, '')),))
        self.db.commit()
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE, path=self.cookie_path, secure=self.secure, httponly=True, samesite='Strict')
        return response

    async def index(self, request):
        return web.FileResponse(ROOT / 'dashboard.html')

    async def asset(self, request):
        filename = request.path[1:]
        return web.FileResponse(ROOT / filename)

    async def health(self, request):
        bots = self.manager.bots.values() if self.manager else [self.bot]
        return web.json_response({'ready': any(bot.is_ready() for bot in bots), 'launch_id': os.environ.get('HEARTHLING_LAUNCH_ID')})

    def bot_for(self, request):
        identity = request.headers.get('X-Bot-ID') or request.query.get('bot') or str(self.bot.bot_id)
        if request.query.get('bot') and request.query['bot'] != identity:
            raise web.HTTPBadRequest(reason='The requested bot selections do not match.')
        bots = self.manager.bots if self.manager else {str(self.bot.bot_id): self.bot}
        if identity not in bots:
            raise web.HTTPNotFound(reason='That bot account does not exist. Choose another account.')
        request['selected_bot'] = bots[identity]
        return bots[identity]

    def bot_summaries(self):
        if self.manager:
            return self.manager.summaries()
        return [{'id': str(self.bot.bot_id), 'name': self.bot.character_name, 'server_id': str(self.bot.server_id),
                 'bot_mode': self.bot.features.settings.bot_mode,
                 'enabled': True, 'ready': self.bot.is_ready(), 'running': True,
                 'in_server': bool(self.bot.get_guild(self.bot.server_id)), 'error': None,
                 'active_requests': len(self.bot.active_users)}]

    def people_and_channels(self, bot):
        guild = bot.get_guild(bot.server_id)
        people = {str(bot.features.owner_id): 'Owner'}
        channels = {}
        rows = set(bot.db.execute('SELECT DISTINCT author_id, channel_id FROM turns'))
        rows.update(bot.features.memories_db.execute('SELECT DISTINCT author_id, channel_id FROM memories'))
        for author, channel in rows:
            people.setdefault(author, author)
            if channel:
                channels.setdefault(channel, 'Conversation ' + channel)
        if guild:
            for member in guild.members:
                if not member.bot:
                    people[str(member.id)] = member.display_name
            for channel in [*guild.channels, *guild.threads]:
                channels[str(channel.id)] = '#' + channel.name
        owner_dm = bot.get_user(bot.features.owner_id)
        if owner_dm and owner_dm.dm_channel:
            channels[str(owner_dm.dm_channel.id)] = 'DM with owner'
        return ([{'id': identity, 'name': name} for identity, name in sorted(people.items(), key=lambda row: row[1].casefold())],
                [{'id': identity, 'name': name, 'server_text': bool(guild and isinstance(guild.get_channel_or_thread(int(identity)), (discord.TextChannel, discord.Thread)))}
                 for identity, name in sorted(channels.items(), key=lambda row: row[1].casefold())])

    async def state(self, request):
        bot = self.bot_for(request)
        people, channels = self.people_and_channels(bot)
        summaries = self.bot_summaries()
        return web.json_response({
            'settings': bot.features.settings.model_dump(), 'system_prompt': bot.system_template,
            'connections': {'chat': bool(bot.features.credentials.get('ai_api_key')),
                             'image': bool(bot.features.credentials.get('image_api_key')),
                             'image_keys': len({key for key in [bot.features.credentials.get('image_api_key'), *bot.features.credentials.get('image_fallback_keys', [])] if key}),
                            'brave': bool(bot.features.credentials.get('brave_api_key'))},
            'owner_id': str(bot.features.owner_id), 'people': people, 'channels': channels,
            'bot': next(profile for profile in summaries if profile['id'] == str(bot.bot_id)),
            'bots': summaries, 'manage_bots': self.manager is not None,
            'characters': bot.characters.list(), 'providers': bot.providers.list(),
            'image_connections': {item.id: bool(bot.features.credentials.get('image_key:' + item.id)) for item in bot.features.settings.image_providers},
            'bot_interactions_stopped': bot.social.interactions_stopped(), 'effective_name': bot.character_name,
            'expressions': bot.expressions.catalog(limit=80),
        })

    async def expressions(self, request):
        return web.json_response(self.bot_for(request).expressions.catalog(query=request.query.get('q', '')[:100], limit=200))

    async def settings(self, request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) - {'settings', 'keys'}:
            raise ValueError('Unknown settings request.')
        self.bot_for(request).features.update_settings(data.get('settings', {}), data.get('keys', {}))
        return await self.state(request)

    async def status(self, request):
        bot = self.bot_for(request)
        checks = {}
        for name, encoded in bot.db.execute("SELECT name, value FROM settings WHERE name LIKE 'status:%'"):
            result = json.loads(encoded)
            checks[name[7:]] = {key: result[key] for key in ('ok', 'error', 'checked_at', 'model') if key in result}
        activity = []
        for identity, arguments, encoded, author in bot.db.execute(
                'SELECT a.request_id, a.arguments, a.result, t.author_id FROM actions a '
                'LEFT JOIN turns t ON t.request_id=a.request_id ORDER BY a.rowid DESC LIMIT 15'):
            result = json.loads(encoded) if encoded else {}
            picture = bot.db.execute("SELECT filename FROM pictures WHERE request_id=? AND state='complete'", (identity,)).fetchone()
            downloadable = bool(picture and (bot.features.data_dir / 'pictures' / Path(picture[0]).name).is_file())
            activity.append({'request_id': identity, 'author_id': author, 'action': json.loads(arguments)['action'],
                             'ok': result.get('ok'), 'uncertain': result.get('outcome_may_be_uncertain', not bool(encoded)),
                             'error': result.get('error'), 'picture_available': downloadable,
                             'time': discord.utils.snowflake_time(int(identity)).isoformat() if identity.isdigit() else None})
        latency = bot.latency
        return web.json_response({'ready': bot.is_ready(), 'latency_ms': round(latency * 1000) if math.isfinite(latency) else None,
                                  'active_requests': len(bot.active_users), 'checks': checks, 'activity': activity,
                                  'memory_count': bot.features.memories_db.execute('SELECT count(*) FROM memories').fetchone()[0],
                                  'bot_id': str(bot.bot_id), 'bots': self.bot_summaries()})

    async def memories(self, request):
        bot = self.bot_for(request)
        query = request.query.get('q', '')[:100].casefold()
        author, channel = request.query.get('author', ''), request.query.get('channel', '')
        try:
            offset = max(0, int(request.query.get('offset', 0)))
            limit = max(1, min(int(request.query.get('limit', 50)), 200))
        except ValueError:
            raise web.HTTPBadRequest(reason='offset and limit must be whole numbers.')
        rows = bot.features.memories_db.execute('SELECT id, author_id, channel_id, fact, keywords, updated_at FROM memories ORDER BY updated_at DESC, id DESC').fetchall()
        result = [{'id': row[0], 'author_id': row[1], 'channel_id': row[2], 'fact': row[3], 'keywords': row[4], 'updated_at': row[5]}
                  for row in rows if (not author or row[1] == author) and (not channel or row[2] == channel)
                  and query in row[3].casefold()]
        return web.json_response({'memories': result[offset:offset + limit], 'total': len(result)})

    async def save_memory(self, request):
        bot = self.bot_for(request)
        data = MemoryInput.model_validate(await request.json())
        people, channels = self.people_and_channels(bot)
        if data.author_id not in {person['id'] for person in people} or (data.channel_id and data.channel_id not in {channel['id'] for channel in channels}):
            raise ValueError('Choose a known person and conversation. Message this bot there first if it is missing.')
        return web.json_response(bot.features.remember(data.author_id, data.channel_id, data.fact, data.memory_id, keywords=data.keywords))

    async def delete_memory(self, request):
        bot = self.bot_for(request)
        identity = request.match_info['identity']
        if not identity.isdigit():
            raise web.HTTPNotFound(reason='That memory no longer exists.')
        memory = bot.features.memories_db.execute('SELECT author_id, channel_id FROM memories WHERE id=?', (identity,)).fetchone()
        if not memory:
            raise web.HTTPNotFound(reason='That memory no longer exists.')
        return web.json_response(bot.features.forget(*memory, int(identity)))

    async def check(self, request):
        bot = self.bot_for(request)
        features = bot.features
        kind = request.match_info['kind']
        try:
            if kind != 'chat':
                features.require_assistant()
            if kind == 'chat':
                answer = await bot.providers.reply(messages=[{'role': 'user', 'content': 'Reply with OK.'}], max_tokens=512)
                if not answer.choices[0].message.content:
                    raise FeatureError('The provider answered without any text. Check the selected model.')
                result = {'ok': True, 'result': {'model': getattr(answer, 'model', features.settings.ai_model)}}
            elif kind == 'web_search' and features.settings.search_enabled:
                result = {'ok': True, 'result': await features.web_search('Discord developer documentation', 3)}
            elif kind == 'host_status' and features.settings.host_status_enabled:
                result = {'ok': True, 'result': await host_status(features.settings.host_services)}
            else:
                raise ValueError('This check is unknown or disabled in Tools.')
        except (FeatureError, APIError, OSError, asyncio.TimeoutError) as exc:
            result = {'ok': False, 'error': features.clean_error(exc)}
        features.record_status(kind, result)
        return web.json_response(result)

    async def picture(self, request):
        bot = self.bot_for(request)
        identity = request.match_info['identity']
        if not re.fullmatch(r'[A-Za-z0-9-]{1,80}', identity):
            raise web.HTTPNotFound()
        row = bot.db.execute("SELECT filename FROM pictures WHERE request_id=? AND state='complete'", (identity,)).fetchone()
        if not row:
            raise web.HTTPNotFound(reason='That picture is not available.')
        path = bot.features.data_dir / 'pictures' / Path(row[0]).name
        return web.FileResponse(path, headers={'Content-Disposition': f'attachment; filename="{path.name}"'})

    async def add_bot(self, request):
        if self.manager is None:
            raise ValueError('Account management is unavailable in this preview.')
        identity = await self.manager.add(await request.json())
        return web.json_response({'id': identity, 'bots': self.manager.summaries()})

    async def update_bot(self, request):
        if self.manager is None:
            raise ValueError('Account management is unavailable in this preview.')
        await self.manager.update(request.match_info['identity'], await request.json())
        return web.json_response({'bots': self.manager.summaries()})

    async def characters(self, request):
        bot = self.bot_for(request)
        return web.json_response({'characters': bot.characters.list(), 'active': bot.features.settings.active_character})

    async def save_character(self, request):
        return web.json_response(self.bot_for(request).characters.save(await request.json()))

    async def preview_character(self, request):
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get('username', ''), str):
            raise ValueError('Choose character Markdown and a username for the preview.')
        value = CharacterInput.model_validate({'name': 'Preview', 'markdown': data.get('markdown')})
        return web.json_response(parse_character(value.markdown, data.get('username', '')[:100]))

    async def activate_character(self, request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'name'} or not isinstance(data['name'], str):
            raise ValueError('Choose a saved character name, or an empty name for the default.')
        self.bot_for(request).characters.activate(data['name'])
        return await self.state(request)

    async def delete_character(self, request):
        self.bot_for(request).characters.delete(request.match_info['name'])
        return web.json_response({'ok': True})

    async def lore(self, request):
        return web.json_response({'lore': self.bot_for(request).characters.lore()})

    async def save_lore(self, request):
        return web.json_response(self.bot_for(request).characters.save_lore(await request.json()))

    async def delete_lore(self, request):
        bot = self.bot_for(request)
        cursor = bot.characters.lore_db.execute('DELETE FROM lore WHERE id=?', (request.match_info['identity'],))
        bot.characters.lore_db.commit()
        return web.json_response({'deleted': cursor.rowcount})

    async def context_request(self, bot, value):
        channel = bot.get_channel(int(value.channel_id)) or await bot.fetch_channel(int(value.channel_id))
        guild = getattr(channel, 'guild', None)
        if guild:
            if guild.id != bot.server_id:
                raise ValueError('Choose a channel in this bot’s configured server.')
            author = guild.get_member(int(value.author_id)) or await guild.fetch_member(int(value.author_id))
            permission = channel.permissions_for(author)
            if not permission.view_channel or not permission.read_message_history:
                raise ValueError('This person cannot read that conversation.')
        else:
            if int(value.author_id) != bot.features.owner_id or getattr(getattr(channel, 'recipient', None), 'id', None) != bot.features.owner_id:
                raise ValueError('Direct-message context is limited to this bot’s owner.')
            author = bot.get_user(bot.features.owner_id) or await bot.fetch_user(bot.features.owner_id)
        return SimpleNamespace(id='dashboard-' + uuid.uuid4().hex, author=author, channel=channel, guild=guild,
                               content=value.message, created_at=discord.utils.utcnow(), mentions=[], reference=None, attachments=[], embeds=[])

    async def context(self, request):
        bot = self.bot_for(request)
        message = await self.context_request(bot, ContextInput.model_validate(await request.json()))
        return web.json_response(await bot.converse(message, preview=True))

    async def reminders(self, request):
        bot = self.bot_for(request)
        return web.json_response({'reminders': bot.social.reminder_list(), 'timezones': dict(bot.db.execute('SELECT user_id,zone FROM timezones'))})

    async def save_reminder(self, request):
        bot = self.bot_for(request)
        value = ReminderInput.model_validate(await request.json())
        message = await self.context_request(bot, value)
        return web.json_response(bot.social.add_reminder(message, value.due_at, value.message, dm=value.delivery == 'dm'))

    async def cancel_reminder(self, request):
        bot = self.bot_for(request)
        if not request.match_info['identity'].isdigit():
            raise ValueError('Choose a valid reminder ID.')
        return web.json_response(bot.social.cancel_reminder(bot.features.owner_id, int(request.match_info['identity']), owner=True))

    async def save_timezone(self, request):
        bot = self.bot_for(request)
        value = TimezoneInput.model_validate(await request.json())
        bot.social.set_zone(value.author_id, value.zone.strip() or None)
        return await self.reminders(request)

    async def clean_memories(self, request):
        value = ContextInput.model_validate(await request.json())
        return web.json_response(self.bot_for(request).characters.clean_memories(value.author_id, value.channel_id))

    async def clear_history(self, request):
        value = ContextInput.model_validate(await request.json())
        return web.json_response(self.bot_for(request).characters.clear_history(value.author_id, value.channel_id))

    async def stop_interactions(self, request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'stopped'} or not isinstance(data['stopped'], bool):
            raise ValueError('Choose whether conversations between bots are stopped.')
        self.bot_for(request).social.stop_interactions(data['stopped'])
        return web.json_response({'ok': True, 'stopped': data['stopped']})

    async def providers(self, request):
        return web.json_response({'providers': self.bot_for(request).providers.list()})

    async def save_provider(self, request):
        self.bot_for(request).providers.save(await request.json())
        return await self.providers(request)

    async def delete_provider(self, request):
        self.bot_for(request).providers.delete(request.match_info['identity'])
        return web.json_response({'ok': True})

    async def check_provider(self, request):
        bot = self.bot_for(request)
        spec = bot.providers.get(request.match_info['identity'])
        started = time.monotonic()
        try:
            async with bot.ai_slots:
                result = await bot.providers.send(spec, [{'role': 'user', 'content': 'Reply with OK.'}], {'max_tokens': 512})
            if not result.choices or not result.choices[0].message.content:
                raise ValueError('This provider returned no dialogue.')
            bot.providers.record(spec, time.monotonic() - started, response=result)
            return web.json_response({'ok': True, 'model': spec.model, 'provider': spec.id})
        except (APIError, ValueError) as exc:
            bot.providers.record(spec, time.monotonic() - started, error=exc)
            return web.json_response({'ok': False, 'error': bot.features.clean_error(exc)})

    async def save_image_provider(self, request):
        bot = self.bot_for(request)
        data = await request.json()
        if not isinstance(data, dict) or set(data) - {'profile', 'api_key'} or not isinstance(data.get('api_key', ''), str):
            raise ValueError('Choose an image profile and an optional replacement key.')
        spec, key = ImageSpec.model_validate(data.get('profile')), data.get('api_key', '')
        if spec.id in BUILTIN_IMAGE_MODELS or len(key) > 4096 or any(char.isspace() for char in key):
            raise ValueError('Use a unique image profile ID and a single private key.')
        profiles = [item.model_dump() for item in bot.features.settings.image_providers if item.id != spec.id] + [spec.model_dump()]
        Settings.model_validate(bot.features.settings.model_dump() | {'image_providers': profiles})
        if key:
            credentials = bot.features.credentials | {'image_key:' + spec.id: key}
            write_private_json(bot.features.data_dir / 'connections.json', credentials)
            bot.features.credentials = credentials
        bot.features.update_settings({'image_providers': profiles}, {})
        return await self.state(request)

    async def delete_image_provider(self, request):
        bot, identity = self.bot_for(request), request.match_info['identity']
        config = bot.features.settings
        if identity in {config.image_provider, config.dm_followup_image_provider, *BUILTIN_IMAGE_MODELS}:
            raise ValueError('Choose another image provider before deleting this one.')
        bot.features.update_settings({'image_providers': [item.model_dump() for item in config.image_providers if item.id != identity]}, {})
        bot.features.credentials.pop('image_key:' + identity, None)
        write_private_json(bot.features.data_dir / 'connections.json', bot.features.credentials)
        return web.json_response({'ok': True})

    async def diagnostics(self, request):
        return web.json_response(diagnostics(self.bot_for(request)))

    async def sync_commands(self, request):
        from commands import sync_commands
        bot = self.bot_for(request)
        bot.commands_synced = False
        await sync_commands(bot)
        row = bot.db.execute("SELECT value FROM settings WHERE name='status:commands'").fetchone()
        return web.json_response(json.loads(row[0]))

    async def export(self, request):
        bot = self.bot_for(request)
        return web.Response(text=bot.features.redact(json.dumps(export_profile(bot), ensure_ascii=False, indent=2)),
                            content_type='application/json', headers={'Content-Disposition': f'attachment; filename="bot-{bot.bot_id}-profile.json"'})

    async def import_data(self, request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'profile', 'apply'} or not isinstance(data['apply'], bool):
            raise ValueError('Choose a profile and whether to check or apply it.')
        return web.json_response(import_profile(self.bot_for(request), data['profile'], apply=data['apply']))

    async def maintenance_status(self, request):
        return web.json_response(self.maintenance.status())

    async def maintain(self, request):
        if request.match_info['action'] == 'reload':
            reload_bot(self.bot_for(request))
            return web.json_response({'ok': True, 'message': 'Saved settings and prompts reloaded.'})
        return web.json_response(self.maintenance.restart(request.match_info['action']))

    async def export_release(self, request):
        return web.Response(body=release_archive(), content_type='application/zip',
                            headers={'Content-Disposition': 'attachment; filename="hearthling-programme.zip"'})

    async def stage_release(self, request):
        return web.json_response(await self.maintenance.stage(await request.read()))

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', self.port).start()
        logging.info('Owner dashboard listening on localhost:%s', self.port)

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
