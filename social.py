"""Bounded channel conversation, durable reminders and optional background messages."""
import asyncio
from datetime import datetime, timezone
import json
import random
import re
import time
from types import SimpleNamespace
from typing import Annotated
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from conversation import profile, text_only
from images import prompt_messages
from pending import RequestQueue
import wordle

# One shared roll per human message across every managed bot; a hit lets one of them reply unprompted.
RANDOM_REPLY_CHANCE = 0.001
RANDOM_IMAGE_CHANCE = 0.0003


class ChannelRule(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool = False
    chance: int = Field(default=10, ge=1, le=50)
    cooldown: int = Field(default=120, ge=0, le=86400)
    bot_triggers: bool = True
    nickname_trigger: bool = True


class QuietWindow(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    days: list[int] = Field(min_length=1, max_length=7)
    start: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    end: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')

    @field_validator('days')
    @classmethod
    def weekdays(cls, value):
        if any(day < 0 or day > 6 for day in value):
            raise ValueError('Weekdays run from 0 (Monday) to 6 (Sunday).')
        return sorted(set(value))


class SocialOptions(BaseModel):
    server_replies: bool = True
    dm_replies: bool = True
    channel_whitelist_enabled: bool = False
    channel_whitelist: list[str] = Field(default_factory=list, max_length=200)
    channel_blacklist: list[str] = Field(default_factory=list, max_length=200)
    ignored_users: list[str] = Field(default_factory=list, max_length=500)
    dm_blacklist: list[str] = Field(default_factory=list, max_length=500)
    nicknames: list[str] = Field(default_factory=list, max_length=20)
    channel_rules: dict[str, ChannelRule] = Field(default_factory=dict, max_length=200)
    single_user_mode: bool = False
    split_replies: bool = False
    split_max: int = Field(default=5, ge=1, le=10)
    mention_users: bool = False
    mention_bots: bool = False
    mention_context_max: int = Field(default=10, ge=0, le=30)
    reply_context_style: str = Field(default='neutral', pattern=r'^(neutral|legacy)$')
    bot_conversations: bool = True
    reply_stagger: int = Field(default=2, ge=0, le=30)
    spontaneous_enabled: bool = False
    spontaneous_channels: list[str] = Field(default_factory=list, max_length=50)
    spontaneous_min_minutes: int = Field(default=60, ge=1, le=10080)
    spontaneous_max_minutes: int = Field(default=180, ge=1, le=10080)
    spontaneous_silence_minutes: int = Field(default=5, ge=0, le=1440)
    spontaneous_prompt: str = Field(default='Start a short, natural conversation in your own character. Do not pretend somebody asked you a question.', max_length=4000)
    timezone: str = Field(default='UTC', max_length=100)
    unavailable_windows: list[QuietWindow] = Field(default_factory=list, max_length=30)
    reminders_enabled: bool = True
    dm_followups_enabled: bool = False
    dm_followup_after_minutes: int = Field(default=120, ge=1, le=10080)
    dm_followup_cooldown_hours: int = Field(default=24, ge=1, le=720)
    dm_followup_max: int = Field(default=1, ge=1, le=10)
    dm_followup_images: bool = False
    dm_followup_image_chance: int = Field(default=25, ge=0, le=100)
    dm_followup_caption_chance: int = Field(default=85, ge=0, le=100)
    dm_followup_image_prompt: str = Field(default='A small friendly picture inspired by your character and the recent conversation.', max_length=4000)
    time_passage_context: bool = True
    recent_context: int = Field(default=20, ge=0, le=200)
    wordle_enabled: bool = False
    wordle_channel: str = ''
    wordle_time: str = Field(default='09:00', pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    quiet_channels: list[str] = Field(default_factory=list, max_length=200)
    releases_enabled: bool = False
    releases_channel: str = ''
    releases_repositories: list[Annotated[str, Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')]] = Field(default_factory=list, max_length=10)
    news_enabled: bool = False
    news_channel: str = ''
    news_hours: int = Field(default=6, ge=1, le=48)
    news_feeds: list[Annotated[str, Field(pattern=r'^https?://\S{1,2000}$')]] = Field(default_factory=list, max_length=30)
    news_topic: str = Field(default='Notable releases and announcements in the topics these feeds cover.', min_length=1, max_length=2000)

    @model_validator(mode='before')
    @classmethod
    def retire_bot_limits(cls, values):
        if isinstance(values, dict):
            return {key: value for key, value in values.items()
                    if key not in {'bot_start_chance', 'bot_decay', 'bot_floor_chance', 'bot_turn_limit'}}
        return values

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('Use an IANA timezone such as Europe/London or Asia/Manila.') from None
        return value

    @field_validator('channel_whitelist', 'channel_blacklist', 'ignored_users', 'dm_blacklist', 'spontaneous_channels', 'quiet_channels')
    @classmethod
    def discord_ids(cls, value):
        if any(not re.fullmatch(r'[0-9]{15,21}', item) for item in value):
            raise ValueError('Use Discord IDs containing 15 to 21 digits.')
        return list(dict.fromkeys(value))

    @field_validator('channel_rules')
    @classmethod
    def channel_ids(cls, value):
        cls.discord_ids(list(value))
        return value

    @field_validator('wordle_channel', 'releases_channel', 'news_channel')
    @classmethod
    def optional_wordle_channel(cls, value):
        if value and not re.fullmatch(r'[0-9]{15,21}', value):
            raise ValueError('Use a channel ID containing 15 to 21 digits, or leave it blank.')
        return value

    @field_validator('nicknames')
    @classmethod
    def plain_names(cls, value):
        if any(not name.strip() or len(name) > 50 or any(ord(c) < 32 or c in '<>@' for c in name) for name in value):
            raise ValueError('Nicknames must be plain names of 1 to 50 characters.')
        return list(dict.fromkeys(name.strip() for name in value))

    @model_validator(mode='after')
    def timing(self):
        if self.spontaneous_max_minutes < self.spontaneous_min_minutes:
            raise ValueError('The maximum random-message interval must be at least the minimum.')
        if self.spontaneous_enabled and not self.spontaneous_channels:
            raise ValueError('Choose at least one channel before enabling random messages.')
        if self.wordle_enabled and not self.wordle_channel:
            raise ValueError('Choose a Wordle channel before enabling the daily game.')
        if self.releases_enabled and not (self.releases_channel and self.releases_repositories):
            raise ValueError('Choose a channel and at least one GitHub repository before enabling release announcements.')
        if self.news_enabled and not (self.news_channel and self.news_feeds):
            raise ValueError('Choose a channel and at least one feed before enabling news roundups.')
        return self


class Social:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db
        self.task = self.notice_task = None
        self.admitted, self.last_reply, self.chains, self.unprompted = {}, {}, {}, {}
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS timezones (user_id TEXT PRIMARY KEY, zone TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                guild_id TEXT, due_at REAL NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                result TEXT, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                user_id TEXT NOT NULL, channel_id TEXT NOT NULL, last_in REAL NOT NULL DEFAULT 0,
                last_out REAL NOT NULL DEFAULT 0, last_followup REAL NOT NULL DEFAULT 0, followups INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, channel_id)
            );
            CREATE TABLE IF NOT EXISTS social_state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bot_reply_sources (message_id TEXT PRIMARY KEY);
        ''')
        self.queue = RequestQueue(bot)

    @property
    def config(self):
        return self.bot.features.settings

    def state(self, name, default=None):
        row = self.db.execute('SELECT value FROM social_state WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def save_state(self, name, value):
        self.db.execute('INSERT OR REPLACE INTO social_state VALUES (?, ?)', (name, json.dumps(value)))
        self.db.commit()

    def zone(self, user_id=None):
        row = self.db.execute('SELECT zone FROM timezones WHERE user_id=?', (str(user_id),)).fetchone() if user_id else None
        return row[0] if row else self.config.timezone

    def set_zone(self, user_id, zone):
        if zone:
            SocialOptions.valid_timezone(zone)
            self.db.execute('INSERT OR REPLACE INTO timezones VALUES (?, ?)', (str(user_id), zone))
        else:
            self.db.execute('DELETE FROM timezones WHERE user_id=?', (str(user_id),))
        self.db.commit()
        return self.zone(user_id)

    def available(self, now=None):
        local = datetime.fromtimestamp(now or time.time(), ZoneInfo(self.config.timezone))
        minute = local.hour * 60 + local.minute
        for window in self.config.unavailable_windows:
            start, end = (int(value[:2]) * 60 + int(value[3:]) for value in (window.start, window.end))
            if start < end:
                blocked = local.weekday() in window.days and start <= minute < end
            else:
                blocked = ((local.weekday() in window.days and minute >= start)
                           or ((local.weekday() - 1) % 7 in window.days and minute < end))
            if blocked:
                return False
        return True

    def access(self, message, *, check_schedule=True):
        config = self.config
        if not self.bot.accepting_messages or str(message.author.id) in config.ignored_users:
            return False
        if check_schedule and not self.available():
            return False
        if message.guild is None:
            return (config.dm_replies and message.author.id == self.bot.features.owner_id and not message.author.bot
                    and str(message.author.id) not in config.dm_blacklist)
        channel = str(message.channel.id)
        return (message.guild.id == self.bot.server_id and config.server_replies and not config.server_paused
                and channel not in config.channel_blacklist
                and (not config.channel_whitelist_enabled or channel in config.channel_whitelist)
                and (config.public_chat or message.author.id == self.bot.features.owner_id or self.managed_sender(message)))

    def managed_sender(self, message):
        manager = getattr(self.bot, 'manager', None)
        return bool(message.author.bot and message.author.id != self.bot.bot_id and manager
                    and str(message.author.id) in manager.bots and message.guild
                    and manager.bots[str(message.author.id)].server_id == self.bot.server_id)

    def interactions_stopped(self):
        manager = getattr(self.bot, 'manager', None)
        db = manager.db if manager else self.db
        row = db.execute("SELECT value FROM settings WHERE name='bot_interactions_paused'").fetchone()
        return bool(row and json.loads(row[0]))

    def stop_interactions(self, stopped=True):
        manager = getattr(self.bot, 'manager', None)
        db = manager.db if manager else self.db
        db.execute("INSERT OR REPLACE INTO settings VALUES ('bot_interactions_paused', ?)", (json.dumps(stopped),))
        db.commit()

    def consider(self, message):
        if message.author.id == self.bot.bot_id or not self.access(message):
            return False
        now = time.time()
        channel_id = str(message.channel.id)
        if not message.author.bot:
            self.db.execute('INSERT INTO activity (user_id, channel_id, last_in) VALUES (?, ?, ?) '
                            'ON CONFLICT(user_id,channel_id) DO UPDATE SET last_in=excluded.last_in, followups=0',
                            (str(message.author.id), channel_id, now))
            self.db.commit()
        manager = getattr(self.bot, 'manager', None)
        chains = manager.bot_chains if manager else self.chains
        key = (self.bot.server_id, message.channel.id)
        previous_id, depth = chains.get(key, (0, 0))
        if int(message.id) > previous_id:
            depth = depth if message.author.bot else 0
            chains[key] = (int(message.id), depth)
        rule = self.config.channel_rules.get(channel_id, ChannelRule())
        explicit = self.bot.directly_addressed(message, nickname_trigger=rule.nickname_trigger)
        if message.author.bot:
            shared_db = manager.db if manager else self.db
            reference_id = str(getattr(getattr(message, 'reference', None), 'message_id', ''))
            if (not self.managed_sender(message) or not self.config.bot_conversations or self.interactions_stopped()
                    or not rule.bot_triggers or depth >= 1 or not (explicit or rule.enabled)
                    or shared_db.execute('SELECT 1 FROM bot_reply_sources WHERE message_id IN (?,?)',
                                         (str(message.id), reference_id)).fetchone()):
                return False
            accepted = random.random() < 0.10
        else:
            picture = self.random_pick(message, image=True)
            chimed = not explicit and self.random_pick(message)
            if picture or chimed:
                self.unprompted[str(message.id)] = 'image' if picture else 'reply'
                if len(self.unprompted) > 512:
                    self.unprompted.pop(next(iter(self.unprompted)))
            accepted = explicit or picture or chimed or (message.guild and rule.enabled and now - self.last_reply.get(channel_id, 0) >= rule.cooldown
                                              and random.random() * 100 < rule.chance)
        if accepted:
            if message.author.bot:
                # Reserve before any await so several bots cannot exceed the same reply budget.
                chains[key] = (max(int(message.id), previous_id), depth + 1)
                shared_db.execute('INSERT OR IGNORE INTO bot_reply_sources VALUES (?)', (str(message.id),))
                shared_db.commit()
            self.admitted[str(message.id)] = not explicit or message.author.bot
            if len(self.admitted) > 512:
                self.admitted.pop(next(iter(self.admitted)))
            self.last_reply[channel_id] = now
        return bool(accepted)

    def random_pick(self, message, *, image=False):
        """Whether this bot won the shared text or image roll for a human message."""
        quiet = self.config.quiet_channels if getattr(self.bot, 'features', None) else ()
        if message.author.bot or message.guild is None or str(message.channel.id) in quiet:
            return False
        # Seeded by the message so every bot account reaches the same outcome without coordinating.
        roll = random.Random(f'random-{"image" if image else "reply"}:{message.id}')
        if roll.random() >= (RANDOM_IMAGE_CHANCE if image else RANDOM_REPLY_CHANCE):
            return False
        manager = getattr(self.bot, 'manager', None)
        bots = manager.bots.values() if manager else (self.bot,)
        candidates = sorted(bot.bot_id for bot in bots
                            if bot.server_id == self.bot.server_id and getattr(bot, 'accepting_messages', True)
                            and (not image or (getattr(bot, 'features', None)
                                               and 'generate_image' in bot.features.allowed_actions(message))))
        return bool(candidates) and roll.choice(candidates) == self.bot.bot_id

    def unprompted_note(self, request):
        if str(getattr(request, 'id', '')) not in self.unprompted:
            return ''
        if self.unprompted[str(request.id)] == 'image':
            return ('The shared random image roll selected you to create a spontaneous picture inspired by this user message. '
                    'Call generate_image once with a vivid, positive prompt grounded in what the speaker said and the current conversation. '
                    'This selection authorises this picture even though the speaker did not request artwork. '
                    'Use saved appearance facts for any depicted people and include them in subjects. '
                    'After the tool result, respond briefly and naturally in character, presenting the picture as your own initiative.')
        return ('This message was not addressed to you. You noticed it and chose to chime in on your own: '
                'reply briefly and naturally in character, and do not claim anyone called or asked you.')

    def response_allowed(self, request):
        return self.access(request) and (not request.author.bot or
               (self.managed_sender(request) and self.config.bot_conversations and not self.interactions_stopped()))

    def identity_ok(self, text):
        manager = getattr(self.bot, 'manager', None)
        others = [bot.character_name for bot in manager.bots.values() if bot.bot_id != self.bot.bot_id] if manager else []
        return not any(re.search(r'(?im)^\s*(?:\*\*)?' + re.escape(name) + r'(?:\*\*)?\s*:', text) for name in others)

    def reminder_list(self, user_id=None, channel_id=None, *, due_before=None, include_dm=False):
        conditions, values = [], []
        for column, value in (('user_id', user_id), ('channel_id', channel_id)):
            if value is not None:
                conditions.append("channel_id IN (?, 'dm')" if column == 'channel_id' and include_dm else column + '=?')
                values.append(str(value))
        if due_before is not None:
            conditions.append("state='pending' AND due_at<=?")
            values.append(due_before)
        rows = self.db.execute('SELECT id,user_id,channel_id,guild_id,due_at,message,state,result FROM reminders '
                               + ('WHERE ' + ' AND '.join(conditions) if conditions else '')
                               + (' ORDER BY due_at ASC LIMIT 20' if due_before is not None else ' ORDER BY due_at DESC LIMIT 200'), values)
        return [dict(zip(('id', 'user_id', 'channel_id', 'guild_id', 'due_at', 'message', 'state', 'result'), row)) for row in rows]

    def add_reminder(self, request, due_at, message, *, dm=False):
        self.bot.features.require_assistant()
        if not self.config.reminders_enabled or request.author.bot or not self.access(request):
            raise ValueError('Reminders are unavailable in this conversation.')
        if dm and request.author.id != self.bot.features.owner_id:
            raise PermissionError('Direct-message reminders are owner-only.')
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 1000 or self.bot.features.redact(message) != message:
            raise ValueError('Use a reminder of 1 to 1000 characters without credentials or private sign-in links.')
        try:
            due = datetime.fromisoformat(due_at.replace('Z', '+00:00'))
            if due.tzinfo is None:
                zone = ZoneInfo(self.zone(request.author.id))
                if due.replace(tzinfo=zone, fold=0).utcoffset() != due.replace(tzinfo=zone, fold=1).utcoffset():
                    raise ValueError('That local time is ambiguous; include its UTC offset.')
                due = due.replace(tzinfo=zone)
            timestamp = due.timestamp()
        except (TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Use a valid future date and time, including a UTC offset when clocks change.') from exc
        now = time.time()
        if not now < timestamp <= now + 366 * 86400:
            raise ValueError('Choose a reminder time in the future and within one year.')
        if self.db.execute("SELECT count(*) FROM reminders WHERE user_id=? AND state='pending'", (str(request.author.id),)).fetchone()[0] >= 100:
            raise ValueError('This person already has 100 pending reminders. Cancel one first.')
        cursor = self.db.execute('INSERT INTO reminders (user_id,channel_id,guild_id,due_at,message,created_at) VALUES (?,?,?,?,?,?)',
                                 (str(request.author.id), 'dm' if dm else str(request.channel.id),
                                  str(request.guild.id) if request.guild and not dm else None, timestamp, message.strip(), now))
        self.db.commit()
        return {'id': cursor.lastrowid, 'due_at': due.isoformat(), 'timezone': self.zone(request.author.id), 'delivery': 'DM' if dm else 'this conversation'}

    def cancel_reminder(self, user_id, identity, *, owner=False):
        cursor = self.db.execute("UPDATE reminders SET state='cancelled' WHERE id=? AND state='pending'" + ('' if owner else ' AND user_id=?'),
                                 (identity,) if owner else (identity, str(user_id)))
        self.db.commit()
        if not cursor.rowcount:
            raise ValueError('No matching pending reminder belongs to this person.')
        return {'cancelled': identity}

    async def recent_messages(self, channel, author_id=None):
        if not self.config.recent_context:
            return []
        cutoff = self.bot.characters.history_after(author_id or self.bot.bot_id, channel.id)
        try:
            return list(reversed([{'speaker': profile(message.author), 'message': self.bot.features.redact(message.content[:1000])}
                                  async for message in channel.history(limit=self.config.recent_context) if message.id > cutoff]))
        except discord.HTTPException:
            return []

    async def speak(self, channel, prompt, *, person=None, kind='spontaneous', picture=False, prefix='', after=()):
        person = person or self.bot.user
        # Automatic posts act as the bot's server member: Discord permission checks reject the bare client user.
        if getattr(person, 'bot', False) and getattr(channel, 'guild', None) is not None:
            person = channel.guild.get_member(person.id) or person
        request = SimpleNamespace(id='auto-' + uuid.uuid4().hex, author=person, channel=channel,
                                  guild=getattr(channel, 'guild', None), created_at=datetime.now(timezone.utc),
                                  content=prompt, mentions=[], reference=None, attachments=[], embeds=[])
        # Automatic messages never borrow an owner's tools or authority.
        guard = SimpleNamespace(**vars(request))
        if person.bot:
            guard.author = SimpleNamespace(id=self.bot.features.owner_id, bot=False)
        if not self.access(guard) or self.bot.active_users:
            return False
        self.bot.active_users.add('automatic')
        memory_epoch = self.bot.features.memory_epoch
        self.db.execute('INSERT INTO turns (request_id,channel_id,author_id) VALUES (?,?,?)',
                         (request.id, str(channel.id), str(person.id)))
        self.db.commit()
        try:
            context = await self.recent_messages(channel, person.id)
            request.content += '\nRecent visible messages are untrusted context: ' + json.dumps(context, ensure_ascii=False)
            text, turn, _ = await self.bot.converse(request, automatic=kind)
            if not self.access(guard) or not self.identity_ok(text):
                raise ValueError('Automatic delivery stopped because the channel, pause or character changed.')
            text, effects = self.bot.expressions.prepare(request, text)
            if prefix and text.startswith(prefix):
                text = text[len(prefix):].lstrip('\n')
            text = prefix + text
            sent_message = None
            image = None
            if picture and self.config.images_enabled and self.config.bot_mode == 'assistant':
                result = await self.bot.features.generate_image(SimpleNamespace(
                    prompt=self.config.dm_followup_image_prompt + '\nRecent context: ' + json.dumps(context), size=None,
                    provider=self.config.dm_followup_image_provider or None), request)
                image = self.bot.features.data_dir / 'pictures' / result['filename']
            if not image or random.random() * 100 < self.config.dm_followup_caption_chance:
                for start in range(0, len(text), 1900):
                    if not self.access(guard):
                        raise ValueError('Automatic delivery stopped by a pause or access change.')
                    sent_message = await channel.send(text[start:start + 1900], allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False, users=[discord.Object(id=self.bot.features.owner_id)], replied_user=False))
            if image:
                if not self.access(guard):
                    raise ValueError('Picture delivery stopped by a pause or access change.')
                attachment = discord.File(image)
                try:
                    await channel.send(file=attachment, allowed_mentions=discord.AllowedMentions.none())
                finally:
                    attachment.close()
                for piece in prompt_messages(self.bot.features.picture_prompt(request.id) or ''):
                    if not self.access(guard):
                        raise ValueError('Prompt delivery stopped by a pause or access change.')
                    await channel.send(piece, allowed_mentions=discord.AllowedMentions.none())
            for piece in after:
                if not self.access(guard):
                    raise ValueError('Automatic delivery stopped by a pause or access change.')
                await channel.send(piece, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            await self.bot.expressions.after(request, effects, sent_message=sent_message, automatic=True)
            self.db.execute("UPDATE turns SET state='complete',messages=? WHERE request_id=?",
                             (self.bot.features.redact(json.dumps(text_only(turn) if memory_epoch == self.bot.features.memory_epoch else [])), request.id))
            self.db.execute('INSERT INTO activity (user_id,channel_id,last_out) VALUES (?,?,?) '
                            'ON CONFLICT(user_id,channel_id) DO UPDATE SET last_out=excluded.last_out',
                             (str(person.id), str(channel.id), time.time()))
            self.db.commit()
            self.queue.answered(request)
            return True
        except Exception:
            self.db.execute("UPDATE turns SET state='failed' WHERE request_id=?", (request.id,))
            self.db.commit()
            raise
        finally:
            self.bot.active_users.discard('automatic')

    def wordle_note(self, channel_id='', text=''):
        """The bot's own guesses for today's puzzle, or '' when none or the day has passed."""
        game = self.state('wordle-words')
        if not isinstance(game, dict) or game.get('day') != wordle.today() or not game.get('guesses'):
            return ''
        if str(channel_id) != str(game.get('channel_id')) and 'wordle' not in (text or '').lower():
            return ''
        words = ', '.join(str(word) for word in game['guesses'])
        result = ('solved it in %d of 6 guesses' % len(game['guesses'])) if game.get('won') else 'did not solve it within 6 guesses'
        return (f"Today's Wordle (puzzle {game.get('number')}): your guesses in order were {words}; you {result}. "
                'These stored guesses are the only ones you made; never invent or alter one. '
                'If you mention any guessed word or the answer, hide each word with Discord spoiler tags like ||word||.')

    async def run_wordle(self, channel=None, *, force=False):
        if channel is None and self.config.wordle_channel:
            channel = self.bot.get_channel(int(self.config.wordle_channel))
        if channel is None or getattr(channel, 'guild', None) is None or channel.guild.id != self.bot.server_id:
            raise ValueError('Choose a Wordle channel in the configured server.')
        day, answer, number = await wordle.daily()
        played = 'wordle:' + str(channel.id)
        if not force and self.state(played) == day:
            return False
        words = await wordle.word_list()
        history = await wordle.play(self.bot, answer, words, day)
        if not history:
            raise ValueError('The model did not produce a usable first guess.')
        won = history[-1][0] == answer
        block = wordle.share(number, history, answer)
        self.save_state('wordle-words', {'day': day, 'number': number, 'guesses': [guess for guess, _ in history],
                                         'won': won, 'channel_id': str(channel.id)})
        outcome = f'solved it in {len(history)} of 6 guesses' if won else 'did not solve it within 6 guesses'
        prompt = (f'You just finished today’s Wordle and your result card is already posted above. You {outcome}. '
                  'Write one short, natural message in your own voice about that game, without repeating the card. '
                  'You may react to the result cards other players already posted in the recent messages. '
                  'If you mention any guessed word or today’s answer, hide each word with Discord spoiler tags like ||word||.')
        sent = await self.speak(channel, prompt, kind='wordle', prefix=block + '\n')
        if sent:
            self.save_state(played, day)
        self.bot.features.record_status('wordle', {'ok': bool(sent), 'day': day, 'channel_id': str(channel.id),
                                                   'guesses': len(history), 'won': won})
        return bool(sent)

    async def deliver_reminder(self, reminder):
        self.bot.features.require_assistant()
        person = await self.bot.fetch_user(int(reminder['user_id']))
        if reminder['guild_id']:
            channel = self.bot.get_channel(int(reminder['channel_id'])) or await self.bot.fetch_channel(int(reminder['channel_id']))
            if not getattr(channel, 'guild', None) or channel.guild.id != self.bot.server_id:
                raise ValueError('The reminder channel is no longer in the configured server.')
            member = channel.guild.get_member(person.id) or await channel.guild.fetch_member(person.id)
            permissions = channel.permissions_for(member)
            if not permissions.view_channel or not permissions.read_message_history:
                raise ValueError('The reminder author can no longer read that channel.')
            person = member
        elif person.id == self.bot.features.owner_id:
            channel = await person.create_dm()
        else:
            raise PermissionError('Direct-message reminders are owner-only.')
        prompt = 'Deliver this scheduled reminder naturally to the named person now. Do not schedule it again: ' + reminder['message']
        try:
            return await self.speak(channel, prompt, person=person, kind='reminder')
        except discord.Forbidden:
            if person.id != self.bot.features.owner_id or not reminder['guild_id']:
                raise
            return await self.speak(await person.create_dm(), prompt, person=person, kind='reminder')

    async def tick(self, now=None):
        now = now or time.time()
        if not self.bot.is_ready() or not self.bot.accepting_messages or self.bot.active_users or not self.available(now):
            return
        await self.queue.tick(now)
        if self.config.reminders_enabled and self.config.bot_mode == 'assistant':
            reminders = self.reminder_list(due_before=now)
            for item in reminders:
                if item['state'] != 'pending' or item['due_at'] > now or (item['guild_id'] and self.config.server_paused):
                    continue
                if str(item['user_id']) in self.config.ignored_users:
                    continue
                claimed = self.db.execute("UPDATE reminders SET state='sending' WHERE id=? AND state='pending'", (item['id'],))
                self.db.commit()
                if not claimed.rowcount:
                    continue
                try:
                    sent = await self.deliver_reminder(item)
                    state, result = ('sent', 'Delivered.') if sent else ('pending', 'Waiting for access or availability.')
                except Exception as exc:
                    state, result = 'uncertain', self.bot.features.clean_error(exc)
                self.db.execute('UPDATE reminders SET state=?,result=? WHERE id=?', (state, result, item['id']))
                self.db.commit()
                self.bot.features.record_status('reminders', {'ok': state == 'sent', 'id': item['id'], 'state': state, 'result': result})
        if self.config.spontaneous_enabled and not self.config.server_paused:
            for identity in self.config.spontaneous_channels:
                due = self.state('next:' + identity)
                if due is None:
                    self.save_state('next:' + identity, now + random.randint(self.config.spontaneous_min_minutes, self.config.spontaneous_max_minutes) * 60)
                    continue
                if due > now:
                    continue
                self.save_state('next:' + identity, now + random.randint(self.config.spontaneous_min_minutes, self.config.spontaneous_max_minutes) * 60)
                last = self.db.execute('SELECT max(last_in) FROM activity WHERE channel_id=?', (identity,)).fetchone()[0] or 0
                if now - last < self.config.spontaneous_silence_minutes * 60:
                    continue
                channel = self.bot.get_channel(int(identity))
                if channel and getattr(channel, 'guild', None) and channel.guild.id == self.bot.server_id:
                    await self.speak(channel, self.config.spontaneous_prompt)
        if self.config.wordle_enabled and self.config.wordle_channel and not self.config.server_paused:
            local = datetime.fromtimestamp(now, ZoneInfo(self.config.timezone))
            if local.strftime('%H:%M') >= self.config.wordle_time:
                next_key = 'wordle-next:' + str(self.bot.bot_id)
                if now >= self.state(next_key, 0):
                    channel = self.bot.get_channel(int(self.config.wordle_channel))
                    if channel and getattr(channel, 'guild', None) and channel.guild.id == self.bot.server_id:
                        try:
                            await self.run_wordle(channel)
                            self.save_state(next_key, now + 86400)
                        except Exception as exc:
                            self.save_state(next_key, now + 1800)
                            self.bot.features.record_status('wordle', {'ok': False, 'error': self.bot.features.clean_error(exc)})
        if self.config.dm_followups_enabled and self.config.dm_replies:
            rows = self.db.execute('SELECT user_id,channel_id,last_in,last_out,last_followup,followups FROM activity WHERE user_id=?',
                                    (str(self.bot.features.owner_id),)).fetchall()
            for user_id, channel_id, last_in, last_out, last_followup, count in rows:
                if (count >= self.config.dm_followup_max or not last_in or now - max(last_in, last_out) < self.config.dm_followup_after_minutes * 60
                        or now - last_followup < self.config.dm_followup_cooldown_hours * 3600):
                    continue
                person = await self.bot.fetch_user(int(user_id))
                channel = await person.create_dm()
                if str(channel.id) != channel_id or user_id in self.config.dm_blacklist or user_id in self.config.ignored_users:
                    continue
                # Persist the attempt before sending so a restart cannot multiply follow-ups.
                self.db.execute('UPDATE activity SET last_followup=?,followups=followups+1 WHERE user_id=? AND channel_id=?', (now, user_id, channel_id))
                self.db.commit()
                await self.speak(channel, 'Send a brief, natural follow-up after this conversation went quiet. Do not claim an event or promise not supported by the context.',
                                 person=person, kind='dm_followup', picture=self.config.dm_followup_images and random.random() * 100 < self.config.dm_followup_image_chance)

    def start(self):
        if self.task is None or self.task.done():
            # Recover only when the owning bot starts, never when a dashboard-link helper opens the database.
            self.db.execute("UPDATE reminders SET state='uncertain', result='Delivery was interrupted; check Discord before rescheduling.' WHERE state='sending'")
            self.db.commit()
            self.queue.recover()
            self.task = asyncio.create_task(self.run(), name=f'social-{self.bot.bot_id}')
        if self.notice_task is None or self.notice_task.done():
            self.notice_task = asyncio.create_task(self.run(notices=True), name=f'queue-notices-{self.bot.bot_id}')

    async def run(self, *, notices=False):
        while True:
            try:
                if notices:
                    await self.queue.notices(time.time())
                else:
                    await self.tick()
            except Exception as exc:
                self.bot.features.record_status('automation', {'ok': False, 'error': self.bot.features.clean_error(exc)})
            await asyncio.sleep(30)

    async def close(self):
        tasks = [task for task in (self.task, self.notice_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
