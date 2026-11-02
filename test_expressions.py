"""Offline role, native-expression and dashboard check tests. No live accounts."""
import asyncio
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import testenv  # noqa: F401
import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import discord
from PIL import Image

from bots import BotManager
from conversation import message_images
from dashboard import Dashboard
from expressions import prepare_image
from hearthling import APPLICATION_ID, Hearthling, OWNER_ID, SERVER_ID, open_database
from test_features import CHANNEL_ID, GUEST_ID
from test_hearthling import ai_reply, expect_async_error, expect_error


SOURCE = 'https://cdn.discordapp.com/attachments/123/456/tea.png'
EMOJI_ID, STICKER_ID = 900000000000000701, 900000000000000702


def picture(*, animated=False, duration=100):
    output = io.BytesIO()
    first = Image.new('RGBA', (384, 128), 'teal')
    if animated:
        first.save(output, format='GIF', save_all=True, append_images=[Image.new('RGBA', first.size, 'purple')],
                   duration=duration, loop=0)
    else:
        first.save(output, format='PNG')
    return output.getvalue()


def emoji(identity=EMOJI_ID, name='tea'):
    item = MagicMock(spec=discord.Emoji)
    item.id, item.name, item.animated, item.guild_id = identity, name, False, SERVER_ID
    item.url = f'https://cdn.discordapp.com/emojis/{identity}.png'
    item.is_usable.return_value = True
    item.__str__.return_value = f'<:{name}:{identity}>'
    return item


def sticker(identity=STICKER_ID, name='Tea time'):
    item = MagicMock(spec=discord.GuildSticker)
    item.id, item.name, item.available, item.guild_id = identity, name, True, SERVER_ID
    item.url, item.description, item.format = f'https://cdn.discordapp.com/stickers/{identity}.png', 'Tea, please.', discord.StickerFormatType.png
    return item


def fixture(bot):
    people = {identity: NS(id=identity, bot=identity == APPLICATION_ID, name='hearthling' if identity == APPLICATION_ID else 'member',
                          display_name='Hearthling' if identity == APPLICATION_ID else 'Member', create_dm=AsyncMock())
              for identity in (OWNER_ID, GUEST_ID, APPLICATION_ID)}
    guild = NS(id=SERVER_ID, owner_id=OWNER_ID, members=list(people.values()), emojis=[emoji()], stickers=[sticker()],
               emoji_limit=50, sticker_limit=5, channels=[], threads=[], get_member=people.get, get_channel_or_thread=lambda _: None)
    guild.me = people[APPLICATION_ID]
    guild.me.guild_permissions = discord.Permissions(administrator=True)
    guild.fetch_emojis, guild.fetch_stickers = AsyncMock(side_effect=lambda: guild.emojis), AsyncMock(side_effect=lambda: guild.stickers)
    guild.fetch_emoji, guild.fetch_sticker = AsyncMock(), AsyncMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.guild, channel.name = CHANNEL_ID, guild, 'expression-check'
    channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
    guild.channels = [channel]
    counter = iter(range(900000000000010000, 900000000000020000))

    def message(author=GUEST_ID, content='Hearthling, hello.'):
        result = MagicMock(spec=discord.Message)
        result.id, result.author, result.guild, result.channel = next(counter), people[author], guild, channel
        result.content, result.reference, result.mentions, result.embeds, result.stickers = content, None, [], [], []
        result.attachments = [NS(url=SOURCE, filename='tea.png', content_type='image/png', size=100)]
        result.created_at = datetime.now(timezone.utc)
        result.to_reference.side_effect = lambda **kw: discord.MessageReference(message_id=result.id, channel_id=channel.id, guild_id=guild.id, **kw)
        result.add_reaction = AsyncMock()
        return result

    channel.send = AsyncMock(side_effect=lambda *args, **kwargs: message(APPLICATION_ID, args[0] if args else ''))
    async def create_emoji(**kwargs):
        item = emoji(next(counter), kwargs['name'])
        guild.emojis.append(item)
        return item
    async def create_sticker(**kwargs):
        assert kwargs['file'].fp.read().startswith(b'\x89PNG\r\n\x1a\n')
        item = sticker(next(counter), kwargs['name'])
        guild.stickers.append(item)
        return item
    guild.create_custom_emoji, guild.create_sticker = AsyncMock(side_effect=create_emoji), AsyncMock(side_effect=create_sticker)
    bot._connection.user = guild.me
    return guild, channel, message


async def main():
    png = picture()
    for kind, side in (('add_emoji', 128), ('add_sticker', 320)):
        data, suffix, animated = prepare_image(png, kind)
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (side, side) and image.getpixel((0, 0))[3] == 0
        assert suffix == 'png' and not animated
        data, suffix, animated = prepare_image(picture(animated=True), kind)
        with Image.open(io.BytesIO(data)) as image:
            assert image.n_frames == 2 and image.format == ('GIF' if kind == 'add_emoji' else 'PNG')
        assert animated
    expect_error(ValueError, lambda: prepare_image(b'not an image', 'add_emoji'))
    expect_error(ValueError, lambda: prepare_image(b'x' * (8 * 1024 * 1024 + 1), 'add_emoji'))
    expect_error(ValueError, lambda: prepare_image(picture(animated=True, duration=3000), 'add_sticker'))
    with patch.object(Image, 'MAX_IMAGE_PIXELS', 10):
        expect_error(ValueError, lambda: prepare_image(png, 'add_emoji'))

    with tempfile.TemporaryDirectory(prefix='expression-check-', dir=Path(__file__).resolve().parent) as directory:
        db = open_database(':memory:')
        async with Hearthling(None, db, credentials={'ai_api_key': 'FAKE_EXPRESSION_SECRET'}, data_dir=directory) as bot:
            guild, channel, message = fixture(bot)
            with patch.object(bot, 'get_guild', return_value=guild):
                manager = BotManager(bot)
                guest, owner = message(), message(OWNER_ID)
                assert bot.intents.emojis_and_stickers
                text, effects = bot.expressions.prepare(guest, f':tea: <:{"tea"}:{EMOJI_ID}> [REACT: 🙂] [STICKER: {STICKER_ID}] `:tea: [REACT: x]`')
                assert text.startswith(f'<:tea:{EMOJI_ID}> <:tea:{EMOJI_ID}>') and text.endswith('`:tea: [REACT: x]`')
                assert effects == [('react', '🙂'), ('send_sticker', str(STICKER_ID))]
                guest.stickers = [guild.stickers[0]]
                guest.content = f'Hearthling <:tea:{EMOJI_ID}>'
                assert len(message_images(guest, 'Current')) == 3
                guest.stickers = []

                bot.features.update_settings({'bot_mode': 'chatbot'}, {})
                assert set(bot.features.allowed_actions(guest)) == {'remember', 'recall', 'forget', 'list_expressions', 'react', 'send_sticker', 'lookup_members', 'ping_member'}
                assert not (await bot.execute('{"action":"server_info"}', owner))['ok']
                for action in ('add_emoji', 'add_sticker', 'generate_image', 'web_search', 'host_status', 'set_reminder'):
                    assert not (await bot.execute(json.dumps({'action': action}), owner, tool='feature_tool'))['ok']
                await expect_async_error(PermissionError, bot.features.web_search('tea'))
                expect_error(PermissionError, lambda: bot.social.add_reminder(owner, '2030-01-01T12:00:00Z', 'Tea'))
                bot.ai = NS(chat=NS(completions=NS(create=AsyncMock(return_value=ai_reply(content='Tea suits me.')))))
                with patch.object(bot, 'authorised_guild', new=AsyncMock()) as authorise:
                    await bot.converse(owner)
                    authorise.assert_not_awaited()
                tools = bot.ai.chat.completions.create.call_args.kwargs['tools']
                assert [item['function']['name'] for item in tools] == ['feature_tool']
                bot.ai.chat.completions.create.return_value = ai_reply(calls=1)
                with patch.object(bot, 'execute', new=AsyncMock()) as execute:
                    await bot.converse(owner)
                    execute.assert_not_awaited()
                invite = manager.summaries()[0]['invite_url']
                assert not discord.Permissions(int(invite.split('permissions=')[1].split('&')[0])).administrator
                bot.active_users.add(GUEST_ID)
                expect_error(ValueError, lambda: bot.features.update_settings({'bot_mode': 'assistant'}, {}))
                bot.active_users.clear()

                await bot.expressions.use(guest, 'react', str(EMOJI_ID))
                await bot.expressions.use(guest, 'react', ':tea:')
                guest.add_reaction.assert_awaited_once_with(guild.emojis[0])
                await bot.expressions.use(guest, 'send_sticker', str(STICKER_ID))
                await bot.expressions.use(guest, 'send_sticker', 'Tea time')
                assert len([call for call in channel.send.call_args_list if 'stickers' in call.kwargs]) == 1
                assert channel.send.call_args.kwargs['reference'].message_id == guest.id
                await bot.expressions.use(guest, 'react', '🙂')
                await expect_async_error(ValueError, bot.expressions.use(guest, 'react', '❤️'))
                blocked = message()
                blocked.add_reaction.side_effect = asyncio.TimeoutError()
                for _ in range(2):
                    await expect_async_error(ValueError, bot.expressions.use(blocked, 'react', '🙂'))
                blocked.add_reaction.assert_awaited_once()
                channel.permissions_for.return_value = discord.Permissions.none()
                await expect_async_error(PermissionError, bot.expressions.use(message(), 'react', '🙂'))
                channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
                inaccessible = emoji(name='locked'); inaccessible.is_usable.return_value = False
                guild.emojis.append(inaccessible)
                assert not any(item['name'] == 'locked' for item in bot.expressions.catalog()['emojis'])
                await expect_async_error(ValueError, bot.expressions.resolve('locked'))
                guild.emojis.remove(inaccessible)

                normal = message()
                await bot.deliver(normal, f'Lovely. [REACT: 🙂] [STICKER: {STICKER_ID}]', [])
                normal.add_reaction.assert_awaited_once_with('🙂')
                assert channel.send.call_args.kwargs['stickers'] == [guild.stickers[0]]
                followup = message(APPLICATION_ID)
                interaction = NS(id=message().id, user=guest.author, guild=guild, guild_id=SERVER_ID, channel=channel,
                    channel_id=CHANNEL_ID, created_at=datetime.now(timezone.utc), response=NS(defer=AsyncMock()),
                    followup=NS(send=AsyncMock(return_value=followup)), original_response=AsyncMock(return_value=followup))
                command = bot.command_tree.get_command('interact', guild=discord.Object(SERVER_ID))
                with patch.object(bot, 'converse', new=AsyncMock(return_value=(f'Hello. [REACT: 🙂] [STICKER: {STICKER_ID}]', [], []))):
                    await command.callback(interaction, 'Hello.')
                followup.add_reaction.assert_awaited_once_with('🙂')
                assert interaction.followup.send.call_args.kwargs['wait'] is True
                with patch.object(bot, 'converse', new=AsyncMock(return_value=(f'Tea? [STICKER: {STICKER_ID}]', [], []))), \
                        patch.object(bot.social, 'recent_messages', new=AsyncMock(return_value=[])), \
                        patch.object(bot.features, 'generate_image', new=AsyncMock()) as image_call:
                    assert await bot.social.speak(channel, 'Say hello.', picture=True)
                    image_call.assert_not_awaited()
                    assert channel.send.call_args.kwargs['stickers'] == [guild.stickers[0]]

                bot.features.update_settings({'bot_mode': 'assistant', 'expression_cooldown': 0}, {})
                upload = message(content='Hearthling, add this emoji as tea_test.')
                arguments = {'action': 'add_emoji', 'name': 'tea_test', 'image_url': SOURCE}
                with patch('expressions.download_image', new=AsyncMock(return_value=png)) as download:
                    result = await bot.execute(json.dumps(arguments), upload, tool='feature_tool')
                    assert result['ok'], result
                    assert await bot.execute(json.dumps(arguments), upload, tool='feature_tool') == result
                    guild.create_custom_emoji.assert_awaited_once()
                    assert guild.create_custom_emoji.call_args.kwargs['roles'] == []
                    assert str(GUEST_ID) in guild.create_custom_emoji.call_args.kwargs['reason']
                    download.assert_awaited_once_with(SOURCE, maximum=8 * 1024 * 1024)
                    sticker_request = message(content='Hearthling, add this sticker as Tea test.')
                    result = await bot.execute(json.dumps({'action': 'add_sticker', 'name': 'Tea test', 'image_url': SOURCE}), sticker_request, tool='feature_tool')
                    assert result['ok'], result
                    assert guild.create_sticker.call_args.kwargs['file'].fp.closed
                    for bad in (arguments | {'name': 'tea'}, arguments | {'name': 'unseen', 'image_url': 'https://example.invalid/unseen.png'}):
                        assert not (await bot.execute(json.dumps(bad), message(content=upload.content), tool='feature_tool'))['ok']
                    assert not (await bot.execute(json.dumps(arguments | {'name': 'not_requested'}), message(), tool='feature_tool'))['ok']
                    bot.features.update_settings({'public_expression_uploads': False}, {})
                    assert 'add_emoji' not in bot.features.allowed_actions(upload) and 'add_emoji' in bot.features.allowed_actions(owner)
                    bot.features.update_settings({'public_expression_uploads': True, 'expression_cooldown': 60}, {})
                    assert not (await bot.execute(json.dumps(arguments | {'name': 'too_soon'}), message(content=upload.content), tool='feature_tool'))['ok']
                    bot.features.update_settings({'expression_cooldown': 0, 'expression_uploads_per_user': 2}, {})
                    assert not (await bot.execute(json.dumps(arguments | {'name': 'over_limit'}), message(content=upload.content), tool='feature_tool'))['ok']
                    bot.features.update_settings({'expression_uploads_per_user': 10}, {})
                    guild.create_custom_emoji.side_effect = asyncio.TimeoutError()
                    uncertain_request = message(OWNER_ID, 'Hearthling, upload this emoji.')
                    uncertain_args = json.dumps(arguments | {'name': 'uncertain'})
                    result = await bot.execute(uncertain_args, uncertain_request, tool='feature_tool')
                    assert result['outcome_may_be_uncertain']
                    before = guild.create_custom_emoji.await_count
                    assert await bot.execute(uncertain_args, uncertain_request, tool='feature_tool') == result
                    assert guild.create_custom_emoji.await_count == before

                bot.features.update_settings({'bot_mode': 'chatbot'}, {})
                dashboard = Dashboard(bot, manager=manager, public_url='http://127.0.0.1/')
                async with TestClient(TestServer(dashboard.app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                    dashboard.public_url = str(client.make_url('/')); dashboard.origin = dashboard.public_url.rstrip('/')
                    origin = {'Origin': dashboard.origin}
                    assert (await client.get('/api/expressions')).status == 401
                    token = dashboard.issue_link(OWNER_ID).split('#login=')[1]
                    assert (await client.post('/api/login', json={'token': token}, headers=origin)).status == 200
                    assert (await (await client.get('/api/expressions')).json())['emojis']
                    with patch('dashboard.host_status', new=AsyncMock()) as host:
                        result = await (await client.post('/api/check/host_status', json={}, headers=origin)).json()
                        assert result['ok'] is False
                        host.assert_not_awaited()
                    state = await (await client.get('/api/state')).json()
                    assert state['bot']['bot_mode'] == 'chatbot'
                    assert 'FAKE_EXPRESSION_SECRET' not in json.dumps(state)
                await manager.close()
        db.close()
    print('PASS: assistant/chatbot authority, native emoji/sticker/reaction delivery and replay, all reply paths, public bounded uploads and image conversion.')


if __name__ == '__main__':
    asyncio.run(main())
