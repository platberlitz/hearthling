"""Server expressions with bounded uploads and replay-safe native Discord effects."""
import asyncio
import hashlib
import io
import json
import re
import time
import unicodedata
import warnings
from types import SimpleNamespace

import discord
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

from conversation import message_content
from images import download_image


class ExpressionError(ValueError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def prepare_image(raw, kind):
    """Decode before uploading; preserve bounded animation and fit Discord's canvas."""
    if not raw or len(raw) > 8 * 1024 * 1024 or kind not in {'add_emoji', 'add_sticker'}:
        raise ValueError('Choose an image smaller than 8 MiB for an emoji or sticker.')
    side, maximum = (128, 256 * 1024) if kind == 'add_emoji' else (320, 512 * 1024)
    try:
        with warnings.catch_warnings(action='error', category=Image.DecompressionBombWarning), Image.open(io.BytesIO(raw)) as source:
            if source.format not in {'PNG', 'JPEG', 'GIF', 'WEBP'}:
                raise ValueError('Use a PNG, JPEG, GIF or WebP image.')
            frames_count = getattr(source, 'n_frames', 1)
            if frames_count > 100 or source.width * source.height * frames_count > 32_000_000:
                raise ValueError('Use a smaller image or an animation with at most 100 frames and 32 million decoded pixels.')
            frames, durations = [], []
            for frame in ImageSequence.Iterator(source):
                image = ImageOps.exif_transpose(frame.copy()).convert('RGBA')
                image.thumbnail((side, side), Image.Resampling.LANCZOS)
                canvas = Image.new('RGBA', (side, side))
                canvas.alpha_composite(image, ((side - image.width) // 2, (side - image.height) // 2))
                frames.append(canvas)
                durations.append(max(20, int(frame.info.get('duration', 100))))
            if kind == 'add_sticker' and len(frames) > 1 and sum(durations) > 5000:
                raise ValueError('Animated stickers must last no longer than five seconds.')
            output = io.BytesIO()
            animated = len(frames) > 1
            fmt = 'GIF' if animated and kind == 'add_emoji' else 'PNG'
            options = {'save_all': True, 'append_images': frames[1:], 'duration': durations, 'loop': 0} if animated else {}
            if fmt == 'GIF':
                options['disposal'] = 2
            frames[0].save(output, format=fmt, optimize=True, **options)
            data = output.getvalue()
            if len(data) > maximum:
                raise ValueError(f'The resized image still exceeds Discord’s {maximum // 1024} KiB limit. Use a simpler or shorter image.')
            return data, 'gif' if fmt == 'GIF' else 'png', animated
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('The image could not be decoded safely. Use a smaller PNG, JPEG, GIF or WebP.') from None


class Expressions:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db
        self.upload_lock = asyncio.Lock()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS expression_uploads (
                request_id TEXT NOT NULL, guild_id TEXT NOT NULL, author_id TEXT NOT NULL,
                action TEXT NOT NULL, name TEXT NOT NULL, digest TEXT NOT NULL, created_at INTEGER NOT NULL,
                result TEXT, PRIMARY KEY(request_id,action,name,digest)
            );
            CREATE TABLE IF NOT EXISTS expression_effects (
                request_id TEXT NOT NULL, action TEXT NOT NULL, expression TEXT NOT NULL, target TEXT NOT NULL,
                result TEXT, PRIMARY KEY(request_id,action,expression,target)
            );
        ''')

    def guild(self):
        guild = self.bot.get_guild(self.bot.server_id)
        if guild is None:
            raise ValueError('This bot is not connected to its configured server.')
        return guild

    def allowed(self, request, *, automatic=False):
        if not self.bot.features.settings.expressions_enabled:
            return False
        if automatic and request.author.id == self.bot.bot_id and request.author.bot:
            guard = SimpleNamespace(author=SimpleNamespace(id=self.bot.features.owner_id, bot=False),
                                    channel=request.channel, guild=request.guild)
            return self.bot.social.access(guard)
        return self.bot.social.response_allowed(request)

    def catalog(self, request=None, query='', limit=40):
        if request is not None and request.guild and request.guild.id != self.bot.server_id:
            raise PermissionError('Expressions are restricted to the configured server.')
        guild = self.bot.get_guild(self.bot.server_id)
        if guild is None:
            return {'emojis': [], 'stickers': [], 'connected': False}
        query = query.strip().casefold()
        emojis = [{'id': str(item.id), 'name': item.name, 'markup': f'<{"a" if item.animated else ""}:{item.name}:{item.id}>',
                   'url': str(item.url)} for item in getattr(guild, 'emojis', []) if item.is_usable() and (not query or query in item.name.casefold())]
        stickers = [{'id': str(item.id), 'name': item.name, 'description': item.description or '', 'url': str(item.url)}
                    for item in getattr(guild, 'stickers', []) if item.available and (not query or query in item.name.casefold())]
        return {'emojis': emojis[:limit], 'stickers': stickers[:limit], 'emoji_count': len(emojis), 'sticker_count': len(stickers)}

    async def resolve(self, value, *, sticker=False):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Choose an emoji or sticker from the server list.')
        value = value.strip()
        if not sticker and len(value) <= 32 and not any(char.isspace() or char in '<>@' for char in value):
            if any(ord(char) > 127 and (unicodedata.category(char).startswith('S') or char == '\u20e3') for char in value):
                return value
        guild = self.guild()
        match = re.fullmatch(r'<a?:[A-Za-z0-9_]+:([0-9]{15,21})>', value)
        identity = match[1] if match else value if re.fullmatch(r'[0-9]{15,21}', value) else None
        items = guild.stickers if sticker else guild.emojis
        found = [item for item in items if str(item.id) == identity] if identity else [item for item in items if item.name.casefold() == value.strip(':').casefold()]
        if not found and identity:
            found = [await (guild.fetch_sticker(int(identity)) if sticker else guild.fetch_emoji(int(identity)))]
        if len(found) != 1:
            raise ValueError('That expression is missing or its name is ambiguous. Choose its exact server ID.')
        item = found[0]
        if (not item.available if sticker else not item.is_usable()):
            raise ValueError('That server expression is unavailable to this bot.')
        return item

    async def target(self, request, identity=None, fallback=None):
        if request.guild:
            member = request.guild.get_member(request.author.id) if request.author.bot else request.author
            if member is None:
                raise ValueError('That bot is no longer a member of this server.')
            permissions = request.channel.permissions_for(member)
            if not permissions.view_channel or not permissions.read_message_history:
                raise PermissionError('The speaker cannot read messages in this channel.')
        if identity:
            return await request.channel.fetch_message(int(identity))
        if isinstance(request, discord.Message):
            return request
        if isinstance(fallback, discord.Message):
            return fallback
        if interaction := getattr(request, 'interaction', None):
            return await interaction.original_response()
        raise ValueError('Reply to the message you want me to react to, or give its ID in this channel.')

    async def use(self, request, action, value, *, message_id=None, fallback=None, channel=None, automatic=False):
        if action not in {'react', 'send_sticker'} or not self.allowed(request, automatic=automatic):
            raise PermissionError('Expressions are disabled or this conversation is unavailable.')
        channel = channel or request.channel
        if getattr(channel, 'guild', None) and channel.guild.id != self.bot.server_id:
            raise PermissionError('That channel is outside the configured server.')
        item = await self.resolve(value, sticker=action == 'send_sticker')
        if (action == 'send_sticker' and not isinstance(request, discord.Message) and fallback is None
                and (interaction := getattr(request, 'interaction', None))):
            fallback = await interaction.original_response()
        target = await self.target(request, message_id, fallback) if action == 'react' else channel
        expression = str(item.id) if not isinstance(item, str) else item
        key = (str(request.id), action, expression, str(target.id))
        previous = self.db.execute('SELECT result FROM expression_effects WHERE request_id=? AND action=? AND expression=? AND target=?', key).fetchone()
        if previous:
            result = json.loads(previous[0]) if previous[0] else {'ok': False, 'uncertain': True, 'error': 'The previous expression outcome is unconfirmed; it will not be repeated.'}
            if not result['ok']:
                raise ExpressionError(result['error'], uncertain=result.get('uncertain', False))
            return result['result']
        count = self.db.execute('SELECT count(*) FROM expression_effects WHERE request_id=? AND action=?', key[:2]).fetchone()[0]
        if count >= (2 if action == 'react' else 1):
            raise ValueError('Each reply can use at most two reactions and one sticker.')
        if not self.allowed(request, automatic=automatic):
            raise PermissionError('Expression delivery stopped after a pause or access change.')
        self.db.execute('INSERT INTO expression_effects VALUES (?,?,?,?,NULL)', key)
        self.db.commit()
        try:
            if action == 'react':
                await target.add_reaction(item)
                data = {'message_id': str(target.id), 'emoji': str(item)}
            else:
                options = {'allowed_mentions': discord.AllowedMentions.none()}
                reference = request if isinstance(request, discord.Message) else fallback
                if isinstance(reference, discord.Message) and reference.channel.id == channel.id:
                    options['reference'] = reference.to_reference(fail_if_not_exists=False)
                    options['mention_author'] = False
                message = await channel.send(stickers=[item], **options)
                data = {'message_id': str(message.id), 'sticker_id': str(item.id), 'name': item.name}
            result = {'ok': True, 'result': data}
        except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
            result = {'ok': False, 'error': self.bot.features.clean_error(exc),
                      'uncertain': not isinstance(exc, discord.HTTPException) or exc.status >= 500}
        self.db.execute('UPDATE expression_effects SET result=? WHERE request_id=? AND action=? AND expression=? AND target=?',
                        (json.dumps(result), *key))
        self.db.commit()
        self.bot.features.record_status(action, result)
        if not result['ok']:
            raise ExpressionError(result['error'], uncertain=result['uncertain'])
        return data

    def prepare(self, request, text):
        catalog = self.catalog(request, limit=1000) if self.bot.features.settings.expressions_enabled else {'emojis': []}
        by_id = {item['id']: item for item in catalog['emojis']}
        names = {}
        for item in catalog['emojis']:
            names.setdefault(item['name'].casefold(), []).append(item)
        effects, pieces = [], []
        for index, part in enumerate(re.split(r'(```.*?```|`[^`\n]*`)', text, flags=re.S)):
            if index % 2:
                pieces.append(part)
                continue
            def marker(match):
                action = 'react' if match[1].upper() == 'REACT' else 'send_sticker'
                if sum(item[0] == action for item in effects) < (2 if action == 'react' else 1):
                    effects.append((action, match[2].strip()))
                return ''
            part = re.sub(r'\[(REACT|STICKER):\s*([^\]\n]{1,100})\]', marker, part, flags=re.I)
            part = re.sub(r'<a?:([A-Za-z0-9_]+):([0-9]{15,21})>',
                          lambda match: by_id[match[2]]['markup'] if match[2] in by_id else ':' + match[1] + ':', part)
            part = re.sub(r'(?<![\w:<]):([A-Za-z0-9_]{2,32}):(?!:)',
                          lambda match: names[match[1].casefold()][0]['markup'] if len(names.get(match[1].casefold(), [])) == 1 else match[0], part)
            pieces.append(part)
        return ''.join(pieces).strip(), effects if self.bot.features.settings.expressions_enabled else []

    async def after(self, request, effects, *, sent_message=None, channel=None, automatic=False):
        channel = channel or request.channel
        errors = []
        for action, value in effects:
            try:
                await self.use(request, action, value, fallback=sent_message, channel=channel, automatic=automatic)
            except (ValueError, PermissionError, discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
                errors.append(self.bot.features.clean_error(exc))
        if errors and self.allowed(request, automatic=automatic):
            options = {'allowed_mentions': discord.AllowedMentions.none()}
            reference = request if isinstance(request, discord.Message) else sent_message
            if isinstance(reference, discord.Message) and reference.channel.id == channel.id:
                options['reference'] = reference.to_reference(fail_if_not_exists=False)
                options['mention_author'] = False
            await channel.send(self.bot.fallback('expression_failed', detail='; '.join(dict.fromkeys(errors))[:1500]), **options)

    async def upload(self, request, args):
        feature, config = self.bot.features, self.bot.features.settings
        feature.validate(args.model_dump_json(), request)
        feature.require_assistant()
        if not (re.search(r'\b(emojis?|emotes?|stickers?)\b', request.content, re.I) and
                re.search(r'\b(add|upload|create|make|save|copy|import|steal|yoink|turn|convert)\b', request.content, re.I)):
            raise ValueError('Explicitly ask to add an emoji or sticker and supply the image you want to use.')
        name = (args.name or '').strip()
        if (args.action == 'add_emoji' and not re.fullmatch(r'[A-Za-z0-9_]{2,32}', name)) or (
                args.action == 'add_sticker' and (not 2 <= len(name) <= 30 or any(ord(char) < 32 or char in '<>@' for char in name))):
            raise ValueError('Emoji names need 2 to 32 letters, digits or underscores; sticker names need 2 to 30 plain characters.')
        if args.description and len(args.description) < 2:
            raise ValueError('A sticker description must be empty or contain 2 to 100 characters.')
        payload = await message_content(self.bot, request)
        payload = json.loads(payload[0]['text'] if isinstance(payload, list) else payload)
        visible = {item['url'] for item in payload.get('images', [])}
        visible.update(item['avatar_url'] for item in payload.get('visible_people', []) if item.get('avatar_url'))
        text = request.content + '\n' + payload.get('replying_to', {}).get('message', '')
        visible.update(url.rstrip('.,!?') for url in re.findall(r'https://[^\s<>\[\]()"\']+', text))
        if not args.image_url or args.image_url not in visible or feature.redact(args.image_url) != args.image_url:
            raise ValueError('Choose an image attached, linked or visibly referenced in this request, or one of its visible profile pictures.')
        guild = self.guild()
        permissions = guild.me.guild_permissions
        if not (permissions.create_expressions or permissions.manage_expressions or permissions.administrator):
            raise PermissionError('This assistant needs Create Expressions permission in the configured server.')
        if self.upload_lock.locked():
            raise ValueError('An expression upload is already running. Let it finish before asking again.')
        async with self.upload_lock:
            feature.validate(args.model_dump_json(), request)
            now, author = int(time.time()), str(request.author.id)
            recent = [peer.db.execute('SELECT max(created_at),count(*) FROM expression_uploads WHERE author_id=? AND created_at>?',
                                      (author, now - 86400)).fetchone() for peer in feature.image_peers]
            latest = max((row[0] for row in recent if row[0] is not None), default=None)
            if latest is not None and now - latest < config.expression_cooldown:
                raise ValueError(f'Wait {config.expression_cooldown - (now - latest)} seconds before another expression upload.')
            total = sum(peer.db.execute('SELECT count(*) FROM expression_uploads WHERE created_at>?', (now - 86400,)).fetchone()[0]
                        for peer in feature.image_peers)
            if sum(row[1] for row in recent) >= config.expression_uploads_per_user or total >= config.expression_uploads_per_day:
                raise ValueError('The expression-upload limit for the last 24 hours has been reached. The owner can adjust it in Tools.')
            raw = await download_image(args.image_url, maximum=8 * 1024 * 1024)
            image, suffix, animated = await asyncio.to_thread(prepare_image, raw, args.action)
            digest = hashlib.sha256(image).hexdigest()
            key = (str(request.id), args.action, name.casefold(), digest)
            for peer in feature.image_peers:
                if peer.db.execute('SELECT 1 FROM expression_uploads WHERE request_id=? AND action=? AND name=? AND digest=?', key).fetchone():
                    raise ValueError('That upload was already attempted for this request; it will not be repeated.')
            existing = await (guild.fetch_emojis() if args.action == 'add_emoji' else guild.fetch_stickers())
            if any(item.name.casefold() == name.casefold() for item in existing):
                raise ValueError('An expression with that name already exists. Use it, or choose a different name.')
            used = sum(bool(item.animated) == animated for item in existing) if args.action == 'add_emoji' else len(existing)
            if used >= (guild.emoji_limit if args.action == 'add_emoji' else guild.sticker_limit):
                raise ValueError('This server has no free slots for that expression. No existing expression was changed.')
            feature.validate(args.model_dump_json(), request)
            self.db.execute('INSERT INTO expression_uploads VALUES (?,?,?,?,?,?,?,NULL)',
                            (key[0], str(guild.id), author, key[1], key[2], digest, now))
            self.db.commit()
            try:
                reason = f'Requested by Discord user {request.author.id}; request {request.id}'
                if args.action == 'add_emoji':
                    item = await guild.create_custom_emoji(name=name, image=image, roles=[], reason=reason)
                    data = {'id': str(item.id), 'name': item.name, 'markup': str(item), 'animated': animated}
                else:
                    with io.BytesIO(image) as image_file:
                        attachment = discord.File(image_file, filename='sticker.' + suffix)
                        try:
                            item = await guild.create_sticker(name=name, description=args.description, emoji=args.emoji, file=attachment, reason=reason)
                        finally:
                            attachment.close()
                    data = {'id': str(item.id), 'name': item.name, 'description': item.description}
                result = {'ok': True, 'result': data}
            except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
                result = {'ok': False, 'error': feature.clean_error(exc),
                          'uncertain': not isinstance(exc, discord.HTTPException) or exc.status >= 500}
            self.db.execute('UPDATE expression_uploads SET result=? WHERE request_id=? AND action=? AND name=? AND digest=?',
                            (json.dumps(result), *key))
            self.db.commit()
            if not result['ok']:
                raise ExpressionError(result['error'], uncertain=result['uncertain'])
            return data
