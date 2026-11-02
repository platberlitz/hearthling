"""Visible Discord context and bounded, explicitly labelled image input."""
import json
import re
from urllib.parse import urlsplit

import discord


def image_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme == 'https' and parts.hostname in {'cdn.discordapp.com', 'media.discordapp.net'}
                and not parts.username and not parts.password and parts.port in {None, 443}
                and parts.path.startswith(('/attachments/', '/avatars/', '/guilds/', '/embed/avatars/', '/emojis/', '/stickers/'))):
            return value
    except ValueError:
        pass
    return None


def profile(user):
    result = {'id': str(user.id), 'is_bot': bool(getattr(user, 'bot', False))}
    for source, field in (('name', 'username'), ('global_name', 'global_name'),
                          ('nick', 'server_nickname'), ('display_name', 'display_name')):
        value = getattr(user, source, None)
        if isinstance(value, str):
            result[field] = value[:100]
    avatar = getattr(user, 'display_avatar', None)
    if isinstance(avatar, discord.Asset):
        avatar = avatar.replace(format='png', size=256).url
    if url := image_url(avatar):
        result['avatar_url'] = url
    return result


def message_images(message, label):
    images = []
    for attachment in getattr(message, 'attachments', []) or []:
        if getattr(attachment, 'content_type', None) not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}:
            continue
        if getattr(attachment, 'size', 0) > 10 * 1024 * 1024:
            continue
        if url := image_url(getattr(attachment, 'url', None)):
            images.append({'url': url, 'label': f'{label}: {attachment.filename[:150]}'})
    for embed in getattr(message, 'embeds', []) or []:
        for field in ('image', 'thumbnail'):
            candidate = getattr(embed, field, None)
            if url := image_url(getattr(candidate, 'proxy_url', None)):
                images.append({'url': url, 'label': label + ': embedded image'})
    for sticker in getattr(message, 'stickers', []) or []:
        if sticker.format != discord.StickerFormatType.lottie and (url := image_url(str(sticker.url))):
            images.append({'url': url, 'label': label + ': sticker ' + sticker.name[:100]})
    for animated, name, identity in re.findall(r'<(a?):([A-Za-z0-9_]+):([0-9]{15,21})>', getattr(message, 'content', ''))[:8]:
        images.append({'url': f'https://cdn.discordapp.com/emojis/{identity}.{"gif" if animated else "png"}',
                       'label': label + ': emoji ' + name})
    return images[:8]


async def resolved_reply(request):
    reference = getattr(request, 'reference', None)
    if not (reference and reference.message_id and reference.channel_id == request.channel.id
            and reference.type == discord.MessageReferenceType.default):
        return None
    permissions = request.channel.permissions_for(request.author) if request.guild else None
    if permissions is not None and not (permissions.view_channel and permissions.read_message_history):
        return None
    replied = reference.resolved
    if not isinstance(replied, discord.Message):
        try:
            replied = await request.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            return None
    if isinstance(replied, discord.Message) and replied.channel.id == request.channel.id:
        return replied
    return None


async def message_content(bot, request):
    config = bot.features.settings
    history_after = bot.characters.history_after(request.author.id, request.channel.id)
    vision = bot.providers.vision_enabled()
    speaker = profile(request.author)
    content = {'speaker_name': request.author.display_name, 'speaker': speaker, 'message': request.content}
    profiles = {speaker['id']: speaker}
    if bot.user:
        own = profile(bot.user)
        content['your_discord_profile'] = own
        profiles.setdefault(own['id'], own)
    for user in (getattr(request, 'mentions', []) or [])[:config.mention_context_max]:
        item = profile(user)
        profiles.setdefault(item['id'], item)
    images = message_images(request, 'Current message')
    reference = request.reference
    if (reference and reference.message_id and reference.channel_id == request.channel.id
            and reference.type == discord.MessageReferenceType.default):
        content['replying_to'] = {'message_id': str(reference.message_id), 'unavailable': True}
        permissions = request.channel.permissions_for(request.author) if request.guild else None
        if permissions is None or (permissions.view_channel and permissions.read_message_history):
            replied = await resolved_reply(request)
            if replied is not None:
                item = profile(replied.author)
                profiles.setdefault(item['id'], item)
                content['replying_to'] = {'message_id': str(replied.id), 'speaker_id': item['id'],
                                         'speaker_name': replied.author.display_name, 'speaker': item,
                                          'message': replied.content[:4000]}
                if config.reply_context_style == 'legacy':
                    content['replying_to']['legacy_quote'] = f'{replied.author.display_name}: {replied.content[:4000]}'
                images.extend(message_images(replied, 'Referenced message'))
    content['visible_people'] = list(profiles.values())
    if request.guild:
        permissions = request.channel.permissions_for(request.author) if callable(getattr(request.channel, 'permissions_for', None)) else None
        if permissions and permissions.view_channel:
            matched = []
            for member in getattr(request.guild, 'members', []) or []:
                values = [getattr(member, field, None) for field in ('name', 'global_name', 'nick', 'display_name')]
                if any(isinstance(value, str) and len(value) > 1 and re.search(r'(?<!\w)' + re.escape(value) + r'(?!\w)', request.content, re.I) for value in values):
                    if request.channel.permissions_for(member).view_channel:
                        matched.append(profile(member))
                if len(matched) >= config.mention_context_max:
                    break
            content['mentioned_names'] = matched
            # Fetch only visible text here, never saved private turns or extra image sources.
            if permissions.read_message_history and config.recent_context and isinstance(request.channel, (discord.TextChannel, discord.Thread)):
                try:
                    recent = {}
                    messages, size = [], 0
                    async for earlier in request.channel.history(limit=config.recent_context, before=request.created_at):
                        if earlier.id <= history_after:
                            break
                        speaker = profile(earlier.author)
                        item = {'message_id': str(earlier.id), 'speaker': speaker,
                                'created_at': earlier.created_at.isoformat(), 'message': earlier.content[:4000]}
                        reference = earlier.reference
                        if (reference and reference.channel_id == request.channel.id
                                and reference.type == discord.MessageReferenceType.default):
                            item['reply_to_message_id'] = str(reference.message_id)
                        size += len(json.dumps(item, ensure_ascii=False))
                        if size > 16000:
                            break
                        messages.append(item)
                        recent.setdefault(speaker['id'], speaker)
                    content['recent_channel_messages'] = list(reversed(messages))
                    content['recent_participants'] = list(recent.values())[:config.mention_context_max]
                except discord.HTTPException:
                    content['recent_channel_messages_unavailable'] = True
                    content['recent_participants_unavailable'] = True
    if config.vision_profiles:
        images.extend({'url': item['avatar_url'], 'label': f'Profile picture of {item.get("display_name", item["id"])} ({item["id"]})'}
                      for item in profiles.values() if item.get('avatar_url'))
    unique = {item['url']: item for item in reversed(images)}
    images = list(reversed(list(unique.values())))[:config.vision_max_images]
    if images:
        content['images'] = [{'label': item['label'], 'url': item['url']} for item in images]
        content['image_status'] = ('Image inputs follow this text. Profile pictures are avatars, not verified real-world appearances; '
                                   'never base a generated picture of a person on their profile picture, use their saved appearance facts instead.'
                                   if vision else 'Vision is off. These images have not been inspected; do not describe unseen pixels.')
    text = json.dumps(content, ensure_ascii=False)
    if not vision or not images:
        return text
    parts = [{'type': 'text', 'text': text}]
    for item in images:
        parts.extend([{'type': 'text', 'text': item['label']},
                      {'type': 'image_url', 'image_url': {'url': item['url'], 'detail': config.vision_detail}}])
    return parts


def memory_text(content):
    if isinstance(content, list):
        content = content[0].get('text', '') if content else ''
    if isinstance(content, str):
        try:
            data, _ = json.JSONDecoder().raw_decode(content)
        except (TypeError, ValueError):
            try:
                data = json.loads(content)
            except (TypeError, ValueError):
                return content
    else:
        return ''
    if not isinstance(data, dict):
        return content if isinstance(content, str) else ''
    speaker = data.get('speaker')
    speaker_id = speaker.get('id') if isinstance(speaker, dict) else None
    def names(item):
        return [item[key] for key in ('name', 'global_name', 'server_nickname', 'display_name')
                if isinstance(item, dict) and isinstance(item.get(key), str) and item.get('id') != speaker_id]
    parts = [data.get('message', '')]
    replied = data.get('replying_to')
    if isinstance(replied, dict):
        parts.append(replied.get('message', ''))
        parts.extend(names(replied.get('speaker')))
    for item in data.get('visible_people', []) or []:
        parts.extend(names(item))
    for item in data.get('recent_channel_messages', []) or []:
        if isinstance(item, dict):
            parts.append(item.get('message', ''))
            parts.extend(names(item.get('speaker')))
    return '\n'.join(part for part in parts if isinstance(part, str) and part)


def memory_focus(content):
    """Separate the current request and its people from lower-priority channel chatter."""
    if isinstance(content, list):
        content = content[0].get('text', '') if content else ''
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return '', []
    if not isinstance(data, dict):
        return '', []
    people = {}
    def add(person):
        if isinstance(person, dict) and person.get('id'):
            people.setdefault(person['id'], person)
    add(data.get('speaker'))
    add(data.get('your_discord_profile'))
    for person in (data.get('visible_people') or []) + (data.get('mentioned_names') or []):
        add(person)
    parts = [data.get('message', '')]
    reply = data.get('replying_to')
    if isinstance(reply, dict):
        parts.append(reply.get('message', ''))
        add(reply.get('speaker'))
    return '\n'.join(part for part in parts if isinstance(part, str)), list(people.values())


def text_only(messages, reason='Image input is unavailable. Do not claim to have inspected its contents.'):
    result = []
    for message in messages:
        content = message.get('content')
        if isinstance(content, list):
            texts = [part.get('text', '') for part in content if part.get('type') == 'text']
            if any(part.get('type') == 'image_url' for part in content):
                texts.append(reason)
            message = {**message, 'content': '\n'.join(texts)}
        result.append(message)
    return result


async def shared_history(bot, request, *, budget=12000):
    """Earlier dialogue for this speaker, labelled and checked against current channel access."""
    limit = bot.features.settings.history_turns
    if request.author.bot or not request.guild or not limit or budget <= 0:
        return []
    manager = getattr(bot, 'manager', None)
    peers = manager.bots.values() if manager else [bot]
    cutoff = bot.characters.history_after(request.author.id, request.channel.id)
    candidates = []
    for peer in peers:
        if peer.server_id != bot.server_id:
            continue
        rows = peer.db.execute("SELECT request_id,channel_id,messages FROM turns WHERE author_id=? "
            "AND (? OR channel_id<>?) "
            "AND state='complete' AND messages<>'[]' AND CAST(request_id AS INTEGER)>? "
            "AND CAST(request_id AS INTEGER)<? ORDER BY CAST(request_id AS INTEGER) DESC LIMIT ?",
            (str(request.author.id), peer is not bot, str(request.channel.id), cutoff, str(request.id), limit)).fetchall()
        for identity, channel_id, encoded in rows:
            if not identity.isdigit():
                continue
            if int(identity) <= max(peer.characters.history_after(request.author.id, channel_id),
                                    bot.characters.history_after(request.author.id, channel_id)):
                continue
            candidates.append((int(identity), peer, channel_id, encoded))
    candidates.sort(key=lambda item: item[0], reverse=True)
    result, readable, used = [], {}, 0
    for identity, peer, channel_id, encoded in candidates:
        if channel_id not in readable:
            channel = request.channel if channel_id == str(request.channel.id) else bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await bot.fetch_channel(int(channel_id))
                except discord.HTTPException:
                    readable[channel_id] = False
                    continue
            readable[channel_id] = False
            if getattr(channel, 'guild', None) and channel.guild.id == request.guild.id:
                member = channel.guild.get_member(request.author.id)
                me = channel.guild.me
                if member and me:
                    permissions = [channel.permissions_for(person) for person in (member, me)]
                    readable[channel_id] = all(p.view_channel and p.read_message_history for p in permissions)
        if not readable[channel_id]:
            continue
        dialogue = []
        for message in text_only(json.loads(encoded)):
            role, text = message.get('role'), message.get('content')
            if role not in {'user', 'assistant'} or not isinstance(text, str) or not text or message.get('tool_calls'):
                continue
            if role == 'user':
                try:
                    payload, _ = json.JSONDecoder().raw_decode(text)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    text = payload.get('message', '')
            if isinstance(text, str) and text:
                dialogue.append({'speaker_id': str(request.author.id if role == 'user' else peer.bot_id),
                                 'message': text[:2000]})
        if not dialogue:
            continue
        entry = {'request_id': str(identity), 'channel_id': channel_id,
                 'character': {'id': str(peer.bot_id), 'name': peer.character_name}, 'dialogue': dialogue}
        size = len(json.dumps(entry, ensure_ascii=False))
        if used + size > budget:
            continue
        used += size
        result.append(entry)
        if len(result) >= limit:
            break
    return list(reversed(result))


async def model_reply(bot, messages, **options):
    return await bot.providers.reply(messages, prefill=True, **options)
