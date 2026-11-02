"""Run offline feature/auth checks, or --serve for an isolated local browser preview."""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import discord

import testenv  # noqa: F401
from hearthling import APPLICATION_ID, Hearthling, OWNER_ID, SERVER_ID, open_database, should_reply
from dashboard import COOKIE, Dashboard
from features import BRAVE_URL, FeatureError, IMAGE_MODEL, parse_search, host_status
from test_hearthling import ai_reply, expect_async_error, expect_error


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZr0AAAAASUVORK5CYII=')
GUEST_ID = 900000000000000033
CHANNEL_ID = 900000000000000001


def request(author_id=OWNER_ID, identity=900000000000000100, channel_id=CHANNEL_ID):
    channel = NS(id=channel_id, send=AsyncMock())
    author = NS(id=author_id, bot=False, display_name='Owner' if author_id == OWNER_ID else 'Guest', create_dm=AsyncMock())
    message = NS(id=identity, author=author, channel=channel, guild=NS(id=SERVER_ID), reference=None,
                 mentions=[], content='Hearthling, please draw a cup of tea.', created_at=datetime.now(timezone.utc))
    message.to_reference = lambda **kwargs: discord.MessageReference(message_id=identity, channel_id=channel_id, guild_id=SERVER_ID, **kwargs)
    return message


def http_session(payload, status=200):
    async def chunks(size):
        yield json.dumps(payload).encode()
    response = NS(status=status, reason='Forbidden' if status == 403 else 'OK', content=NS(iter_chunked=chunks))
    context = MagicMock()
    context.__aenter__.return_value = response
    session = MagicMock()
    session.__aenter__.return_value = session
    session.post.return_value = session.get.return_value = context
    return session


def add_turn(db, message, text='[]'):
    db.execute("INSERT OR IGNORE INTO turns (request_id, channel_id, author_id, messages, state) VALUES (?, ?, ?, ?, 'complete')",
               (str(message.id), str(message.channel.id), str(message.author.id), text))
    db.commit()


async def main():
    with tempfile.TemporaryDirectory(prefix='hearthling-check-', dir=Path(__file__).resolve().parent) as directory:
        db = open_database(':memory:')
        credentials = {'ai_api_key': 'FAKE_CHAT_SECRET', 'image_api_key': 'FAKE_IMAGE_SECRET',
                       'image_base_url': 'https://example.invalid/v1', 'image_family': 'responses',
                       'brave_api_key': 'FAKE_BRAVE_SECRET'}
        async with Hearthling(None, db, credentials=credentials, data_dir=directory) as bot:
            feature = bot.features
            owner, guest = request(), request(GUEST_ID, 900000000000000101)
            add_turn(db, owner, json.dumps([{'role': 'assistant', 'content': 'Owner-private-history'}]))
            add_turn(db, guest)
            guest_actions = feature.tool(guest)['function']['parameters']['properties']['action']['enum']
            assert {'generate_image', 'web_search', 'web_fetch', 'image_search', 'post_image', 'remember', 'recall', 'forget',
                    'set_timezone', 'set_reminder', 'list_reminders', 'cancel_reminder',
                    'list_expressions', 'react', 'send_sticker', 'add_emoji', 'add_sticker',
                    'lookup_members', 'ping_member', 'set_my_colour'} == set(guest_actions)
            for action in ('host_status', 'dashboard'):
                with patch.object(feature, 'perform', new=AsyncMock()) as perform:
                    result = await bot.execute(json.dumps({'action': action}), guest, tool='feature_tool')
                    assert not result['ok']
                    perform.assert_not_awaited()
            for bad in ('{"action":"shell"}', '{"action":"remember","author_id":"123","fact":"x"}',
                        '{"action":"generate_image","size":"99999x99999"}', '{"action":"forget","memory_id":true}'):
                assert not (await bot.execute(bad, guest, tool='feature_tool'))['ok']
            for bad_request in (NS(**{**vars(guest), 'guild': None}), NS(**{**vars(guest), 'guild': NS(id=123)}),
                                NS(**{**vars(guest), 'author': NS(id=OWNER_ID, bot=True)})):
                assert not feature.allowed_actions(bad_request)
            assert not should_reply(guest, public_chat=False)
            assert not should_reply(guest, name_trigger=False)

            saved = feature.remember(OWNER_ID, CHANNEL_ID, 'Owner-private tea preference')
            feature.remember(GUEST_ID, CHANNEL_ID, 'Guest likes green tea')
            other_channel = request(GUEST_ID, guest.id + 1, CHANNEL_ID + 1)
            add_turn(db, other_channel, json.dumps([{'role': 'assistant', 'content': 'Tea in another channel'}]))
            feature.remember(GUEST_ID, other_channel.channel.id, 'Guest drinks jasmine tea in the garden')
            assert feature.remember(GUEST_ID, other_channel.channel.id, 'Guest likes green tea')['already_saved']
            expect_error(ValueError, lambda: feature.forget(GUEST_ID, CHANNEL_ID, saved['memory_id']))
            expect_error(ValueError, lambda: feature.remember(GUEST_ID, CHANNEL_ID + 1, 'Not mine', saved['memory_id']))
            expect_error(ValueError, lambda: feature.remember(GUEST_ID, CHANNEL_ID, 'FAKE_IMAGE_SECRET'))
            expect_error(ValueError, lambda: feature.remember(GUEST_ID, CHANNEL_ID, 'password: test'))
            expect_error(ValueError, lambda: feature.remember(GUEST_ID, CHANNEL_ID, 'My API key is private-test-key'))
            assert len(feature.memories(GUEST_ID, CHANNEL_ID)) == 2
            assert feature.memory_count(GUEST_ID, CHANNEL_ID) == {'personal': 2, 'server': 0}
            guest_memory = feature.memories(GUEST_ID, CHANNEL_ID, 'green')[0]
            assert guest_memory['scope'] == 'personal'
            saved_keywords = feature.memories_db.execute('SELECT keywords FROM memories WHERE id=?', (guest_memory['id'],)).fetchone()[0]
            assert 'tea' in saved_keywords and 'guest' in saved_keywords
            assert {item['fact'] for item in feature.activated_memories(GUEST_ID, CHANNEL_ID, 'Is the tea ready?')} == {
                'Guest likes green tea', 'Guest drinks jasmine tea in the garden'}
            assert feature.activated_memories(GUEST_ID, CHANNEL_ID, 'tea', budget=1) == []
            assert feature.activated_memories(GUEST_ID, CHANNEL_ID, 'Completely unrelated words.') == []
            recalled = await bot.execute('{"action":"recall","query":"jasmine"}', guest, tool='feature_tool')
            assert recalled['ok'] and recalled['result']['memories'][0]['fact'] == 'Guest drinks jasmine tea in the garden'
            bot.ai = NS(chat=NS(completions=NS(create=AsyncMock(return_value=ai_reply(content='Tea it is.')))))
            await bot.converse(guest)
            sent = json.dumps(bot.ai.chat.completions.create.call_args.kwargs)
            assert 'Guest likes green tea' in sent and 'Guest drinks jasmine tea in the garden' in sent
            assert 'Owner-private' not in sent and 'Tea in another channel' not in sent
            feature.update_settings({'memory_enabled': False}, {})
            preview = await bot.converse(guest, preview=True)
            assert 'Guest drinks jasmine tea' not in json.dumps(preview['messages'])
            feature.update_settings({'memory_enabled': True}, {})
            private_token = 'offline-private-sign-in-token-1234567890'
            private_link = 'https://companion.example.org/dashboard/#login=' + private_token
            db.execute('UPDATE turns SET messages=? WHERE request_id=?',
                       (json.dumps([{'role': 'assistant', 'content': 'Owner-private history: ' + private_link}]), str(owner.id)))
            db.commit()
            reply = request(identity=900000000000009999)
            reply.guild = None
            reply.content = 'Hearthling, can I use this link? ' + private_link
            reply.channel.typing = MagicMock()
            quoted = MagicMock(spec=discord.Message)
            quoted.id, quoted.channel, quoted.content = reply.id - 1, reply.channel, private_link
            quoted.author = NS(id=APPLICATION_ID, display_name='Hearthling')
            reply.reference = NS(message_id=quoted.id, channel_id=reply.channel.id,
                                 type=discord.MessageReferenceType.default, resolved=quoted)
            with patch.object(bot, 'authorised_guild', new=AsyncMock()), patch.object(bot, 'deliver', new=AsyncMock()):
                await bot.on_message(reply)
            assert private_token not in json.dumps(bot.ai.chat.completions.create.call_args.kwargs)
            stored, state = db.execute('SELECT messages, state FROM turns WHERE request_id=?', (str(reply.id),)).fetchone()
            assert state == 'complete' and private_token not in stored
            expect_error(ValueError, lambda: feature.remember(OWNER_ID, CHANNEL_ID, private_link))
            feature.update_settings({'auto_memory': False}, {})
            assert not (await bot.execute('{"action":"remember","fact":"Blue is preferred"}', guest, tool='feature_tool'))['ok']
            guest.content = 'Hearthling, remember that I like blue.'
            assert (await bot.execute('{"action":"remember","fact":"Blue is preferred"}', guest, tool='feature_tool'))['ok']
            feature.forget(GUEST_ID, CHANNEL_ID, forget_all=True)
            assert not feature.memories(GUEST_ID, CHANNEL_ID)
            assert not feature.memories(GUEST_ID, CHANNEL_ID + 1)
            assert db.execute('SELECT messages FROM turns WHERE request_id=?', (str(other_channel.id),)).fetchone()[0] == '[]'
            assert db.execute('SELECT messages FROM turns WHERE request_id=?', (str(owner.id),)).fetchone()[0] != '[]'
            edited = feature.remember(GUEST_ID, CHANNEL_ID, 'Guest brews oolong now.')
            feature.remember(GUEST_ID, CHANNEL_ID + 1, 'Guest brews oolong now.', edited['memory_id'], keywords='matcha, oolong')
            assert db.execute('SELECT channel_id FROM memories WHERE id=?', (edited['memory_id'],)).fetchone()[0] == str(CHANNEL_ID)
            assert [item['fact'] for item in feature.activated_memories(GUEST_ID, CHANNEL_ID, 'Any matcha today?')] == ['Guest brews oolong now.']
            assert feature.activated_memories(GUEST_ID, CHANNEL_ID, 'Just tea.') == []
            assert feature.memory_count(GUEST_ID, CHANNEL_ID + 1)['personal'] == 1
            forgotten = await bot.execute(json.dumps({'action': 'forget', 'memory_id': edited['memory_id']}), other_channel, tool='feature_tool')
            assert forgotten['ok'] and forgotten['result']['forgotten'] == 1
            assert not feature.memories(GUEST_ID, CHANNEL_ID)

            shared = feature.remember(GUEST_ID, '', 'Grey right eye and blue left eye.')
            feature.remember(GUEST_ID, '', 'Peacock gentleman who drinks red Bordeaux.')
            assert feature.remember(GUEST_ID, '', 'Grey right eye and blue left eye.')['already_saved']
            assert [(row['fact'], row['scope']) for row in feature.memories(OWNER_ID, CHANNEL_ID + 2)] == [
                ('Owner-private tea preference', 'personal'),
                ('Peacock gentleman who drinks red Bordeaux.', 'server'),
                ('Grey right eye and blue left eye.', 'server')]
            assert feature.memories(GUEST_ID, CHANNEL_ID)[0]['scope'] == 'server'
            assert feature.memory_count(GUEST_ID, CHANNEL_ID) == {'personal': 0, 'server': 2}
            assert feature.forget(GUEST_ID, CHANNEL_ID + 1, forget_all=True)['forgotten'] == 0
            expect_error(ValueError, lambda: feature.forget(OWNER_ID, '', shared['memory_id']))
            forgotten = await bot.execute(json.dumps({'action': 'forget', 'memory_id': shared['memory_id']}), guest, tool='feature_tool')
            assert forgotten['ok'] and forgotten['result']['forgotten'] == 1
            assert 'Grey right eye' not in json.dumps(feature.memories(OWNER_ID, CHANNEL_ID))
            assert db.execute("DELETE FROM memories WHERE channel_id=''").rowcount == 1
            for index in range(120):
                feature.remember(GUEST_ID, '', f'unique server trait {index}')
            assert feature.memory_count(OWNER_ID, CHANNEL_ID)['server'] == 120
            assert feature.activated_memories(GUEST_ID, '', 'trait', budget=0) == []
            assert db.execute("DELETE FROM memories WHERE channel_id=''").rowcount == 120
            assert db.execute('SELECT messages FROM turns WHERE request_id=?', (str(owner.id),)).fetchone()[0] != '[]'

            guest.content = 'Hearthling, draw tea.'
            session = http_session({'output': [{'type': 'image_generation_call', 'result': base64.b64encode(PNG).decode()}]})
            with patch('features.aiohttp.ClientSession', return_value=session):
                result = await bot.execute('{"action":"generate_image","prompt":"A teacup"}', guest, tool='feature_tool')
                assert result['ok'] and result['result']['model'] == IMAGE_MODEL
                payload = session.post.call_args.kwargs['json']
                assert payload['model'] == 'gpt-5-mini' and payload['tools'][0]['model'] == IMAGE_MODEL
                assert payload['tool_choice'] == {'type': 'image_generation'}
                filename = Path(directory) / 'pictures' / result['result']['filename']
                assert filename.read_bytes() == PNG
                repeated = await bot.execute('{"action":"generate_image","prompt":"A teacup"}', guest, tool='feature_tool')
                assert repeated == result
                assert not (await bot.execute('{"action":"generate_image","prompt":"A different teacup"}', guest, tool='feature_tool'))['ok']
                assert session.post.call_count == 1
                another = request(GUEST_ID, guest.id + 100)
                assert (await bot.execute('{"action":"generate_image","prompt":"A teacup"}', another, tool='feature_tool'))['ok']
                assert session.post.call_count == 2  # No cooldown or daily caps; a new message may generate again.
                styled_request = request(GUEST_ID, guest.id + 150)
                assert (await bot.execute('{"action":"generate_image","prompt":"A teacup","art_style":"pencil drawing"}', styled_request, tool='feature_tool'))['ok']
                assert session.post.call_count == 3
                assert session.post.call_args.kwargs['json']['input'][0]['content'][0]['text'] == 'A teacup, pencil drawing'
            await bot.deliver(guest, 'Your tea.', [])
            image_call, prompt_call = guest.channel.send.call_args_list[-2:]
            assert prompt_call.args[0] == 'Prompt sent to the image model:\n```\nA teacup, anime style, cel-shaded, flat colors, clean wordless artwork\n```'
            assert prompt_call.kwargs['reference'].message_id == guest.id
            assert image_call.kwargs['file'].filename == filename.name
            assert image_call.kwargs['reference'].message_id == guest.id
            assert image_call.kwargs['mention_author'] is False
            async def reject_file(*args, **kwargs):
                if 'file' in kwargs:
                    kwargs['file'].close()  # discord.py closes its wrapper even when sending fails.
                    raise discord.HTTPException(NS(status=403, reason='Forbidden'), 'Cannot send here')
            async def accept_file(*args, **kwargs):
                assert kwargs['file'].fp.read() == PNG if 'file' in kwargs else args[0].startswith('Prompt sent to the image model:')
                assert 'reference' not in kwargs
            guest.channel.send.side_effect = reject_file
            guest.author.create_dm.return_value = NS(id=guest.channel.id + 1, send=AsyncMock(side_effect=accept_file))
            await bot.deliver(guest, 'Your tea.', [])
            assert guest.author.create_dm.return_value.send.await_count == 2
            feature.update_settings({'public_images': False}, {})
            assert 'generate_image' not in feature.allowed_actions(guest)
            assert not (await bot.execute('{"action":"generate_image","prompt":"Tea"}', guest, tool='feature_tool'))['ok']
            feature.update_settings({'public_images': True}, {})

            rejection = http_session({'error': {'message': 'Rejected by provider; FAKE_IMAGE_SECRET'}}, 403)
            with patch('features.aiohttp.ClientSession', return_value=rejection):
                rejected = await bot.execute('{"action":"generate_image","prompt":"Tea"}', owner, tool='feature_tool')
                assert not rejected['ok'] and 'HTTP 403' in rejected['error']
                assert 'FAKE_IMAGE_SECRET' not in json.dumps(rejected) and '[redacted]' in rejected['error']
                assert db.execute('SELECT state FROM pictures WHERE request_id=?', (str(owner.id),)).fetchone()[0] == 'rejected'
            timeout_request = request(GUEST_ID + 1, guest.id + 200)
            timed_out = http_session({})
            timed_out.post.side_effect = asyncio.TimeoutError()
            with patch('features.aiohttp.ClientSession', return_value=timed_out):
                failed = await bot.execute('{"action":"generate_image","prompt":"Tea"}', timeout_request, tool='feature_tool')
                assert failed['outcome_may_be_uncertain']
                again = await bot.execute('{"action":"generate_image","prompt":"Tea"}', timeout_request, tool='feature_tool')
                assert failed == again and timed_out.post.call_count == 1
            source = request(OWNER_ID, guest.id + 500)
            source.attachments = [NS(content_type='image/png', size=len(PNG), filename='source.png',
                                     url='https://cdn.discordapp.com/attachments/1/2/source.png')]
            source.content = 'Hearthling, draw my character as a companion.'
            edit_session = http_session({'output': [{'type': 'image_generation_call', 'result': base64.b64encode(PNG).decode()}]})
            with patch('features.aiohttp.ClientSession', return_value=edit_session), \
                    patch('features.download_image', new=AsyncMock(return_value=PNG)) as download:
                result = await bot.execute(json.dumps({'action': 'generate_image', 'prompt': 'A companion portrait'}), source, tool='feature_tool')
                assert result['ok']
                payload = edit_session.post.call_args.kwargs['json']
                parts = payload['input'][0]['content']
                assert [part['type'] for part in parts] == ['input_text', 'input_image']
                assert parts[0]['text'].startswith('A companion portrait')
                assert parts[1]['image_url'] == 'data:image/png;base64,' + base64.b64encode(PNG).decode()
                assert parts[1]['detail'] == 'high' and payload['tools'][0]['action'] == 'edit'
                download.assert_awaited_once_with('https://cdn.discordapp.com/attachments/1/2/source.png', maximum=10 * 1024 * 1024)
                hijack = request(OWNER_ID, guest.id + 600)
                hijack.content = 'Hearthling, draw a portrait.'
                assert not (await bot.execute('{"action":"generate_image","prompt":"Tea","image_url":"https://cdn.discordapp.com/attachments/9/9/other-user.png"}', hijack, tool='feature_tool'))['ok']
                assert edit_session.post.call_count == 1
                linked = request(OWNER_ID, guest.id + 700)
                linked.content = 'Hearthling, draw a portrait from https://cdn.discordapp.com/attachments/3/4/linked.png please.'
                linked_result = await bot.execute('{"action":"generate_image","prompt":"Tea","image_url":"https://cdn.discordapp.com/attachments/3/4/linked.png"}', linked, tool='feature_tool')
                assert linked_result['ok'] and edit_session.post.call_count == 2

            search_data = {'query': {'original': 'tea'}, 'web': {'results': [
                {'title': '<b>Tea &amp; biscuits</b>', 'url': 'https://example.org/tea', 'description': 'A <em>verified</em> excerpt.'},
                {'title': 'Not a web link', 'url': 'javascript:alert(1)', 'description': 'Ignore this result.'},
            ]}}
            assert parse_search(search_data) == [{'title': 'Tea & biscuits', 'url': 'https://example.org/tea', 'snippet': 'A verified excerpt.'}]
            assert parse_search({'query': {'original': 'No matches'}}) == []
            expect_error(FeatureError, lambda: parse_search({'error': 'Unknown response'}))
            search = http_session(search_data)
            search_request = request(GUEST_ID, guest.id + 400)
            with patch('features.aiohttp.ClientSession', return_value=search):
                result = await bot.execute('{"action":"web_search","query":"tea","limit":3,"freshness":"pw"}', search_request, tool='feature_tool')
                assert result['ok'] and result['result']['results'][0]['url'] == 'https://example.org/tea'
                assert search.get.call_args.args == (BRAVE_URL,)
                assert search.get.call_args.kwargs['headers']['X-Subscription-Token'] == 'FAKE_BRAVE_SECRET'
                assert search.get.call_args.kwargs['params']['freshness'] == 'pw'
                cached = await feature.web_search('tea', 3, 'pw')
                assert cached['cached'] and cached['fetched_at'] == result['result']['fetched_at']
                assert search.get.call_count == 1
                await expect_async_error(FeatureError, feature.web_search('FAKE_BRAVE_SECRET'))
                await expect_async_error(FeatureError, feature.web_search('word ' * 51))
                feature.update_settings({'public_search': False}, {})
                assert 'web_search' not in feature.allowed_actions(guest)
                assert not (await bot.execute('{"action":"web_search","query":"tea"}', guest, tool='feature_tool'))['ok']
                assert search.get.call_count == 1
            feature.update_settings({'public_search': True}, {})
            for status in (401, 429, 503):
                rejected_search = http_session({'error': {'detail': 'Provider error FAKE_BRAVE_SECRET'}}, status)
                with patch('features.aiohttp.ClientSession', return_value=rejected_search):
                    result = await bot.execute(json.dumps({'action': 'web_search', 'query': f'tea {status}'}), search_request, tool='feature_tool')
                    assert not result['ok'] and f'HTTP {status}' in result['error']
                    assert 'FAKE_BRAVE_SECRET' not in result['error'] and rejected_search.get.call_count == 1
            timed_search = http_session({})
            timed_search.get.side_effect = asyncio.TimeoutError()
            with patch('features.aiohttp.ClientSession', return_value=timed_search):
                await expect_async_error(FeatureError, feature.web_search('a different search'))
                assert timed_search.get.call_count == 1

            base_health = await host_status()
            assert base_health['services'] == [] and 'No services are configured' in base_health['note']
            process = NS(communicate=AsyncMock(return_value=(b'Id=hearthling.service\nLoadState=loaded\nActiveState=active\nSubState=running\n', b'')), kill=MagicMock())
            with patch('features.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)) as launch:
                health = await host_status(('hearthling.service',))
                assert health['memory_total_bytes'] > 0 and health['disk_free_bytes'] >= 0
                assert health['services'][0]['Id'] == 'hearthling.service'
                assert launch.call_args.args[:2] == ('systemctl', 'show') and len(launch.call_args.args) == 5
                process.communicate.return_value = (b'', b'')
                await expect_async_error(FeatureError, host_status(('hearthling.service',)))
                process.communicate.side_effect = [asyncio.TimeoutError(), (b'', b'')]
                await expect_async_error(FeatureError, host_status(('hearthling.service',)))
                process.kill.assert_called_once()

            bot.dashboard = dashboard = Dashboard(bot, public_url='http://127.0.0.1/')
            expect_error(PermissionError, lambda: dashboard.issue_link(GUEST_ID))
            async with TestClient(TestServer(dashboard.app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                dashboard.public_url = str(client.make_url('/'))
                dashboard.origin = dashboard.public_url.rstrip('/')
                origin = {'Origin': dashboard.origin}
                for path in ('/api/state', '/api/status', '/api/memories', f'/api/pictures/{guest.id}'):
                    assert (await client.get(path)).status == 401
                assert (await client.get('/')).status == 200
                assert (await client.get('/avatar.png')).status == 200
                link = dashboard.issue_link(OWNER_ID)
                token = link.split('#login=')[1]
                assert db.execute('SELECT 1 FROM web_sessions WHERE token_hash=?', (token,)).fetchone() is None
                assert (await client.post('/api/login', json={'token': token}, headers={'Origin': 'https://untrusted.invalid'})).status == 403
                logged_in = await client.post('/api/login', json={'token': token}, headers=origin)
                assert logged_in.status == 200 and logged_in.cookies[COOKIE]['httponly']
                assert logged_in.cookies[COOKIE]['samesite'] == 'Strict'
                assert (await client.post('/api/login', json={'token': token}, headers=origin)).status == 401
                assert (await client.post('/api/request-link', json={}, headers={'Origin': 'https://untrusted.invalid'})).status == 403
                with patch.object(dashboard, 'owner_user', new=AsyncMock(return_value=None)):
                    refused = await client.post('/api/request-link', json={}, headers=origin)
                    assert refused.status == 400 and 'could not reach your Discord account' in (await refused.json())['error']
                with patch.object(dashboard, 'owner_user', new=AsyncMock(return_value=owner)), patch.object(dashboard, 'send_link', new=AsyncMock()) as send:
                    asked = await client.post('/api/request-link', json={}, headers=origin)
                    assert asked.status == 200 and await asked.json() == {'ok': True}
                    send.assert_awaited_once_with(owner)
                    assert (await client.post('/api/request-link', json={}, headers=origin)).status == 429
                replacement = NS(bot_id=bot.bot_id, is_ready=MagicMock(return_value=True), get_user=MagicMock(return_value=owner))
                with patch.object(dashboard, 'manager', NS(bots={str(bot.bot_id): replacement})), patch.object(dashboard, 'send_link', new=AsyncMock()) as send:
                    dashboard.link_requested_at = 0.0
                    assert (await client.post('/api/request-link', json={}, headers=origin)).status == 200
                    replacement.get_user.assert_called_once_with(OWNER_ID)
                    send.assert_awaited_once_with(owner)
                state = await (await client.get('/api/state')).json()
                assert 'FAKE_CHAT_SECRET' not in json.dumps(state) and 'FAKE_IMAGE_SECRET' not in json.dumps(state)
                assert (await client.post('/api/settings', json={'settings': {'public_chat': False}})).status == 403
                assert feature.settings.public_chat
                saved_response = await client.post('/api/settings', json={'settings': {'name_trigger': False}, 'keys': {'ai_api_key': 'NEW_FAKE_CHAT_SECRET'}}, headers=origin)
                assert saved_response.status == 200
                assert 'NEW_FAKE_CHAT_SECRET' not in await saved_response.text()
                assert not feature.settings.name_trigger and bot.ai.api_key == 'NEW_FAKE_CHAT_SECRET'
                assert (Path(directory) / 'connections.json').stat().st_mode & 0o777 == 0o600
                assert json.loads((Path(directory) / 'connections.json').read_text())['image_api_key'] == 'FAKE_IMAGE_SECRET'
                assert json.loads((Path(directory) / 'connections.json').read_text())['brave_api_key'] == 'FAKE_BRAVE_SECRET'
                assert (await client.post('/api/settings', json={'settings': {'ai_base_url': 'https://user:secret@invalid.example'}}, headers=origin)).status == 400
                assert feature.settings.ai_base_url == 'https://api.openai.com/v1'
                memory_response = await client.post('/api/memories', json={'author_id': str(GUEST_ID), 'channel_id': str(CHANNEL_ID), 'fact': '<script>Not HTML</script>', 'keywords': 'html, script'}, headers=origin)
                assert memory_response.status == 200
                identity = (await memory_response.json())['memory_id']
                assert feature.memories_db.execute('SELECT keywords FROM memories WHERE id=?', (identity,)).fetchone()[0] == 'html, script'
                edited = await client.post('/api/memories', json={'author_id': str(GUEST_ID), 'channel_id': str(CHANNEL_ID), 'memory_id': identity, 'fact': 'Updated tea preference'}, headers=origin)
                assert edited.status == 200 and feature.memories(GUEST_ID, CHANNEL_ID)[0]['fact'] == 'Updated tea preference'
                response = await client.delete(f'/api/memories/{identity}', json={}, headers=origin)
                assert response.status == 200 and not feature.memories(GUEST_ID, CHANNEL_ID)
                shared_response = await client.post('/api/memories', json={'author_id': str(GUEST_ID), 'channel_id': '', 'fact': 'Server-wide tea trait'}, headers=origin)
                assert shared_response.status == 200
                shared_id = (await shared_response.json())['memory_id']
                listed = await (await client.get('/api/memories')).json()
                assert listed['total'] >= 1
                assert any(row['id'] == shared_id and row['channel_id'] == '' for row in listed['memories'])
                assert (await client.delete(f'/api/memories/{shared_id}', json={}, headers=origin)).status == 200
                assert not feature.memories(GUEST_ID, CHANNEL_ID)
                picture = await client.get(f'/api/pictures/{guest.id}')
                assert picture.status == 200 and await picture.read() == PNG
                assert picture.headers['X-Frame-Options'] == 'DENY'
                feature.update_settings({'host_status_enabled': True}, {})
                with patch('dashboard.host_status', new=AsyncMock(return_value={'host': 'private-test-host'})):
                    response = await client.post('/api/check/host_status', json={}, headers=origin)
                    assert (await response.json())['result']['host'] == 'private-test-host'
                db.execute("UPDATE web_sessions SET expires_at=0 WHERE kind='session'")
                db.commit()
                assert (await client.get('/api/state')).status == 401
                token = dashboard.issue_link(OWNER_ID).split('#login=')[1]
                assert (await client.post('/api/login', json={'token': token}, headers=origin)).status == 200
                assert (await client.post('/api/logout', json={}, headers=origin)).status == 200
                assert (await client.get('/api/status')).status == 401
                expired = dashboard.issue_link(OWNER_ID).split('#login=')[1]
                db.execute("UPDATE web_sessions SET expires_at=0 WHERE kind='link'")
                db.commit()
                assert (await client.post('/api/login', json={'token': expired}, headers=origin)).status == 401
            production = Dashboard(bot, public_url='https://companion.example.org/hearthling/')
            token = production.issue_link(OWNER_ID).split('#login=')[1]
            response = await production.login(NS(json=AsyncMock(return_value={'token': token})))
            assert response.cookies[COOKIE]['secure'] and response.cookies[COOKIE]['path'] == '/hearthling/'
            # The exact dashboard command must work even when the conversation provider is unavailable.
            owner.content = 'Hearthling, dashboard'
            owner.guild = None  # Owner DMs still work when the name trigger is disabled.
            owner.id += 500
            owner.channel.typing = MagicMock(return_value=AsyncMock())
            with patch.object(bot, 'converse', new=AsyncMock()) as converse, patch.object(bot, 'deliver', new=AsyncMock()) as deliver:
                dashboard.send_link = AsyncMock()
                await bot.on_message(owner)
                converse.assert_not_awaited()
                dashboard.send_link.assert_awaited_once()
                assert 'private dashboard link' in deliver.call_args.args[1]
        db.close()
        saved_db = Path(directory) / 'restart-check.sqlite3'
        for initial in (True, False):
            restarted_db = open_database(saved_db)
            async with Hearthling(None, restarted_db, data_dir=directory) as restarted:
                if initial:
                    restarted.features.remember(GUEST_ID, CHANNEL_ID, 'This fact survives a restart.')
                    restarted.features.update_settings({'public_images': False}, {})
                else:
                    assert restarted.features.memories(GUEST_ID, CHANNEL_ID + 1)[0]['fact'] == 'This fact survives a restart.'
                    assert not restarted.features.memories(OWNER_ID, CHANNEL_ID)
                    assert not restarted.features.settings.public_images
            restarted_db.close()
    print('PASS: public/private tool gates, scoped durable facts, image payload/PNG/rejections/quotas/replay, Brave sources/cache/errors, secret masking, private settings, sign-in link redaction, one-use sign-in, cookie expiry and cross-site protection.')


async def preview():
    with tempfile.TemporaryDirectory(prefix='hearthling-preview-', dir=Path(__file__).resolve().parent) as directory:
        db = open_database(':memory:')
        async with Hearthling(None, db, data_dir=directory) as bot:
            add_turn(db, request())
            dashboard = Dashboard(bot, public_url='http://127.0.0.1:8789/', port=8789)
            # An isolated preview with no provider keys, no Discord connection and an in-memory database.
            token = 'local-preview-owner-link-2026'
            db.execute('INSERT INTO web_sessions VALUES (?, ?, ?)',
                       (hashlib.sha256(token.encode()).hexdigest(), 'link', int(time.time()) + 3600))
            db.commit()
            await dashboard.start()
            try:
                await asyncio.Event().wait()
            finally:
                await dashboard.close()
        db.close()


if __name__ == '__main__':
    logging.getLogger('discord.client').setLevel(logging.ERROR)
    asyncio.run(preview() if '--serve' in sys.argv else main())
