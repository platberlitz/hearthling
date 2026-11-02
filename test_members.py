"""Member services and private four-key rotation checks."""
import asyncio
import base64
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

import testenv  # noqa: F401
from aiohttp.test_utils import TestClient, TestServer
import discord

from conversation import message_content
from dashboard import Dashboard
from features import FeatureAction, FeatureError
from hearthling import APPLICATION_ID, Hearthling, OWNER_ID, SERVER_ID, open_database
from images import ImageOptions, image_request, selected
from test_expressions import fixture
from test_features import GUEST_ID, PNG, http_session, request
from test_hearthling import expect_async_error, expect_error


class Role:
    def __init__(self, identity, position, colour=0):
        self.id, self.position, self.name = identity, position, f'role-{identity}'
        self.colour, self.permissions = discord.Colour(colour), discord.Permissions.none()
        self.managed = self.hoist = self.mentionable = False
        self.edit = AsyncMock(side_effect=self.change)

    def __lt__(self, other):
        return (self.position, -self.id) < (other.position, -other.id)

    def __ge__(self, other):
        return not self < other

    def is_default(self):
        return self.id == SERVER_ID

    async def change(self, **values):
        # Reproduce a new role omitted by Role.edit's stale cached-role move list.
        for name in ('colour',):
            if name in values:
                setattr(self, name, values[name])
        return self


async def main():
    with tempfile.TemporaryDirectory(prefix='hearthling-members-', dir=Path(__file__).resolve().parent) as directory:
        credentials = {'ai_api_key': 'FAKE_CHAT', 'image_base_url': 'https://example.invalid/v1',
                       'image_family': 'responses', 'image_api_key': 'KEY_ONE',
                       'image_fallback_keys': ['KEY_TWO', 'KEY_THREE', 'KEY_FOUR']}
        db = open_database(':memory:')
        async with Hearthling(None, db, credentials=credentials, data_dir=directory) as bot:
            guild, channel, message = fixture(bot)
            guild.chunked = True
            member = guild.get_member(GUEST_ID)
            member.name, member.global_name, member.nick, member.display_name = 'Devon_handle', 'Devon', 'Green tea', 'Green tea'
            guild.fetch_member = AsyncMock(side_effect=guild.get_member)
            bot.get_guild = lambda identity: guild if identity == SERVER_ID else None
            source = message(OWNER_ID, 'Hearthling, ping Devon.')
            for name in ('Devon_handle', 'Devon', 'Green tea', f'<@{GUEST_ID}>'):
                assert (await bot.member(guild, name)).id == GUEST_ID
            found = await bot.people.lookup(source, 'Devon')
            assert found['members'][0]['username'] == 'Devon_handle' and not found['ambiguous']
            guild.get_member(OWNER_ID).global_name = 'Devon'
            await expect_async_error(ValueError, bot.member(guild, 'Devon'))
            guild.get_member(OWNER_ID).global_name = 'Owner'
            content = await message_content(bot, source)
            assert any(item['id'] == str(GUEST_ID) for item in json.loads(content)['mentioned_names'])
            first = await bot.people.ping(source, 'Devon')
            assert (await bot.people.ping(source, str(GUEST_ID))) == first and channel.send.await_count == 1
            sent = channel.send.call_args
            assert sent.args[0] == f'<@{GUEST_ID}>' and sent.kwargs['reference'].message_id == source.id
            assert sent.kwargs['allowed_mentions'].to_dict() == {'parse': [], 'users': [GUEST_ID]}
            await expect_async_error(ValueError, bot.people.ping(message(OWNER_ID, 'Ping Devon'), 'Devon'))
            await expect_async_error(PermissionError, bot.people.ping(message(OWNER_ID, 'Hello Devon'), 'Devon'))
            await expect_async_error(ValueError, bot.people.ping(message(OWNER_ID, 'Ping Hearthling'), str(APPLICATION_ID)))
            channel.permissions_for.return_value = discord.Permissions(view_channel=False)
            await expect_async_error(PermissionError, bot.people.ping(source, 'Devon'))
            channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
            db.execute('DELETE FROM member_pings'); db.commit()
            channel.send.side_effect = asyncio.TimeoutError()
            await expect_async_error(asyncio.TimeoutError, bot.people.ping(source, 'Devon'))
            count = channel.send.await_count
            await expect_async_error(FeatureError, bot.people.ping(source, 'Devon'))
            assert channel.send.await_count == count

            default, shared, top = Role(SERVER_ID, 0), Role(900000000000000601, 2, 0x123456), Role(900000000000000602, 10)
            roles = [default, shared, top]
            for person in guild.members:
                person._roles = [default.id]
            member._roles.append(shared.id); guild.me._roles.append(top.id)
            guild.me.guild_permissions = discord.Permissions(manage_roles=True)
            guild.fetch_roles = AsyncMock(side_effect=lambda: roles)
            async def members(limit=None):
                for person in guild.members:
                    yield person
            guild.fetch_members = members
            async def create(**values):
                assert values['permissions'].value == 0 and not values['hoist'] and not values['mentionable']
                role = Role(900000000000000603, 1, values['colour'].value)
                role.name = values['name']; roles.append(role)
                return role
            guild.create_role = AsyncMock(side_effect=create)
            async def move(*, positions, **kwargs):
                assert len(positions) == 1
                for role, position in positions.items():
                    assert role.name == f'colour-{GUEST_ID}' and position < top.position
                    role.position = position
                return roles
            guild.edit_role_positions = AsyncMock(side_effect=move)
            async def assign(role, **kwargs):
                member._roles.append(role.id)
            member.add_roles = AsyncMock(side_effect=assign)
            colour_request = message(GUEST_ID, 'Hearthling, make my role colour #aabbcc.')
            result = await bot.people.colour(colour_request, '#aabbcc')
            personal = roles[-1]
            assert result['visible'] and result['member_id'] == str(GUEST_ID) and not result['shared_roles_changed']
            assert shared.colour.value == 0x123456 and shared.edit.await_count == 0
            assert personal.permissions.value == 0 and personal.id in member._roles
            await bot.people.colour(colour_request, '#ddeeff')
            assert guild.create_role.await_count == 1 and member.add_roles.await_count == 1
            other = guild.get_member(OWNER_ID); other._roles.append(personal.id)
            await expect_async_error(PermissionError, bot.people.colour(colour_request, '#112233'))
            assert personal.colour.value == 0xddeeff
            other._roles.remove(personal.id); personal.permissions = discord.Permissions(manage_messages=True)
            await expect_async_error(PermissionError, bot.people.colour(colour_request, '#112233'))
            personal.permissions = discord.Permissions.none(); shared.position = 20
            await expect_async_error(PermissionError, bot.people.colour(colour_request, '#112233'))
            shared.position = 2
            await expect_async_error(ValueError, bot.people.colour(colour_request, '#000000'))
            personal.edit.side_effect = asyncio.TimeoutError()
            await expect_async_error(FeatureError, bot.people.colour(colour_request, '#112233'))
            assert db.execute('SELECT role_id FROM personal_colours').fetchone()[0] == str(personal.id)
            guild.fetch_roles.side_effect = [roles.copy(), roles[:-1]]
            await expect_async_error(FeatureError, bot.people.colour(colour_request, '#112233'))
            guild.fetch_roles.side_effect = lambda: roles
            bot.features.update_settings({'bot_mode': 'chatbot'}, {})
            assert 'ping_member' in bot.features.allowed_actions(colour_request) and 'web_search' not in bot.features.allowed_actions(colour_request)
            await expect_async_error(PermissionError, bot.people.colour(colour_request, '#112233'))
            bot.features.update_settings({'bot_mode': 'assistant'}, {})
            expect_error(ValueError, lambda: FeatureAction(action='set_my_colour', colour='#aabbcc', member_id=str(OWNER_ID)))
            bot.features.update_settings({}, {'image_fallback_keys': credentials['image_fallback_keys']})
            assert all(key not in bot.features.redact(json.dumps({'keys': credentials['image_fallback_keys']})) for key in credentials['image_fallback_keys'])
            expect_error(ValueError, lambda: bot.features.update_settings({}, {'image_fallback_keys': ['one'] * 4}))
            success = {'output': [{'type': 'image_generation_call', 'result': base64.b64encode(PNG).decode()}]}
            rejected = {'error': {'message': 'Daily total quota exceeded'}}
            session = http_session(success)
            session.post.side_effect = [http_session(rejected, 429).post.return_value for _ in range(3)] + [http_session(success).post.return_value]
            with patch('features.aiohttp.ClientSession', return_value=session):
                image = await bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009001))
            assert session.post.call_count == 4 and image['bytes'] == len(PNG)
            assert [call.kwargs['headers']['Authorization'] for call in session.post.call_args_list] == ['Bearer ' + value for value in ['KEY_ONE', 'KEY_TWO', 'KEY_THREE', 'KEY_FOUR']]
            assert db.execute('SELECT count(*) FROM pictures').fetchone()[0] == 1
            bot.features.update_settings({'image_model': 'gpt-image-1-fast'}, {})
            spec, key = selected(bot.features)
            assert spec.id == 'default' and spec.model == 'gpt-image-1-fast' and key == 'KEY_ONE'
            assert image_request(spec, 'Tea')['tools'][0]['model'] == 'gpt-image-1-fast'
            client = http_session(success)
            client.post.side_effect = [http_session(rejected, 429).post.return_value, http_session(success).post.return_value]
            with patch('features.aiohttp.ClientSession', return_value=client):
                fast = await bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009002))
            assert client.post.call_count == 2 and fast['model'] == 'gpt-image-1-fast'
            assert [call.kwargs['headers']['Authorization'] for call in client.post.call_args_list] == ['Bearer KEY_ONE', 'Bearer KEY_TWO']
            expect_error(ValueError, lambda: ImageOptions(image_providers=[{'id': 'default', 'name': 'Copy', 'base_url': 'https://example.invalid/v1'}]))
            client = http_session(success)
            with patch('features.aiohttp.ClientSession', return_value=client):
                generated = await bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009003))
            assert client.post.call_count == 1 and generated['model'] == 'gpt-image-1-fast'
            banana = {'id': 'banana', 'name': 'Banana', 'family': 'gemini', 'base_url': 'https://example.invalid/google',
                      'model': 'gemini-3-pro-image', 'quality': '2K'}
            bot.features.credentials['image_key:banana'] = 'GEMINI_KEY'
            bot.features.update_settings({'image_providers': [banana], 'image_provider': 'banana'}, {})
            gemini_reply = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                {'thought': True, 'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(b'draft').decode()}},
                {'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode()}}]}}]}
            client = http_session(gemini_reply)
            with patch('features.aiohttp.ClientSession', return_value=client):
                nano = await bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea', size='1536x1024'), request(identity=900000000000009004))
            call = client.post.call_args
            assert call.args[0] == 'https://example.invalid/google/v1beta/models/gemini-3-pro-image:generateContent'
            assert call.kwargs['headers'] == {'x-goog-api-key': 'GEMINI_KEY'} and nano['bytes'] == len(PNG)
            assert call.kwargs['json']['generationConfig'] == {'responseModalities': ['IMAGE'], 'imageConfig': {'aspectRatio': '3:2', 'imageSize': '2K'}}
            client = http_session({'candidates': [{'finishReason': 'IMAGE_SAFETY', 'content': {'parts': []}}]})
            with patch('features.aiohttp.ClientSession', return_value=client):
                await expect_async_error(FeatureError, bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009005)))
            bot.features.update_settings({'image_provider': 'default', 'image_providers': []}, {})
            for index, (status, text, expected) in enumerate([(429, 'Rate limit exceeded', 1), (401, 'Bad key', 1), (403, 'Refused', 1), (503, 'Daily quota exceeded', 1), (429, 'Daily total quota exceeded', 4)]):
                client = http_session({'error': {'message': text}}, status)
                with patch('features.aiohttp.ClientSession', return_value=client):
                    await expect_async_error(FeatureError, bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009010 + index)))
                assert client.post.call_count == expected
            client = http_session(success); client.post.side_effect = asyncio.TimeoutError()
            with patch('features.aiohttp.ClientSession', return_value=client):
                await expect_async_error(FeatureError, bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009020)))
            assert client.post.call_count == 1
            backups = [{'id': 'backup-' + name, 'name': 'Backup ' + name, 'family': 'openai_images',
                        'base_url': 'http://127.0.0.1:8317/v1', 'model': 'backup/gpt-image-' + name,
                        'supports_edits': True} for name in ('primary', 'secondary')]
            for item in backups:
                bot.features.credentials['image_key:' + item['id']] = 'BACKUP_KEY'
            bot.features.update_settings({'image_providers': backups,
                                          'image_fallback_providers': [item['id'] for item in backups]}, {})
            expect_error(ValueError, lambda: bot.features.update_settings({'image_fallback_providers': ['missing']}, {}))
            expect_error(ValueError, lambda: bot.features.update_settings({'image_fallback_providers': ['backup-secondary'] * 2}, {}))
            backup_success = {'data': [{'b64_json': base64.b64encode(PNG).decode()}]}
            client = http_session(success)
            client.post.side_effect = [http_session(rejected, 429).post.return_value for _ in range(4)] + [
                http_session({'error': 'Rate limited'}, 429).post.return_value,
                http_session(backup_success).post.return_value]
            with patch('features.aiohttp.ClientSession', return_value=client), \
                    patch.object(bot.features, 'image_references', new=AsyncMock(return_value=[PNG])):
                fallback = await bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009030))
            assert client.post.call_count == 6 and fallback['model'] == 'backup/gpt-image-secondary'
            assert client.post.call_args.args[0].endswith('/images/edits')
            assert client.post.call_args.kwargs['headers']['Authorization'] == 'Bearer BACKUP_KEY'
            assert 'data' in client.post.call_args.kwargs
            assert db.execute('SELECT state FROM pictures WHERE request_id=?', ('900000000000009030',)).fetchone()[0] == 'complete'
            for offset, outcome in enumerate((http_session({'error': 'Unavailable'}, 503).post.return_value, asyncio.TimeoutError())):
                client = http_session(success)
                client.post.side_effect = [outcome]
                with patch('features.aiohttp.ClientSession', return_value=client):
                    await expect_async_error(FeatureError, bot.features.generate_image(
                        FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009031 + offset)))
                assert client.post.call_count == 1
            client = http_session(success)
            client.post.side_effect = [http_session({'error': 'Bad key'}, 401).post.return_value,
                                       http_session(backup_success).post.return_value]
            with patch('features.aiohttp.ClientSession', return_value=client):
                fallback = await bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009033))
            assert client.post.call_count == 2 and fallback['model'] == 'backup/gpt-image-primary'
            assert client.post.call_args.args[0].endswith('/images/generations')
            client = http_session(success)
            client.post.side_effect = [http_session({'error': {'message': 'No valid OpenAI API keys available for this model'}}, 503).post.return_value,
                                       http_session(backup_success).post.return_value]
            with patch('features.aiohttp.ClientSession', return_value=client):
                fallback = await bot.features.generate_image(FeatureAction(action='generate_image', prompt='Tea'), request(identity=900000000000009034))
            assert client.post.call_count == 2 and fallback['model'] == 'backup/gpt-image-primary'
            bot.features.update_settings({'image_fallback_providers': [], 'image_providers': []}, {})
            dashboard = Dashboard(bot, public_url='http://127.0.0.1/')
            async with TestClient(TestServer(dashboard.app)) as client:
                origin = {'Origin': dashboard.origin}
                token = dashboard.issue_link(OWNER_ID).split('#login=')[1]
                assert (await client.post('/api/login', json={'token': token}, headers=origin)).status == 200
                state = await (await client.get('/api/state')).json()
                assert state['connections']['image_keys'] == 4
                assert all(key not in json.dumps(state) for key in credentials['image_fallback_keys'])
                assert (await client.post('/api/settings', json={'keys': {'image_fallback_keys': ['NEW_TWO', 'NEW_THREE', 'NEW_FOUR']}}, headers=origin)).status == 200
                assert (await client.post('/api/settings', json={'keys': {'image_api_key': ''}}, headers=origin)).status == 200
                assert bot.features.credentials['image_fallback_keys'] == ['NEW_TWO', 'NEW_THREE', 'NEW_FOUR']
                assert (Path(directory) / 'connections.json').stat().st_mode & 0o777 == 0o600
                assert (await client.post('/api/settings', json={'keys': {'image_fallback_keys': []}}, headers=origin)).status == 200
                assert not bot.features.credentials['image_fallback_keys']
        db.close()
    print('PASS: identity matching, explicit single-user pings and replay, personal-only colours and shared-role guards, and four-key daily-quota-only rotation and masking.')


if __name__ == '__main__':
    asyncio.run(main())
