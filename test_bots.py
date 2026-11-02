"""Offline account isolation/lifecycle checks; --serve is a disconnected browser preview."""
import asyncio
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
from bots import BotInput, BotManager, validate_token
from dashboard import Dashboard
from features import utc_now
from test_hearthling import ai_reply, expect_async_error, expect_error
from test_features import CHANNEL_ID, GUEST_ID, PNG, add_turn, request


SECOND_ID = '900000000000000222'
SECOND_SERVER = '900000000000000333'
CREDENTIALS = {'discord_token': 'FAKE_PRIMARY_TOKEN', 'ai_api_key': 'FAKE_CHAT_KEY',
               'image_api_key': 'FAKE_IMAGE_KEY', 'image_base_url': 'https://example.invalid/v1',
               'image_family': 'responses', 'brave_api_key': 'FAKE_BRAVE_KEY'}
PROFILE = {'name': 'Alfred', 'server_id': SECOND_SERVER, 'enabled': False, 'token': 'FAKE_SECOND_TOKEN'}


def fake_ai():
    return NS(api_key=CREDENTIALS['ai_api_key'], chat=NS(completions=NS(create=AsyncMock(return_value=ai_reply(content='Hello.')))), close=AsyncMock())


async def connected_client(self, token):
    if token == 'FAKE_REJECTED_TOKEN':
        raise discord.LoginFailure('An intentionally rejected test token')
    if token == 'FAKE_FLAKY_TOKEN' and not getattr(connected_client, 'flaked', False):
        connected_client.flaked = True
        raise AttributeError("'NoneType' object has no attribute 'sequence'")
    self._ready.set()
    await asyncio.Event().wait()


async def main():
    with tempfile.TemporaryDirectory(prefix='bot-check-', dir=Path(__file__).resolve().parent) as directory:
        root = Path(directory)
        db = open_database(root / 'hearthling.sqlite3')
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=root) as primary:
            original = request(GUEST_ID)
            add_turn(db, original, json.dumps([{'role': 'assistant', 'content': 'Primary-only history'}]))
            primary.features.remember(GUEST_ID, CHANNEL_ID, 'Primary-only memory')
            manager = BotManager(primary)
            assert len(manager.profiles) == 1
            assert db.execute('SELECT messages FROM turns').fetchone()[0].find('Primary-only') >= 0
            for data in (PROFILE | {'server_id': '../outside'}, PROFILE | {'name': '<@123>'}, PROFILE | {'token': 'private\nvalue'}, PROFILE | {'owner_id': '123'}):
                expect_error(ValueError, lambda data=data: BotInput.model_validate(data))
            primary.features.update_settings({'releases_enabled': True, 'releases_channel': str(CHANNEL_ID), 'releases_repositories': ['example/project']}, {})
            with patch('bots.validate_token', new=AsyncMock(return_value=SECOND_ID)) as validator:
                assert await manager.add(PROFILE) == SECOND_ID
                validator.assert_awaited_once_with(PROFILE['token'])
                await expect_async_error(ValueError, manager.add(PROFILE))
            secondary = manager.bots[SECOND_ID]
            assert primary.features.settings.releases_enabled
            assert not secondary.features.settings.releases_enabled
            assert secondary.db is not db and secondary.character_name == 'Alfred'
            assert secondary.features.credentials['discord_token'] == PROFILE['token']
            assert secondary.features.credentials['brave_api_key'] == CREDENTIALS['brave_api_key']
            assert secondary.features.settings.personality.startswith('You are Alfred,')
            assert 'Primary-only' in json.dumps(secondary.features.memories(GUEST_ID, CHANNEL_ID + 1))
            primary.characters.save_lore({'scope': 'bot', 'text': 'Shared garden lore.'})
            assert any(item['text'] == 'Shared garden lore.' for item in secondary.characters.lore())
            primary.characters.save_lore({'scope': 'global', 'target': str(GUEST_ID), 'text': 'Every account lore.'})
            stored = secondary.characters.lore_db.execute("SELECT target FROM lore WHERE scope='global'").fetchone()
            assert stored[0] == ''
            assert any(item['text'] == 'Every account lore.' for item in secondary.characters.lore())
            assert any(item['text'] == 'Every account lore.' for item in secondary.characters.lore(request(GUEST_ID, 900000000000000401)))
            assert secondary.ai_slots is primary.ai_slots and secondary.features.image_lock is primary.features.image_lock
            assert secondary.social.queue.notice_db is primary.db
            secondary.features.remember(GUEST_ID, CHANNEL_ID, 'Secondary-only memory')
            for path in (secondary.features.data_dir / 'credentials.json', secondary.features.data_dir / 'connections.json'):
                assert path.stat().st_mode & 0o777 == 0o600
                assert CREDENTIALS['discord_token'] not in path.read_text()

            message = request(GUEST_ID, 900000000000000401, CHANNEL_ID + 1)
            message.guild.id = int(SECOND_SERVER)
            message.content = 'Alfred, what is in your memory?'
            assert not primary.addressed(message)
            assert not secondary.addressed(message)  # Added paused until the owner starts it.
            secondary.accepting_messages = True
            assert secondary.addressed(message)
            with patch.object(secondary.ai.chat.completions, 'create', new=AsyncMock(return_value=ai_reply(content='I remember.'))) as reply:
                await secondary.converse(message)
                sent = json.dumps(reply.call_args.kwargs)
                assert 'Secondary-only memory' in sent and 'Primary-only memory' in sent
                assert 'Primary-only history' not in sent
                assert 'Your name is Alfred.' in sent and f'Your Discord user ID is {SECOND_ID}' in sent
                assert f'Server ID: {SECOND_SERVER}' in sent and '{{' not in sent
            peers = secondary.peer_characters()
            assert [item['name'] for item in peers] == ['Hearthling'] and peers[0]['discord_id'] == str(APPLICATION_ID)
            assert 'friendly, curious companion' in peers[0]['description'] and len(peers[0]['description']) <= 2600
            assert primary.peer_characters()[0]['description'].startswith('You are Alfred,')
            secondary.characters.save({'name': 'Alfred', 'markdown': '''## System Persona

Alfred is a butler. He speaks in clipped fragments.

[Alfred's persona: precise, loyal; Alfred's appearance: grey hair, dark tailcoat;]'''})
            secondary.characters.activate('Alfred')
            reference = primary.peer_characters()
            assert reference[0]['description'] == ("Alfred is a butler. He speaks in clipped fragments. [Alfred's persona: precise, loyal; "
                                                    'Alfred\'s appearance: grey hair, dark tailcoat;]')
            with patch.object(secondary.ai.chat.completions, 'create', new=AsyncMock(return_value=ai_reply(content='Hearthling keeps the tea.'))) as reply:
                await secondary.converse(message)
                sent = json.dumps(reply.call_args.kwargs)
                assert 'Their untrusted character references:' in sent and 'friendly, curious companion' in sent
            for mode in ('assistant', 'chatbot'):
                with patch.object(secondary.features.settings, 'bot_mode', mode):
                    preview = await secondary.converse(message, preview=True)
                    prompt = preview['messages'][0]['content']
                    assert 'Within this setting, you are Alfred, a real individual' in prompt
                    assert 'Preserve its species, personality, intelligence, vocabulary and speech patterns' in prompt
                    assert secondary.characters.context(message.author)['persona'] in prompt
                    assert '<voice_examples>\n\n</voice_examples>' in prompt
                    assert 'Your fellow participants are independent people' in prompt
                    assert 'Your account is an assistant bot' not in prompt and 'regular chatbot' not in prompt
                    assert ('Your account has administrative features enabled' if mode == 'assistant' else
                            'server administration are disabled, even for your owner') in prompt
            message.content = f'<@{SECOND_ID}>'
            message.mentions = [NS(id=int(SECOND_ID))]
            assert secondary.addressed(message) and not primary.addressed(message)
            message.mentions = [NS(id=APPLICATION_ID)]
            message.content = f'<@{APPLICATION_ID}>'
            assert not secondary.addressed(message)
            replied = MagicMock(spec=discord.Message)
            replied.author.id = int(SECOND_ID)
            replied.channel.id = message.channel.id
            message.reference = NS(type=discord.MessageReferenceType.default, channel_id=message.channel.id, resolved=replied)
            message.mentions = []; message.content = 'And then?'
            assert secondary.addressed(message)
            replied.author.id = APPLICATION_ID
            assert not secondary.addressed(message)
            message.reference = None
            message.content = 'Hello B.(x)!'
            assert should_reply(message, bot_id=int(SECOND_ID), server_id=int(SECOND_SERVER), character_name='B.(x)')
            message.content = 'Hello Bzzz!'
            assert not should_reply(message, bot_id=int(SECOND_ID), server_id=int(SECOND_SERVER), character_name='B.(x)')
            assert not (await secondary.execute('{"action":"server_info"}', message))['ok']
            assert not (await secondary.execute('{"action":"host_status"}', message, tool='feature_tool'))['ok']
            owner = request(); owner.guild.id = int(SECOND_SERVER)
            actor = NS(id=OWNER_ID, guild_permissions=NS(administrator=True))
            guild = NS(id=int(SECOND_SERVER), fetch_member=AsyncMock(return_value=actor))
            with patch.object(secondary, 'get_guild', return_value=guild) as get_guild:
                assert await secondary.authorised_guild(owner) == (guild, actor)
                get_guild.assert_called_once_with(int(SECOND_SERVER))
            owner.guild.id = SERVER_ID
            await expect_async_error(PermissionError, secondary.authorised_guild(owner))
            with patch.object(secondary, 'application_info', new=AsyncMock(return_value=NS(id=int(SECOND_ID)))):
                secondary._connection.user = NS(edit=AsyncMock())
                await secondary.setup_hook()
                secondary.user.edit.assert_not_awaited()
            secondary._connection.user = None

            picture_id = 900000000000000501
            db.execute("INSERT INTO pictures (request_id, author_id, channel_id, created_at, filename, state) VALUES (?, ?, ?, ?, ?, 'complete')",
                       (str(picture_id), str(GUEST_ID), str(CHANNEL_ID), int(time.time()), 'primary.png'))
            db.commit()
            picture_dir = root / 'pictures'; picture_dir.mkdir()
            (picture_dir / 'primary.png').write_bytes(PNG)

            dashboard = Dashboard(primary, manager=manager, public_url='http://127.0.0.1/')
            manager.dashboard = dashboard
            async with TestClient(TestServer(dashboard.app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                dashboard.public_url = str(client.make_url('/')); dashboard.origin = dashboard.public_url.rstrip('/')
                origin = {'Origin': dashboard.origin}
                selected = origin | {'X-Bot-ID': SECOND_ID}
                assert (await client.get('/api/state', headers=selected)).status == 401
                with patch('bots.validate_token', new=AsyncMock()) as validator:
                    assert (await client.post('/api/bots', json=PROFILE, headers=origin)).status == 401
                    validator.assert_not_awaited()
                token = dashboard.issue_link(OWNER_ID).split('#login=')[1]
                assert (await client.post('/api/login', json={'token': token}, headers=origin)).status == 200
                state = await (await client.get('/api/state', headers=selected)).json()
                assert state['bot']['id'] == SECOND_ID and len(state['bots']) == 2 and state['connections']['brave']
                assert all(value not in json.dumps(state) for key, value in CREDENTIALS.items() if key.endswith(('key', 'token')))
                assert (await client.get('/api/state', headers={'X-Bot-ID': '../outside'})).status == 404
                assert (await client.get(f'/api/state?bot={APPLICATION_ID}', headers=selected)).status == 400
                assert (await client.post('/api/settings', json={'settings': {'public_search': False}}, headers=selected)).status == 200
                assert not secondary.features.settings.public_search and primary.features.settings.public_search
                assert (await client.post('/api/settings', json={'keys': {'brave_api_key': 'SECOND_PRIVATE_KEY'}}, headers=selected)).status == 200
                assert secondary.features.credentials['brave_api_key'] == 'SECOND_PRIVATE_KEY'
                assert primary.features.credentials['brave_api_key'] == CREDENTIALS['brave_api_key']
                memories = await (await client.get('/api/memories', headers=selected)).json()
                assert [row['fact'] for row in memories['memories']] == ['Secondary-only memory', 'Primary-only memory']
                memory_id = next(row['id'] for row in memories['memories'] if row['fact'] == 'Secondary-only memory')
                add_turn(secondary.db, message, json.dumps([{'role': 'assistant', 'content': 'Secondary-only memory in another channel'}]))
                assert (await client.delete(f'/api/memories/{memory_id}', json={}, headers=selected)).status == 200
                assert primary.features.memories(GUEST_ID, CHANNEL_ID)[0]['fact'] == 'Primary-only memory'
                for peer in (primary, secondary):
                    assert not peer.db.execute("SELECT 1 FROM turns WHERE author_id=? AND state='complete' AND messages<>'[]'", (str(GUEST_ID),)).fetchone()
                    assert not peer.db.in_transaction
                secondary.features.remember(GUEST_ID, CHANNEL_ID, 'Second account survives restart')
                assert (await client.get(f'/api/pictures/{picture_id}', headers=selected)).status == 404
                response = await client.get(f'/api/pictures/{picture_id}?bot={APPLICATION_ID}')
                assert response.status == 200 and await response.read() == PNG
                with patch.object(secondary.features, 'web_search', new=AsyncMock(return_value={'results': []})) as search:
                    assert (await client.post('/api/check/web_search', json={}, headers=selected)).status == 200
                    search.assert_awaited_once()
                    assert not db.execute("SELECT 1 FROM settings WHERE name='status:web_search'").fetchone()
                with patch('bots.validate_token', new=AsyncMock(return_value=str(APPLICATION_ID))) as validator:
                    assert (await client.post('/api/bots', json=PROFILE, headers=selected | {'Origin': 'https://untrusted.invalid'})).status == 403
                    validator.assert_not_awaited()
                    response = await client.post('/api/bots', json=PROFILE, headers=origin)
                    assert response.status == 400 and 'already' in (await response.json())['error']
                with patch('bots.validate_token', new=AsyncMock(return_value=str(APPLICATION_ID))):
                    response = await client.post(f'/api/bots/{SECOND_ID}', json=PROFILE | {'token': 'WRONG_ACCOUNT_TOKEN'}, headers=origin)
                    assert response.status == 400 and secondary.features.credentials['discord_token'] == PROFILE['token']

            with patch('hearthling.Hearthling.start', new=connected_client):
                await manager.update(SECOND_ID, PROFILE | {'enabled': True, 'token': ''})
                manager.start(str(APPLICATION_ID))
                await asyncio.sleep(0)
                running = manager.bots[SECOND_ID]
                assert running is not secondary and running.is_ready()
                assert manager.bots[str(APPLICATION_ID)].is_ready()
                class Request(dict):
                    headers = {'X-Bot-ID': SECOND_ID}
                    query = {}
                assert dashboard.bot_for(Request()) is running
                running.active_users.add(GUEST_ID)
                await expect_async_error(ValueError, manager.update(SECOND_ID, PROFILE | {'token': ''}))
                assert running.is_ready()
                running.active_users.clear()
                await manager.update(SECOND_ID, PROFILE | {'token': ''})
                assert running.is_closed() and not running.accepting_messages
                await manager.update(SECOND_ID, PROFILE | {'enabled': True, 'token': ''})
                await asyncio.sleep(0)
                assert manager.bots[SECOND_ID] is not running and manager.bots[SECOND_ID].is_ready()
                assert manager.bots[SECOND_ID].features.memories(GUEST_ID, CHANNEL_ID)[0]['fact'] == 'Second account survives restart'
                with patch('bots.validate_token', new=AsyncMock(return_value=SECOND_ID)), patch('bots.logging.warning'):
                    await manager.update(SECOND_ID, PROFILE | {'enabled': True, 'token': 'FAKE_REJECTED_TOKEN'})
                    await asyncio.wait_for(manager.tasks[SECOND_ID], 2)
                    assert manager.errors[SECOND_ID].startswith('Discord rejected')
                    assert manager.bots[str(APPLICATION_ID)].is_ready()
                manager.reconnect_delay = 0
                with patch('bots.validate_token', new=AsyncMock(return_value=SECOND_ID)), patch('bots.logging.exception'), \
                        patch('bots.logging.warning'):
                    await manager.update(SECOND_ID, PROFILE | {'enabled': True, 'token': 'FAKE_FLAKY_TOKEN'})
                    for _ in range(20):
                        await asyncio.sleep(0)
                    assert connected_client.flaked and manager.bots[SECOND_ID].is_ready()
                    assert SECOND_ID not in manager.errors and not manager.tasks[SECOND_ID].done()
                with patch('bots.validate_token', new=AsyncMock(return_value=SECOND_ID)):
                    await manager.update(SECOND_ID, PROFILE)
                assert json.loads((root / 'bots' / SECOND_ID / 'connections.json').read_text())['discord_token'] == PROFILE['token']
                await manager.close()
        db.close()
        reopened = open_database(root / 'hearthling.sqlite3')
        async with Hearthling(fake_ai(), reopened, credentials=CREDENTIALS, data_dir=root) as primary:
            manager = BotManager(primary)
            assert not manager.profiles[SECOND_ID]['enabled']
            assert manager.bots[SECOND_ID].features.memories(GUEST_ID, CHANNEL_ID)[0]['fact'] == 'Second account survives restart'
            assert not manager.bots[SECOND_ID].features.settings.public_search
            assert manager.bots[SECOND_ID].features.credentials['brave_api_key'] == 'SECOND_PRIVATE_KEY'
            assert {row['fact'] for row in primary.features.memories(GUEST_ID, CHANNEL_ID)} == {'Primary-only memory', 'Second account survives restart'}
            await manager.close()
        reopened.close()
        native = MagicMock()
        native.__aenter__.return_value = native
        native.login = AsyncMock()
        native.application_info = AsyncMock(return_value=NS(id=int(SECOND_ID)))
        with patch('bots.discord.Client', return_value=native):
            assert await validate_token('ANOTHER_FAKE_TOKEN') == SECOND_ID
            native.login.assert_awaited_once_with('ANOTHER_FAKE_TOKEN')
            native.login.side_effect = discord.LoginFailure('test rejection')
            await expect_async_error(ValueError, validate_token('ANOTHER_FAKE_TOKEN'))
    print('PASS: separate identities, owner gates, private settings/keys/pictures, shared memories and lore, account validation, authenticated bot management, independent pause/reconnect/failure and persisted profiles.')


async def preview():
    with tempfile.TemporaryDirectory(prefix='bots-preview-', dir=Path(__file__).resolve().parent) as directory:
        db = open_database(':memory:')
        async with Hearthling(fake_ai(), db, credentials={'discord_token': 'FAKE_PRIMARY_TOKEN'}, data_dir=directory) as primary:
            add_turn(db, request())
            manager = BotManager(primary)
            dashboard = manager.dashboard = Dashboard(primary, manager=manager, public_url='http://127.0.0.1:8789/', port=8789)
            token = 'local-preview-owner-link-2026'
            db.execute('INSERT INTO web_sessions VALUES (?, ?, ?)', (hashlib.sha256(token.encode()).hexdigest(), 'link', int(time.time()) + 3600))
            db.commit()
            async def preview_token(value):
                if value != 'preview-second-token':
                    raise ValueError('Use the local preview token for this disconnected check.')
                return SECOND_ID
            original_add = manager.add
            async def preview_add(data):
                identity = await original_add(data)
                add_turn(manager.bots[identity].db, request())
                return identity
            manager.add = preview_add
            async def preview_search(self, query, limit=10, freshness=None):
                return {'source': 'Brave Search', 'query': query, 'fetched_at': utc_now(), 'cached': False,
                        'results': [{'title': 'Discord developer documentation', 'url': 'https://docs.discord.com/developers/',
                                      'snippet': 'A local preview result for checking the source-link layout.'}]}
            person = NS(id=OWNER_ID, bot=False, name='preview_owner', display_name='Owner', global_name=None, nick=None, display_avatar=None)
            channel = MagicMock(spec=discord.TextChannel)
            channel.id, channel.name = int(CHANNEL_ID), 'preview-chat'
            channel.permissions_for.return_value = NS(view_channel=True, read_message_history=True)
            guild = MagicMock(spec=discord.Guild)
            guild.id, guild.members, guild.channels, guild.threads = SERVER_ID, [person], [channel], []
            from test_expressions import emoji, sticker
            guild.emojis, guild.stickers = [emoji()], [sticker()]
            guild.get_member.return_value = person
            guild.get_channel_or_thread.side_effect = lambda identity: channel if identity == int(CHANNEL_ID) else None
            channel.guild = guild
            async def preview_reply(self, spec, messages, options):
                return ai_reply(content='A local preview reply. No provider request was made.')
            # Both the token check and Gateway are local test doubles; no Discord login or paid calls.
            with patch('bots.validate_token', new=preview_token), patch('hearthling.Hearthling.start', new=connected_client), \
                    patch('features.Features.web_search', new=preview_search), \
                    patch('providers.Providers.send', new=preview_reply), \
                    patch('hearthling.Hearthling.get_guild', return_value=guild), patch('hearthling.Hearthling.get_channel', return_value=channel):
                await dashboard.start()
                try:
                    await asyncio.Event().wait()
                finally:
                    await dashboard.close()
                    await manager.close()
        db.close()


if __name__ == '__main__':
    logging.getLogger('discord.client').setLevel(logging.ERROR)
    asyncio.run(preview() if '--serve' in sys.argv else main())
