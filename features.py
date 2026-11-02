"""Pictures, web search, host health checks and scoped lasting memories."""
import asyncio
import base64
from datetime import datetime, timezone
import html
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from social import SocialOptions
from providers import ProviderOptions, ReasoningEffort, connection_url
from images import BUILTIN_IMAGE_MODELS, IMAGE_STYLE, ImageOptions, download_image, extension, gemini_picture, image_request, request_target, selected, styled
from conversation import image_url, message_images, profile, resolved_reply
from appearances import about_person, grounded_prompt, public_appearance, request_appearances
from web_sources import IMAGE_BYTES, PAGE_BYTES, fetch_resource, page_content, picture_extension, public_url


IMAGE_MODEL = 'gpt-image-1'
SHARED_SCOPE = ''
RETIRED_IMAGE_LIMITS = ('image_cooldown', 'images_per_user', 'images_per_day')


def strip_retired_limits(values):
    """Let settings saved before the picture limits were removed load without error."""
    return {name: value for name, value in values.items() if name not in RETIRED_IMAGE_LIMITS}
BRAVE_URL = 'https://api.search.brave.com/res/v1/web/search'
BRAVE_IMAGES_URL = 'https://api.search.brave.com/res/v1/images/search'
ImageSize = Literal['1024x1024', '1024x1536', '1536x1024']


KEYWORD_STOPWORDS = frozenset('''
a about after again also am an and any are as at be been being before but by can could did do does
each every for from had has have he her hers him his if in into is it its just like likes loved loves
many may me member members might mine more most much must my no not of on once only or other our ours
over people person prefer prefers really server servers shall she should so some than that the their
theirs them then there these they this those too under until up us user users very want wants was we
were what when where which while who whom why will with would you your yours
'''.split())


def memory_keywords(fact):
    words, seen = [], set()
    for word in re.findall(r'[^\W\d_]+', (fact or '').casefold()):
        if len(word) < 2 or word in KEYWORD_STOPWORDS or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return ', '.join(words[:20])


def clean_keywords(value):
    keywords, seen = [], set()
    for part in (value or '').split(','):
        keyword = ' '.join(part.casefold().split())
        if not keyword or len(keyword) > 50 or keyword in seen:
            continue
        seen.add(keyword)
        keywords.append(keyword)
    return ', '.join(keywords[:20])


def keyword_hits(keywords, text):
    words = set(re.findall(r'\w+', text.casefold()))
    return sum(bool(re.search(r'(?<!\w)' + re.escape(keyword) + r'(?!\w)', text, re.I))
               if ' ' in keyword else keyword.casefold() in words
               for keyword in (part.strip() for part in keywords.split(',')) if keyword)


IDENTITY_FACT = re.compile(
    r'\b(?:pronouns?|he/him|she/her|they/them|age|birthday|species|years? old|nationality|ethnicity|citizenship|creator|written by|'
    r'is (?:a |an )?(?:girl|boy|woman|man|lad|male|female|human))\b', re.I)


class Settings(SocialOptions, ProviderOptions, ImageOptions):
    model_config = ConfigDict(extra='forbid', strict=True)
    personality: Annotated[str, Field(min_length=1, max_length=30000)]
    bot_mode: Literal['assistant', 'chatbot'] = 'assistant'
    ai_model: Annotated[str, Field(min_length=1, max_length=150)] = 'gpt-5-mini'
    ai_fallback_models: list[str] = Field(default_factory=list, max_length=10)
    ai_fallback_reasoning_efforts: dict[str, ReasoningEffort | None] = Field(default_factory=dict, max_length=10)
    ai_base_url: str = 'https://api.openai.com/v1'
    ai_reasoning_effort: ReasoningEffort | None = None
    ai_prefill: str = Field(default='', max_length=4000)
    image_base_url: str = ''
    image_family: Literal['openai_images', 'responses', 'gemini'] = 'openai_images'
    image_model: Annotated[str, Field(min_length=1, max_length=150)] = IMAGE_MODEL
    image_coordinator_model: Annotated[str, Field(min_length=1, max_length=150)] = 'gpt-5-mini'
    image_style: str = Field(default=IMAGE_STYLE, max_length=300)
    public_chat: bool = True
    server_paused: bool = False
    name_trigger: bool = True
    history_turns: Annotated[int, Field(ge=0, le=200)] = 8
    vision_enabled: bool = False
    vision_profiles: bool = True
    vision_detail: Literal['auto', 'low', 'high'] = 'auto'
    vision_max_images: Annotated[int, Field(ge=1, le=10)] = 8
    images_enabled: bool = True
    public_images: bool = True
    image_size: ImageSize = '1024x1024'
    image_quality: Literal['auto', 'low', 'medium', 'high', 'xhigh', 'max'] = 'high'
    search_enabled: bool = True
    public_search: bool = True
    member_pings: bool = True
    personal_colours: bool = True
    host_status_enabled: bool = True
    host_services: list[Annotated[str, Field(pattern=r'^[A-Za-z0-9@_.:-]{1,100}$')]] = Field(default_factory=list, max_length=30)
    memory_enabled: bool = True
    auto_memory: bool = True
    memory_context_chars: Annotated[int, Field(ge=0, le=20000)] = 4000
    expressions_enabled: bool = True
    expression_uploads: bool = True
    public_expression_uploads: bool = True
    expression_cooldown: int = Field(default=60, ge=0, le=3600)
    expression_uploads_per_user: int = Field(default=5, ge=1, le=100)
    expression_uploads_per_day: int = Field(default=20, ge=1, le=500)
    active_character: str = Field(default='', max_length=50)
    post_prompts: dict[str, str] = Field(default_factory=dict, max_length=5)

    @field_validator('post_prompts')
    @classmethod
    def prompt_sections(cls, value):
        if set(value) - {'chatroom', 'reminder', 'dm_followup', 'time_passage', 'polisher'} or any(len(text) > 8000 for text in value.values()):
            raise ValueError('Use the five named post-system sections, each at most 8000 characters.')
        return value

    @field_validator('ai_fallback_models')
    @classmethod
    def fallback_models(cls, value):
        value = [item.strip() for item in value]
        if any(not item or len(item) > 150 for item in value) or len(set(value)) != len(value):
            raise ValueError('List each fallback model once, one per line, up to 150 characters each.')
        return value

    @field_validator('ai_fallback_reasoning_efforts')
    @classmethod
    def fallback_reasoning_models(cls, value):
        if any(model != model.strip() or not model or len(model) > 150 for model in value):
            raise ValueError('Fallback reasoning names must match model names without surrounding spaces.')
        return value

    @model_validator(mode='after')
    def prune_fallback_reasoning(self):
        self.ai_fallback_reasoning_efforts = {model: effort for model, effort in self.ai_fallback_reasoning_efforts.items()
                                              if model in self.ai_fallback_models}
        return self

    @field_validator('ai_base_url', 'image_base_url')
    @classmethod
    def connection_url(cls, value, info):
        if not value and info.field_name == 'image_base_url':
            return value
        return connection_url(value)

    @field_validator('personality', 'ai_model')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('This setting cannot be blank.')
        return value


class FeatureAction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['generate_image', 'web_search', 'web_fetch', 'image_search', 'post_image', 'host_status', 'remember', 'recall', 'forget', 'dashboard',
                    'set_reminder', 'list_reminders', 'cancel_reminder', 'set_timezone',
                    'list_expressions', 'react', 'send_sticker', 'add_emoji', 'add_sticker',
                    'lookup_members', 'ping_member', 'set_my_colour']
    prompt: Annotated[str, Field(min_length=1, max_length=5000)] | None = None
    subjects: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=20)
    size: ImageSize | None = None
    art_style: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 10
    fact: Annotated[str, Field(min_length=1, max_length=400)] | None = None
    query: Annotated[str, Field(max_length=400)] = ''
    freshness: Literal['pd', 'pw', 'pm', 'py'] | None = None
    memory_id: Annotated[int, Field(ge=1)] | None = None
    forget_all: bool = False
    shared: bool = False
    due_at: str | None = Field(default=None, max_length=100)
    reminder: str | None = Field(default=None, max_length=1000)
    reminder_id: int | None = Field(default=None, ge=1)
    delivery: Literal['here', 'dm'] = 'here'
    timezone: str | None = Field(default=None, max_length=100)
    expression: str | None = Field(default=None, max_length=100)
    message_id: str | None = Field(default=None, pattern=r'^[0-9]{15,21}$')
    image_url: str | None = Field(default=None, max_length=4096)
    url: str | None = Field(default=None, max_length=4096)
    source_url: str | None = Field(default=None, max_length=4096)
    offset: int | None = Field(default=None, ge=0, le=PAGE_BYTES)
    max_chars: int | None = Field(default=None, ge=1000, le=20000)
    name: str | None = Field(default=None, max_length=32)
    description: str = Field(default='', max_length=100)
    emoji: str = Field(default='🙂', min_length=1, max_length=100)
    member: str | None = Field(default=None, max_length=100)
    colour: str | None = Field(default=None, pattern=r'^#[0-9a-fA-F]{6}$')


class FeatureError(ValueError):
    def __init__(self, message, *, uncertain=False, retry_after=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.retry_after = retry_after


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def redact_private_links(text):
    return re.sub(r'([?#&]login=)[A-Za-z0-9_-]{20,}', r'\1[private sign-in token]', text)


def load_credentials(root):
    credentials = json.loads((root / 'credentials.json').read_text())
    saved = root / 'data' / 'connections.json'
    if saved.exists():
        credentials.update(json.loads(saved.read_text()))
    return credentials


def write_private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Features:
    def __init__(self, bot, credentials, data_dir, owner_id, server_id):
        self.bot, self.db = bot, bot.db
        self.credentials = dict(credentials)
        self.data_dir = Path(data_dir)
        self.owner_id, self.server_id = owner_id, server_id
        self.image_lock = asyncio.Lock()
        self.image_peers = [self]
        self.memories_db = self.db
        self.memory_peers = [self]
        self.memory_epoch = 0
        self.search_cache = {}
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY, author_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                fact TEXT NOT NULL COLLATE NOCASE, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                UNIQUE(author_id, channel_id, fact)
            );
            CREATE TABLE IF NOT EXISTS pictures (
                request_id TEXT PRIMARY KEY, author_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                created_at INTEGER NOT NULL, filename TEXT, state TEXT NOT NULL
            );
        ''')
        if 'prompt' not in {row[1] for row in self.db.execute('PRAGMA table_info(pictures)')}:
            self.db.execute('ALTER TABLE pictures ADD COLUMN prompt TEXT')
        if 'keywords' not in {row[1] for row in self.db.execute('PRAGMA table_info(memories)')}:
            self.db.execute("ALTER TABLE memories ADD COLUMN keywords TEXT NOT NULL DEFAULT ''")
        for row in self.db.execute("SELECT id, fact FROM memories WHERE keywords=''").fetchall():
            self.db.execute('UPDATE memories SET keywords=? WHERE id=?', (memory_keywords(row[1]), row[0]))
        self.db.commit()
        saved = self.db.execute("SELECT value FROM settings WHERE name='configuration'").fetchone()
        defaults = {'personality': bot.personality, 'image_base_url': credentials.get('image_base_url', ''),
                    **{key: credentials[key] for key in ('ai_base_url', 'ai_model', 'image_family', 'image_coordinator_model') if credentials.get(key)}}
        self.settings = Settings.model_validate(defaults | strip_retired_limits(json.loads(saved[0]) if saved else {}))
        self.apply_connection()

    def apply_connection(self):
        if self.bot.ai is not None:
            self.bot.ai.base_url = self.settings.ai_base_url
            if self.credentials.get('ai_api_key'):
                self.bot.ai.api_key = self.credentials['ai_api_key']

    def update_settings(self, changes, keys):
        if not isinstance(changes, dict) or not isinstance(keys, dict):
            raise ValueError('Settings and connections must be objects.')
        settings = Settings.model_validate(self.settings.model_dump() | strip_retired_limits(changes))
        if settings.bot_mode != self.settings.bot_mode and getattr(self.bot, 'active_users', None):
            raise ValueError('Let the current request finish before changing this bot’s type.')
        image_ids = {*BUILTIN_IMAGE_MODELS, *(item.id for item in settings.image_providers)}
        if settings.image_provider not in image_ids or (settings.dm_followup_image_provider and settings.dm_followup_image_provider not in image_ids):
            raise ValueError('Choose a saved image provider.')
        if set(settings.image_fallback_providers) - image_ids:
            raise ValueError('Choose saved fallback image providers.')
        if getattr(self.bot, 'characters', None) and settings.active_character != self.settings.active_character:
            if self.bot.active_users:
                raise ValueError('Let the current reply finish before switching characters.')
            if settings.active_character and not any(item['name'].casefold() == settings.active_character.casefold() for item in self.bot.characters.list()):
                raise ValueError('Choose a saved character, or the default personality.')
        if getattr(self.bot, 'providers', None):
            for identity in settings.provider_order + ([settings.polisher_provider] if settings.polisher_provider else []):
                self.bot.providers.get(identity)
        if set(keys) - {'ai_api_key', 'image_api_key', 'image_fallback_keys', 'brave_api_key'}:
            raise ValueError('Unknown connection key.')
        for name, value in keys.items():
            values = value if name == 'image_fallback_keys' else [value]
            if (not isinstance(values, list) or len(values) > 3 or any(not isinstance(item, str) or len(item) > 4096
                    or any(char.isspace() for char in item) or (name == 'image_fallback_keys' and not item) for item in values)):
                raise ValueError('Use one private key per field and up to three distinct fallback keys, without spaces.')
            if name == 'image_fallback_keys' and len(set(values)) != len(values):
                raise ValueError('Fallback keys must be distinct.')
        changed_keys = {name: value for name, value in keys.items() if value or name == 'image_fallback_keys'}
        if changed_keys:
            credentials = self.credentials | changed_keys
            write_private_json(self.data_dir / 'connections.json', credentials)
            self.credentials = credentials
            self.search_cache.clear()
        self.db.execute("INSERT OR REPLACE INTO settings VALUES ('configuration', ?)", (settings.model_dump_json(),))
        self.db.commit()
        self.settings = settings
        self.apply_connection()
        if getattr(self.bot, 'characters', None):
            self.bot.characters.apply()

    def owner_request(self, request):
        return (not request.author.bot and request.author.id == self.owner_id
                and (request.guild is None or request.guild.id == self.server_id))

    def require_assistant(self):
        if self.settings.bot_mode != 'assistant':
            raise PermissionError('This account is a regular chatbot. Practical tools are available only to assistant bots.')

    def allowed_actions(self, request):
        owner = self.owner_request(request)
        if (request.author.bot or (request.guild is None and not owner)
                or (request.guild is not None and request.guild.id != self.server_id)
                or (request.guild is not None and self.settings.server_paused)
                or (getattr(self.bot, 'social', None) and not self.bot.social.access(request))
                or (not owner and not self.settings.public_chat)):
            return []
        allowed = ['remember', 'recall', 'forget'] if self.settings.memory_enabled else []
        allowed.append('lookup_members')
        if self.settings.member_pings and request.guild is not None:
            allowed.append('ping_member')
        if self.settings.expressions_enabled:
            allowed.extend(('list_expressions', 'react', 'send_sticker'))
        if self.settings.bot_mode == 'chatbot':
            return allowed
        if self.settings.expression_uploads and (owner or self.settings.public_expression_uploads):
            allowed.extend(('add_emoji', 'add_sticker'))
        if self.settings.images_enabled and (owner or self.settings.public_images):
            allowed.extend(('generate_image', 'post_image'))
        if self.settings.personal_colours and request.guild is not None:
            allowed.append('set_my_colour')
        if self.settings.search_enabled and (owner or self.settings.public_search):
            allowed.extend(('web_search', 'web_fetch', 'image_search'))
        allowed.append('set_timezone')
        if self.settings.reminders_enabled:
            allowed.extend(('set_reminder', 'list_reminders', 'cancel_reminder'))
        if owner:
            allowed.append('dashboard')
            if self.settings.host_status_enabled:
                allowed.append('host_status')
        return allowed

    def tool(self, request):
        allowed = self.allowed_actions(request)
        if not allowed:
            return None
        schema = FeatureAction.model_json_schema()
        schema['properties']['action']['enum'] = allowed
        return {'type': 'function', 'function': {
            'name': 'feature_tool', 'parameters': schema,
            'description': ('Search the web with Brave using query, limit and optional freshness (pd=day, pw=week, pm=month, py=year); '
                             'cite the returned source URLs. Public members may search without owner approval. '
                             'web_fetch opens a public url from a search result or the speaker, returning page text, links and image URLs. '
                             'Read relevant pages before reporting their contents; use next_offset as offset to continue long pages, with optional max_chars. '
                             'image_search uses query and limit to find existing pictures across websites, returning image_url and source_url. '
                             'When asked to find or show existing pictures, use image_search or web_fetch, then post_image with image_url and '
                             'the original source_url to upload each chosen picture here. post_image accepts direct PNG, JPEG, WebP and GIF URLs, '
                             'posts the actual file with its source, and reports confirmed delivery. Use generate_image for requests to create new artwork. '
                             'lookup_members resolves real usernames, global names and server nicknames (query); ping_member uses member name/ID '
                             'to send a real requested notification here. set_my_colour sets ONLY the speaker’s personal role to colour #RRGGBB. '
                             'generate_image creates requested new artwork; images attached to or linked in the request, and readable images '
                             'in the replied-to message, are sent to the provider automatically as source references. '
                             + (f'Every generated picture automatically gets the house art style ({self.settings.image_style}); write the prompt as content only and '
                                'never name an art style, medium or rendering technique in it. Only when the current speaker explicitly asks for a '
                                'different art style, put their requested style in art_style, and still keep the prompt content only. '
                                if self.settings.image_style else 'Put any art style the speaker asks for in art_style. ') +
                             ('For a picture of known people or characters, list every depicted person in subjects using Discord IDs or exact character names. '
                             'Include yourself or the speaker only when depicted. Use recall with member=name/ID for a complete appearance lookup. '
                             'The picture tool retrieves and reconciles their complete saved appearance records before generation. '
                             'Describe the requested scene, pose and temporary outfit changes accurately in prompt. '
                             'Write the image prompt as positive description of what appears: subject, appearance, pose, expression, setting, '
                             'lighting, colours and composition. Turn every exclusion into its positive equivalent, such as "an empty street" '
                             'for no people or "a plain pale backdrop" for no clutter; keep "no"/"without" only when the speaker demands an exclusion '
                             'that has no positive wording. '
                             'remember saves a fact for the current person across all channels and bot accounts; set shared=true for lasting appearance or trait '
                             'facts about any server member, including bot accounts, so every conversation can use them. '
                             'Write concrete facts with the subject’s name and ID. Use memory_id to update your obsolete fact in place. '
                             'recall and forget work across channels; forget_all clears the speaker’s personal facts across channels. '
                             'forget permanently deletes the memory record and clears recalled copies from saved conversation. '
                             'Members can delete their own records and shared facts about themselves. The owner can delete any memory by ID. '
                             'shared=true with forget_all deletes shared records saved by the speaker, or ALL shared records for the owner; '
                             'use it only for an explicit request to clear that scope. Owner-only actions check host health or DM a dashboard link. '
                             'Schedule a clear future reminder with due_at (ISO date/time), reminder and delivery here/dm; '
                              'cancel by reminder_id, list this person’s reminders, or set their IANA timezone. '
                              'list_expressions finds usable server emojis and stickers. react uses expression (emoji/name/ID) and an optional '
                              'same-channel message_id; send_sticker uses its exact name or ID. add_emoji/add_sticker upload a requested '
                              'visible or user-linked image_url under name; stickers also accept description and a related Unicode emoji. '
                             'Use only the actions offered here. A picture is attached automatically; never invent a URL.')),
        }}

    def validate(self, arguments, request):
        args = FeatureAction.model_validate_json(arguments)
        if args.action not in self.allowed_actions(request):
            raise PermissionError('This action is disabled or unavailable to this speaker.')
        if (args.action == 'remember' and not self.settings.auto_memory
                and not re.search(r'\b(remember|save (?:this|that)|keep (?:this|that) in mind)\b', request.content, re.I)):
            raise PermissionError('Automatic memory is disabled; the speaker must explicitly ask you to remember.')
        return args

    def clean_error(self, value):
        text = ('; '.join(error['msg'].removeprefix('Value error, ') for error in value.errors(include_input=False, include_url=False))
                 if isinstance(value, ValidationError) else str(value))
        return self.redact(text)[:700]

    def redact(self, text):
        secrets = []
        for name, secret in self.credentials.items():
            if 'key' in name or 'token' in name:
                secrets.extend(item for item in (secret if isinstance(secret, list) else [secret]) if isinstance(item, str) and item)
            if name.startswith('provider_headers:') and isinstance(secret, dict):
                for header, value in secret.items():
                    if value and header.lower() not in {'accept', 'content-type', 'anthropic-version', 'user-agent', 'http-referer', 'x-title'}:
                        secrets.append(value)
        def clean(value):
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, '[redacted]')
                return redact_private_links(value)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {clean(key): clean(item) for key, item in value.items()}
            return value
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return clean(text)
        if isinstance(value, (list, dict)):
            safe = clean(value)
            return json.dumps(safe) if safe != value else text
        return clean(text)

    def picture_prompt(self, request_id):
        row = self.db.execute("SELECT prompt FROM pictures WHERE request_id=? AND state='complete'", (str(request_id),)).fetchone()
        return row[0] if row and row[0] else None

    def record_status(self, name, result):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)',
                        ('status:' + name, json.dumps({'checked_at': utc_now(), **result})))
        self.db.commit()

    def memories(self, author_id, channel_id, query='', limit=50):
        limit = max(1, min(int(limit), 1000))
        tokens = [word for word in re.findall(r'\w+', (query or '').casefold()) if len(word) >= 2]
        if not tokens:
            rows = self.memories_db.execute(
                "SELECT id, fact FROM memories WHERE author_id=? AND channel_id<>'' AND instr(lower(fact), lower(?)) > 0 "
                'ORDER BY updated_at DESC, id DESC LIMIT ?', (str(author_id), query, limit)).fetchall()
            result = [{'id': row[0], 'fact': row[1], 'scope': 'personal'} for row in rows]
            shared = self.memories_db.execute(
                'SELECT id, fact FROM memories WHERE channel_id=? AND instr(lower(fact), lower(?)) > 0 '
                'ORDER BY updated_at DESC, id DESC LIMIT ?', (SHARED_SCOPE, query, limit)).fetchall()
            result.extend({'id': row[0], 'fact': row[1], 'scope': 'server'} for row in shared)
            return result
        def scored(select, params):
            clause = ' OR '.join(['instr(lower(fact), ?) > 0 OR instr(lower(keywords), ?) > 0'] * len(tokens))
            sql = select + ' AND (' + clause + ') ORDER BY updated_at DESC, id DESC LIMIT ?'
            bound = []
            for token in tokens:
                bound.extend([token, token])
            rows = self.memories_db.execute(sql, (*params, *bound, limit * 5)).fetchall()
            ranked = []
            for row_id, fact, keywords, updated in rows:
                haystack = f'{fact}\n{keywords}'.casefold()
                hits = sum(1 for token in tokens if token in haystack)
                if hits:
                    ranked.append((hits, updated, row_id, fact))
            ranked.sort(key=lambda item: (-item[0], -item[1], -item[2]))
            return ranked[:limit]
        personal = scored(
            "SELECT id, fact, keywords, updated_at FROM memories WHERE author_id=? AND channel_id<>''",
            (str(author_id),))
        result = [{'id': row_id, 'fact': fact, 'scope': 'personal'} for _, _, row_id, fact in personal]
        shared = scored(
            'SELECT id, fact, keywords, updated_at FROM memories WHERE channel_id=?',
            (SHARED_SCOPE,))
        result.extend({'id': row_id, 'fact': fact, 'scope': 'server'} for _, _, row_id, fact in shared)
        return result

    def memory_count(self, author_id, channel_id):
        own = self.memories_db.execute("SELECT count(*) FROM memories WHERE author_id=? AND channel_id<>''", (str(author_id),)).fetchone()[0]
        shared = self.memories_db.execute('SELECT count(*) FROM memories WHERE channel_id=?', (SHARED_SCOPE,)).fetchone()[0]
        return {'personal': own, 'server': shared}

    def activated_memories(self, author_id, channel_id, text, budget=4000, *, focus='', people=()):
        # ponytail: full scan of all scoped memories per message; switch to an FTS5 table if the
        # memory table ever reaches tens of thousands of rows.
        if budget <= 0:
            return []
        rows = self.memories_db.execute(
            'SELECT id, fact, keywords, updated_at, channel_id FROM memories WHERE (author_id=? OR channel_id=?) '
            'ORDER BY updated_at DESC, id DESC', (str(author_id), SHARED_SCOPE)).fetchall()
        scored = []
        for row in rows:
            hits = keyword_hits(row[2], text or '')
            current_hits = keyword_hits(row[2], focus or '')
            subject = any(about_person(row[1], person) for person in people)
            personal = row[4] != SHARED_SCOPE
            # Speaker facts and known identities must survive messages such as 'draw me' or 'hello'.
            baseline = bool(people) and (personal or subject)
            identity = baseline and bool(IDENTITY_FACT.search(row[1]))
            if hits or current_hits or baseline:
                tier = 0 if identity else 1 if current_hits else 2 if baseline else 3
                scored.append((tier, current_hits, hits, row[3], row[0], row[1]))
        scored.sort(key=lambda item: (item[0], -item[1], -item[3], -item[4], -item[2]))
        result, used = [], 2
        for _, _, _, updated, row_id, fact in scored:
            entry = {'id': row_id, 'fact': fact, 'updated_at': updated}
            size = len(json.dumps(entry, ensure_ascii=False)) + (2 if result else 0)
            if used + size > budget:
                continue
            used += size
            result.append(entry)
        return result

    def remember(self, author_id, channel_id, fact, memory_id=None, keywords=None):
        fact = (fact or '').strip()
        if not 1 <= len(fact) <= 400:
            raise ValueError('A memory needs between 1 and 400 characters.')
        if (self.clean_error(fact) != fact or re.search(
                r'\b(password|api[ _-]?key|access[ _-]?token|secret|bearer)\s*(?::|=|\bis\b)', fact, re.I)):
            raise ValueError('Do not save credentials as memories.')
        selected = clean_keywords(keywords)
        if not selected:
            selected = memory_keywords(fact)
        scope = (str(author_id), str(channel_id))
        # Nonblank channel IDs record where a personal fact was learned, not where it can be recalled.
        where = "author_id=? AND channel_id " + ("= ''" if scope[1] == SHARED_SCOPE else "<> ''")
        now = int(time.time())
        if memory_id is not None:
            update_where = 'author_id=?' if scope[1] == SHARED_SCOPE else where
            cursor = self.memories_db.execute('UPDATE memories SET fact=?, keywords=?, updated_at=?'
                                               + (",channel_id=''" if scope[1] == SHARED_SCOPE else '')
                                               + ' WHERE id=? AND ' + update_where,
                                               (fact, selected, now, memory_id, scope[0]))
            if not cursor.rowcount:
                raise ValueError('That memory does not belong to you in this scope.')
        else:
            existing = self.memories_db.execute('SELECT id FROM memories WHERE ' + where + ' AND fact=?', (scope[0], fact)).fetchone()
            if existing:
                return {'memory_id': existing[0], 'fact': fact, 'already_saved': True}
            cursor = self.memories_db.execute('INSERT INTO memories (author_id, channel_id, fact, keywords, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                                              (*scope, fact, selected, now, now))
            memory_id = cursor.lastrowid
        self.memories_db.commit()
        return {'memory_id': memory_id, 'fact': fact, 'keywords': selected}

    def forget(self, author_id, channel_id, memory_id=None, *, forget_all=False, owner=False, person=None):
        author = str(author_id)
        if not forget_all and memory_id is None:
            raise ValueError('Choose a memory ID, or explicitly request forgetting all your personal memories across channels.')
        if forget_all:
            where = "channel_id=''" if str(channel_id) == SHARED_SCOPE else "channel_id<>''"
            values = []
            if not (owner and str(channel_id) == SHARED_SCOPE):
                where += ' AND author_id=?'
                values.append(author)
            rows = self.memories_db.execute('SELECT id,author_id,channel_id,fact FROM memories WHERE ' + where, values).fetchall()
        else:
            rows = self.memories_db.execute('SELECT id,author_id,channel_id,fact FROM memories WHERE id=?', (memory_id,)).fetchall()
            rows = [row for row in rows if owner or (row[1] == author and (row[2] == '') == (str(channel_id) == ''))
                    or (person and row[2] == '' and about_person(row[3], person))]
        if not rows and not forget_all:
            raise ValueError('That memory does not belong to you in this scope.')
        self.memories_db.executemany('DELETE FROM memories WHERE id=?', [(row[0],) for row in rows])
        self.memories_db.commit()
        authors = {author, *(row[1] for row in rows)}
        shared = any(row[2] == SHARED_SCOPE for row in rows)
        if getattr(self.bot, 'memory', None):
            for identity in authors:
                self.bot.memory.cancel(identity)
            if shared:
                # Another participant may have supplied the same shared fact to an in-flight capture.
                self.bot.memory.db.execute("UPDATE memory_jobs SET state='cancelled',source='' WHERE state IN ('pending','running')")
                self.bot.memory.db.commit()
        facts = [row[3] for row in rows]
        def contains_deleted(encoded):
            if isinstance(encoded, dict):
                return any(contains_deleted(value) for value in encoded.values())
            if isinstance(encoded, list):
                return any(contains_deleted(value) for value in encoded)
            if not isinstance(encoded, str):
                return False
            if any(fact in encoded for fact in facts):
                return True
            try:
                decoded = json.loads(encoded)
            except ValueError:
                return False
            return decoded != encoded and contains_deleted(decoded)
        # Clear copies recalled by other people as well as the saver. Preserve unrelated conversations.
        for peer in self.memory_peers:
            peer.memory_epoch += 1
            for identity, saved_author, encoded in peer.db.execute("SELECT request_id,author_id,messages FROM turns WHERE messages<>'[]'").fetchall():
                if saved_author in authors or contains_deleted(encoded):
                    peer.db.execute("UPDATE turns SET messages='[]' WHERE request_id=?", (identity,))
            for identity, fingerprint, arguments, result in peer.db.execute('SELECT request_id,fingerprint,arguments,result FROM actions').fetchall():
                if json.loads(arguments).get('action') in {'remember', 'recall'} and (contains_deleted(arguments) or contains_deleted(result)):
                    peer.db.execute('UPDATE actions SET arguments=?,result=? WHERE request_id=? AND fingerprint=?',
                                    ('{"action":"forgotten"}', '{"ok":true,"result":{"forgotten":true}}', identity, fingerprint))
            for name, value in peer.db.execute("SELECT name,value FROM settings WHERE name LIKE 'status:%'").fetchall():
                if contains_deleted(value):
                    peer.db.execute('DELETE FROM settings WHERE name=?', (name,))
            peer.db.commit()
        return {'forgotten': len(rows), 'recent_context_cleared': True}

    async def perform(self, args, request):
        # Recheck settings and real Discord identity immediately before performing any feature.
        self.validate(args.model_dump_json(), request)
        if args.action == 'lookup_members':
            return await self.bot.people.lookup(request, args.query or args.member or '', args.limit)
        if args.action == 'ping_member':
            return await self.bot.people.ping(request, args.member)
        if args.action == 'set_my_colour':
            return await self.bot.people.colour(request, args.colour)
        if args.action == 'list_expressions':
            return self.bot.expressions.catalog(request, args.query, args.limit)
        if args.action in {'react', 'send_sticker'}:
            return await self.bot.expressions.use(request, args.action, args.expression, message_id=args.message_id)
        if args.action in {'add_emoji', 'add_sticker'}:
            return await self.bot.expressions.upload(request, args)
        if args.action == 'generate_image':
            return await self.generate_image(args, request)
        if args.action == 'web_search':
            return await self.web_search(args.query, args.limit, args.freshness)
        if args.action == 'image_search':
            return await self.web_search(args.query, args.limit, images=True)
        if args.action == 'web_fetch':
            resource = await fetch_resource(args.url, check=self.check_public_link)
            return page_content(resource, offset=args.offset or 0, max_chars=args.max_chars or 12000)
        if args.action == 'post_image':
            return await self.post_image(args, request)
        if args.action == 'host_status':
            return await host_status(self.settings.host_services)
        if args.action == 'dashboard':
            if self.bot.dashboard is None:
                raise ValueError('The dashboard is not running yet.')
            await self.bot.dashboard.send_link(request.author)
            return {'sent_by_dm': True, 'expires_in_minutes': 10}
        if args.action == 'remember':
            shared = args.shared or (request.guild is not None and public_appearance(args.fact or '')
                                     and not re.search(r'\b(?:private|personally|only (?:me|my|this))\b', request.content, re.I))
            if args.memory_id:
                original = self.memories_db.execute('SELECT channel_id FROM memories WHERE id=? AND author_id=?',
                                                   (args.memory_id, str(request.author.id))).fetchone()
                shared = shared or bool(original and original[0] == SHARED_SCOPE)
            return self.remember(request.author.id, SHARED_SCOPE if shared else request.channel.id,
                                 args.fact, args.memory_id)
        if args.action == 'recall':
            result = {'memories': self.memories(request.author.id, request.channel.id, args.query),
                      'total': self.memory_count(request.author.id, request.channel.id)}
            if args.member:
                catalog, _ = await request_appearances(self, request)
                selector = str(request.author.id) if args.member.casefold() in {'me', 'myself'} else (
                    str(self.bot.bot_id) if args.member.casefold() in {'you', 'yourself'} else args.member)
                result['appearance'] = catalog.get(catalog.resolve(selector))
            return result
        if args.action == 'forget':
            options = {'owner': self.owner_request(request), 'person': profile(request.author)}
            if not args.forget_all and args.memory_id is not None:
                try:
                    return self.forget(request.author.id, request.channel.id, args.memory_id, **options)
                except ValueError:
                    return self.forget(request.author.id, SHARED_SCOPE, args.memory_id, **options)
            return self.forget(request.author.id, SHARED_SCOPE if args.shared else request.channel.id,
                               args.memory_id, forget_all=args.forget_all, **options)
        if args.action == 'set_reminder':
            return self.bot.social.add_reminder(request, args.due_at, args.reminder, dm=args.delivery == 'dm')
        if args.action == 'list_reminders':
            return {'reminders': self.bot.social.reminder_list(request.author.id, request.channel.id,
                                  include_dm=request.guild is None and self.owner_request(request))}
        if args.action == 'cancel_reminder':
            if args.reminder_id is None:
                raise ValueError('Choose the reminder ID to cancel.')
            return self.bot.social.cancel_reminder(request.author.id, args.reminder_id)
        if args.action == 'set_timezone':
            return {'timezone': self.bot.social.set_zone(request.author.id, args.timezone)}
        raise ValueError('Unknown feature.')

    async def image_references(self, args, request, spec):
        candidates = message_images(request, 'Current message')
        replied = await resolved_reply(request)
        if replied is not None:
            candidates.extend(message_images(replied, 'Referenced message'))
        seen = {item['url'] for item in candidates}
        if getattr(args, 'image_url', None):
            url = args.image_url.rstrip('.,!?')
            text = request.content + ('\n' + replied.content if replied is not None else '')
            found = {value.rstrip('.,!?') for value in re.findall(r'https://[^\s<>\[\]()"\']+', text)}
            if image_url(url) is None or self.redact(url) != url or (url not in seen and url not in found):
                raise FeatureError('Choose an image attached, linked or visibly referenced in this request.')
            candidates.append({'url': url, 'label': 'Requested image'})
        urls = list(dict.fromkeys(item['url'] for item in candidates))[:4]
        if urls and spec.family == 'openai_images' and not spec.supports_edits:
            raise FeatureError(f'{spec.name} cannot use reference images; no picture was generated.')
        references = []
        for url in urls:
            try:
                raw = await download_image(url, maximum=10 * 1024 * 1024)
                extension(raw)
            except (FeatureError, ValueError):
                continue
            references.append(raw)
        if urls and not references:
            raise FeatureError('The source images in this request could not be read, so no picture was generated.')
        return references

    async def generate_image(self, args, request):
        self.require_assistant()
        spec, key = selected(self, getattr(args, 'provider', None))
        if not args.prompt or not args.prompt.strip():
            raise ValueError('A picture prompt is required.')
        if self.image_lock.locked():
            raise FeatureError('A picture is already being generated.', retry_after=30)
        async with self.image_lock:
            identity, author = str(request.id), str(request.author.id)
            for peer in self.image_peers:
                previous = peer.db.execute('SELECT state FROM pictures WHERE request_id=?', (identity,)).fetchone()
                if previous and not (peer is self and previous[0] == 'retryable'):
                    raise ValueError('One picture request has already been attempted for this message; it will not be repeated.')
            now = int(time.time())
            art_style = getattr(args, 'art_style', None)
            references = await self.image_references(args, request, spec)
            prompt = await grounded_prompt(self, args, request)
            self.db.execute('INSERT OR REPLACE INTO pictures (request_id, author_id, channel_id, created_at, filename, state, prompt) '
                            'VALUES (?, ?, ?, ?, NULL, ?, ?)', (identity, author, str(request.channel.id), now, 'pending', styled(prompt, art_style, self.settings.image_style)))
            self.db.commit()
            try:
                attempts = []
                for provider_id in dict.fromkeys([spec.id, *self.settings.image_fallback_providers]):
                    candidate, candidate_key = selected(self, provider_id)
                    keys = list(dict.fromkeys([candidate_key, *self.credentials.get('image_fallback_keys', [])]))[:4] if candidate.id in BUILTIN_IMAGE_MODELS else [candidate_key]
                    attempts.extend((candidate, value) for value in keys)
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=spec.timeout)) as session:
                    rejected_provider = None
                    for index, (spec, attempt_key) in enumerate(attempts):
                        if spec.id == rejected_provider:
                            continue
                        payload = image_request(spec, prompt, args.size, references, art_style, self.settings.image_style)
                        url, headers = request_target(spec, attempt_key, edit=bool(references))
                        body_args = {'data': payload} if isinstance(payload, aiohttp.FormData) else {'json': payload}
                        async with session.post(url, **body_args, headers=headers, allow_redirects=False,
                                                timeout=aiohttp.ClientTimeout(total=spec.timeout)) as response:
                            body = await bounded_body(response, 40 * 1024 * 1024)
                            try:
                                data = json.loads(body)
                            except (ValueError, UnicodeError):
                                data = {}
                            status, reason = response.status, response.reason
                        error = data.get('error') or data.get('message') or reason if isinstance(data, dict) else reason
                        if isinstance(error, dict):
                            error = error.get('message') or error.get('code') or 'Request rejected'
                        # Only a definite daily-quota refusal permits another paid-provider key.
                        daily_quota = status == 429 and bool(re.search(r'\bdaily\b.*\b(quota|limit)\b.*\b(exceeded|exhausted|reached)\b', str(error), re.I))
                        unavailable_keys = status == 503 and str(error).strip().casefold() == 'no valid openai api keys available for this model'
                        if status in {401, 403, 404, 429} or unavailable_keys:
                            # Definite refusals can move to another connection. Unknown outcomes cannot.
                            remaining = attempts[index + 1:]
                            if daily_quota and remaining:
                                continue
                            if any(candidate.id != spec.id for candidate, _ in remaining):
                                rejected_provider = spec.id
                                continue
                        if status != 200:
                            raise FeatureError(self.clean_error(f'{spec.name} HTTP {status}: {error}'), uncertain=status >= 500 and not unavailable_keys,
                                               retry_after=(3600 if daily_quota else 60) if status == 429 else None)
                        break
                    if not isinstance(data, dict):
                        raise FeatureError(f'{spec.name} returned an unexpected response; the paid request will not be repeated.', uncertain=True)
                    if spec.family == 'gemini':
                        picture, explanation = gemini_picture(data)
                        if not picture:
                            raise FeatureError(self.clean_error(explanation or f'{spec.name} returned no picture.'))
                        output = []
                    else:
                        output = data.get('output' if spec.family == 'responses' else 'data', [])
                    if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
                        raise FeatureError(f'{spec.name} returned an unexpected response; the paid request will not be repeated.', uncertain=True)
                    if spec.family != 'gemini':
                        picture = next((item.get('result') for item in output
                                        if item.get('type') == 'image_generation_call' and item.get('result')), None)
                    image_url = None
                    if spec.family == 'openai_images' and output:
                        picture, image_url = output[0].get('b64_json'), output[0].get('url')
                    if not picture and not image_url:
                        explanation = data.get('error') or f'{spec.name} returned no picture.'
                        for item in output:
                            content = item.get('content', [])
                            for part in content if isinstance(content, list) else []:
                                if not isinstance(part, dict):
                                    continue
                                if part.get('refusal') or part.get('text'):
                                    explanation = part.get('refusal') or part['text']
                        raise FeatureError(self.clean_error(explanation))
                try:
                    image = await download_image(image_url) if image_url and not picture else base64.b64decode(picture, validate=True)
                    suffix = extension(image)
                except (ValueError, TypeError):
                    raise FeatureError(f'{spec.name} returned unreadable image data; the paid request will not be repeated.', uncertain=True) from None
                directory = self.data_dir / 'pictures'
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                filename = f'picture-{identity}.{suffix}'
                with (directory / filename).open('xb') as handle:
                    handle.write(image)
                    handle.flush()
                    os.fsync(handle.fileno())
                self.db.execute("UPDATE pictures SET filename=?, state='complete' WHERE request_id=?", (filename, identity))
                self.db.commit()
                return {'filename': filename, 'bytes': len(image), 'model': spec.model,
                        'delivery': 'The actual image will be attached automatically to your reply.'}
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                self.db.execute("UPDATE pictures SET state='uncertain' WHERE request_id=?", (identity,))
                self.db.commit()
                raise FeatureError(f'{spec.name} connection ended ({type(exc).__name__}); the outcome is unconfirmed and will not be retried.', uncertain=True) from None
            except FeatureError as exc:
                self.db.execute('UPDATE pictures SET state=? WHERE request_id=?',
                                ('uncertain' if exc.uncertain else 'retryable' if exc.retry_after else 'rejected', identity))
                self.db.commit()
                raise

    def check_public_link(self, url):
        if self.redact(url) != url:
            raise ValueError('Public web requests cannot contain saved credentials or private sign-in links.')

    async def post_image(self, args, request):
        self.require_assistant()
        source = public_url(args.source_url or args.image_url)
        self.check_public_link(source)
        # Keep the complete source link in Discord's 2000-character message budget.
        if len(source) > 1900:
            raise ValueError('Use a source page URL shorter than 1900 characters.')
        maximum = min(IMAGE_BYTES, getattr(request.guild, 'filesize_limit', 10 * 1024 * 1024))
        resource = await fetch_resource(args.image_url, maximum=maximum, check=self.check_public_link,
                                        referer=source if args.source_url else None)
        suffix = await asyncio.to_thread(picture_extension, resource['body'])
        self.validate(args.model_dump_json(), request)
        import discord
        attachment = discord.File(io.BytesIO(resource['body']), filename=f'web-image.{suffix}')
        try:
            message = await request.channel.send(f'Source: <{source}>', file=attachment,
                reference=request.to_reference(fail_if_not_exists=False), allowed_mentions=discord.AllowedMentions.none())
        finally:
            attachment.close()
        return {'posted': True, 'message_id': str(message.id), 'image_url': resource['url'], 'source_url': source,
                'bytes': len(resource['body']), 'delivery': 'The original image has been posted in this conversation with its source link.'}

    async def web_search(self, query, limit=10, freshness=None, *, images=False):
        self.require_assistant()
        key = self.credentials.get('brave_api_key')
        if not key:
            raise FeatureError('Brave Search is not connected. The owner can add its key in Connections.')
        query = query.strip()
        if not query or len(query) > 400 or len(query.split()) > 50:
            raise FeatureError('A search needs between 1 and 400 characters, with at most 50 words.')
        if not 1 <= limit <= 20 or freshness not in {None, 'pd', 'pw', 'pm', 'py'}:
            raise FeatureError('Use 1 to 20 results and a supported freshness filter.')
        if self.clean_error(query) != query:
            raise FeatureError('Search queries cannot contain saved credentials or private sign-in links.')
        cache_key = (query, limit, freshness, images)
        cached = self.search_cache.get(cache_key)
        if cached and time.time() - cached[0] < 300:
            return {**cached[1], 'cached': True}
        params = {'q': query, 'count': limit}
        if not images:
            params.update(result_filter='web', text_decorations='false')
        if freshness:
            params['freshness'] = freshness
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
                async with session.get(BRAVE_IMAGES_URL if images else BRAVE_URL, params=params,
                                       headers={'Accept': 'application/json', 'X-Subscription-Token': key},
                                       allow_redirects=False) as response:
                    try:
                        data = json.loads(await bounded_body(response, 2 * 1024 * 1024))
                    except (ValueError, UnicodeError):
                        data = None
                    if response.status != 200:
                        error = data.get('error', {}) if isinstance(data, dict) else {}
                        if isinstance(error, dict):
                            error = error.get('detail') or error.get('message') or error.get('code')
                        raise FeatureError(self.clean_error(f'Brave Search HTTP {response.status}: {error or response.reason}'),
                                           retry_after=60 if response.status == 429 or response.status >= 500 else None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise FeatureError(f'Brave Search could not be reached ({type(exc).__name__}).', retry_after=60) from None
        results = parse_image_search(data) if images else parse_search(data)
        result = {'source': 'Brave Image Search' if images else 'Brave Search', 'query': query, 'fetched_at': utc_now(),
                  'results': results[:limit], 'cached': False}
        # ponytail: a small in-process cache; successful search excerpts expire after five minutes.
        if len(self.search_cache) >= 32:
            self.search_cache.pop(next(iter(self.search_cache)))
        self.search_cache[cache_key] = (time.time(), result)
        return result


async def bounded_body(response, maximum):
    body = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > maximum:
            raise FeatureError('The provider response exceeded the supported size; it will not be retried.', uncertain=True)
    return bytes(body)


def parse_image_search(data):
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise FeatureError('Brave Image Search returned an unreadable result.')
    results = []
    for item in data['results'][:100]:
        if not isinstance(item, dict):
            continue
        properties = item.get('properties')
        if not isinstance(properties, dict):
            continue
        try:
            image = public_url(properties.get('url'))
            source = public_url(item.get('url'))
        except ValueError:
            continue
        results.append({'title': html.unescape(re.sub(r'<[^>]*>', '', str(item.get('title') or '')))[:200],
                        'image_url': image, 'source_url': source})
    if data['results'] and not results:
        raise FeatureError('Brave Image Search returned no usable image links.')
    return results


def parse_search(data):
    if not isinstance(data, dict) or not isinstance(data.get('query'), dict):
        raise FeatureError('Brave Search returned an unreadable result. No sources could be verified.')
    web = data.get('web', {'results': []})
    if not isinstance(web, dict) or not isinstance(web.get('results'), list):
        raise FeatureError('Brave Search returned an unreadable result. No sources could be verified.')
    results = []
    for item in web['results'][:20]:
        if not isinstance(item, dict):
            continue
        title, url = item.get('title'), item.get('url')
        if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or len(url) > 2048:
            continue
        try:
            address = urlsplit(url)
            if address.scheme not in {'http', 'https'} or not address.hostname or address.username or address.password:
                continue
        except ValueError:
            continue
        snippet = item.get('description', '')
        results.append({'title': html.unescape(re.sub(r'<[^>]*>', '', title))[:200], 'url': url,
                        'snippet': html.unescape(re.sub(r'<[^>]*>', '', snippet))[:1000] if isinstance(snippet, str) else ''})
    if web['results'] and not results:
        raise FeatureError('Brave Search returned no usable source links. No sources could be verified.')
    return results


async def host_status(services=()):
    memory = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()}
    disk = shutil.disk_usage('/')
    interfaces = []
    for interface in Path('/sys/class/net').iterdir():
        if interface.name == 'lo':
            continue
        try:
            interfaces.append({'name': interface.name, 'state': (interface / 'operstate').read_text().strip(),
                               'received_bytes': int((interface / 'statistics/rx_bytes').read_text()),
                               'sent_bytes': int((interface / 'statistics/tx_bytes').read_text())})
        except OSError:
            continue
    base = {'checked_at': utc_now(), 'host': socket.gethostname(), 'cpu_count': os.cpu_count(),
            'load_1_5_15_minutes': list(os.getloadavg()), 'uptime_seconds': int(float(Path('/proc/uptime').read_text().split()[0])),
            'memory_total_bytes': memory['MemTotal'], 'memory_available_bytes': memory['MemAvailable'],
            'disk_total_bytes': disk.total, 'disk_free_bytes': disk.free, 'interfaces': interfaces}
    if not services:
        return {**base, 'services': [], 'note': 'No services are configured for checking. The owner can list systemd units in Tools.'}
    if not shutil.which('systemctl'):
        return {**base, 'services': [], 'note': 'This host has no systemctl, so service status cannot be checked.'}
    process = await asyncio.create_subprocess_exec(
        'systemctl', 'show', *services, '--no-pager', '--property=Id,LoadState,ActiveState,SubState,NRestarts,MemoryCurrent',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=8)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise FeatureError('The machine metrics were readable, but service status timed out.') from None
    status = [dict(line.split('=', 1) for line in group.splitlines() if '=' in line)
              for group in stdout.decode().strip().split('\n\n') if group.strip()]
    if not status or any(not {'Id', 'LoadState', 'ActiveState', 'SubState'} <= service.keys() for service in status):
        raise FeatureError('The machine metrics were readable, but service status could not be verified.')
    return {**base, 'services': status}
