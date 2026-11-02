"""Runnable parity checks: adapters, characters, private APIs, imports and release rollback."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

import testenv  # noqa: F401
from aiohttp.test_utils import TestClient, TestServer
import discord
import httpx
from openai import APIError, APIStatusError

from characters import parse_character
from conversation import text_only
from dashboard import Dashboard
from features import FeatureAction
from hearthling import APPLICATION_ID, Hearthling, OWNER_ID, SERVER_ID, open_database
from images import IMAGE_STYLE, ImageSpec, download_image, extension, image_request
import launcher
from maintenance import export_profile, import_profile, inspect_release, release_archive
from providers import Providers, ProviderSpec, build_request, normalise, strip_prefill, with_prefill, yaml_object
from test_features import PNG, http_session, request
from test_hearthling import ai_reply, expect_async_error, expect_error


CHARACTER = '''## System Persona
You are Rowan, a blunt, kindly gardener.
## Example Dialogue
Rowan: The roses need watering.
## User Context
### actual_user
Likes jasmine tea.
### someone_else
Private unrelated detail.
## Credits
Must not enter the character prompt.
## Fallbacks
failed: The greenhouse door jammed. {detail}
'''


async def main():
    parsed = parse_character(CHARACTER, 'ACTUAL_USER')
    assert parsed['persona'].startswith('You are Rowan') and parsed['examples'].startswith('Rowan:')
    assert 'jasmine' in parsed['user_context'] and 'unrelated' not in parsed['user_context']
    assert parsed['fallbacks']['failed'] == 'The greenhouse door jammed. {detail}'
    assert parsed['ignored_sections'] == ['credits']
    for value in ('&a [*a]', '!!python/object:os.system {}', 'when: 2026-01-01'):
        expect_error(ValueError, lambda value=value: yaml_object(value))
    assert yaml_object('- temperature', exclude=True) == ['temperature']
    tool = {'type': 'function', 'function': {'name': 'lookup', 'description': 'Look up a thing.',
                                           'parameters': {'type': 'object', 'properties': {'id': {'type': 'string'}}}}}
    call = {'id': 'call_test', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"id":"42"}'}}
    messages = [{'role': 'system', 'content': 'Trusted instructions.'},
                {'role': 'user', 'content': [{'type': 'text', 'text': 'Describe this.'}, {'type': 'image_url', 'image_url': {'url': 'https://cdn.discordapp.com/avatars/1/a.png'}}]},
                {'role': 'assistant', 'content': None, 'tool_calls': [call]},
                {'role': 'tool', 'tool_call_id': 'call_test', 'content': '{"ok":true}'}]
    specs = {family: ProviderSpec(id=family, name=family, family=family, base_url='https://example.test/v1', model='test-model', vision=True)
             for family in ('openai_chat', 'openai_responses', 'anthropic', 'gemini')}
    for family, spec in specs.items():
        body = build_request(spec, messages, {'tools': [tool], 'max_tokens': 512, 'tool_choice': 'auto'})
        assert 'Describe this.' in json.dumps(body) and 'lookup' in json.dumps(body)
        if family == 'openai_chat':
            assert body['messages'][-1]['role'] == 'tool' and body['max_tokens'] == 512
        elif family == 'openai_responses':
            assert body['input'][-1]['type'] == 'function_call_output' and body['input'][-1]['call_id'] == 'call_test'
            assert body['store'] is False and body['tools'][0]['name'] == 'lookup'
        elif family == 'anthropic':
            assert body['system'] == 'Trusted instructions.' and body['messages'][-1]['content'][0]['tool_use_id'] == 'call_test'
        else:
            assert body['contents'][-1]['parts'][0]['functionResponse']['name'] == 'lookup'
            assert body['contents'][0]['parts'][1]['discordImage'].startswith('https://cdn.discordapp.com/')
            assert body['tools'][0]['functionDeclarations'][0]['parametersJsonSchema'] == tool['function']['parameters']
        forced = build_request(spec, messages, {'tools': [tool], 'tool_choice': {'type': 'function', 'function': {'name': 'lookup'}}})
        if family == 'openai_responses':
            assert forced['tool_choice'] == {'type': 'function', 'name': 'lookup'}
        if family == 'anthropic':
            assert forced['tool_choice']['type'] == 'tool' and forced['tool_choice']['name'] == 'lookup'
        if family == 'gemini':
            assert forced['toolConfig']['functionCallingConfig'] == {'mode': 'ANY', 'allowedFunctionNames': ['lookup']}
    overridden = specs['openai_chat'].model_copy(update={'include_body_yaml': 'temperature: 0.4\nextra:\n  allowed: true', 'exclude_body_yaml': '- max_tokens'})
    body = build_request(overridden, messages, {})
    assert body['temperature'] == 0.4 and body['extra']['allowed'] is True and 'max_tokens' not in body
    kimi = specs['openai_chat'].model_copy(update={'model': 'moonshot/kimi-k3', 'prefill': 'Hearthling:'})
    chat = [{'role': 'user', 'content': 'Hi'}]
    assert with_prefill(kimi, chat)[-1] == {'role': 'assistant', 'content': 'Hearthling:', 'partial': True}
    assert 'partial' not in with_prefill(kimi.model_copy(update={'model': 'deepseek-v4'}), chat)[-1]
    assert with_prefill(kimi, chat + [{'role': 'tool', 'tool_call_id': 'x', 'content': '{}'}])[-1]['role'] == 'tool'
    def completion(text):
        return normalise(kimi, {'id': 'x', 'object': 'chat.completion', 'created': 0, 'model': 'm',
                                'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}]})
    echoed = completion('Hearthling: hello')
    assert strip_prefill(kimi, echoed).choices[0].message.content == 'hello'
    sent = []
    async def refuse_prefill(spec, history, options):
        sent.append(history[-1]['role'])
        if history[-1]['role'] == 'assistant':
            raise APIStatusError('This model does not support assistant message prefill.', response=httpx.Response(400, request=httpx.Request('POST', 'http://x')), body=None)
        return completion('hello')
    stub = NS(rejected_prefill=set(), rejected_partial=set(), send=refuse_prefill, bot=NS(ai_slots=asyncio.Semaphore(1), features=NS(record_status=lambda *a: None)))
    claude = kimi.model_copy(update={'model': 'claude-opus-5-5'})
    for _ in range(2):
        assert (await Providers.send_prefilled(stub, claude, ('u', 'c'), chat, {})).choices[0].message.content == 'hello'
    assert sent == ['assistant', 'user', 'user'] and stub.rejected_prefill == {('u', 'c')}
    partials = []
    async def empty_partial(spec, history, options):
        partials.append(history[-1].get('partial', False))
        return completion('' if history[-1].get('partial') else 'Hearthling: hello')
    stub.send = empty_partial
    for _ in range(2):
        assert (await Providers.send_prefilled(stub, kimi, ('u', 'k'), chat, {})).choices[0].message.content == 'hello'
    assert partials == [True, False, False] and stub.rejected_partial == {('u', 'k')}
    fixtures = {
        'openai_responses': {'output': [{'type': 'function_call', 'call_id': 'call_test', 'name': 'lookup', 'arguments': '{"id":"42"}'}], 'usage': {'input_tokens': 12, 'output_tokens': 4}},
        'anthropic': {'content': [{'type': 'thinking', 'thinking': 'Private reasoning.', 'signature': 'signed'}, {'type': 'tool_use', 'id': 'call_test', 'name': 'lookup', 'input': {'id': '42'}}], 'usage': {'input_tokens': 12, 'output_tokens': 4}},
        'gemini': {'candidates': [{'content': {'parts': [{'functionCall': {'name': 'lookup', 'args': {'id': '42'}}, 'thoughtSignature': 'signed'}]}}], 'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 4}},
    }
    for family, data in fixtures.items():
        response = normalise(specs[family], data)
        answer = response.choices[0].message
        assert answer.tool_calls[0].function.name == 'lookup' and response.usage.total_tokens == 16
        if family != 'openai_responses':
            turn = {'role': 'assistant', 'content': answer.content, 'tool_calls': [item.model_dump() for item in answer.tool_calls], 'provider_blocks': answer.provider_blocks}
            assert 'signed' in json.dumps(build_request(specs[family], [*messages[:2], turn], {}))
        expect_error(ValueError, lambda family=family: normalise(specs[family], {}))
    image_spec = ImageSpec(id='pictures', name='Pictures', family='openai_images', base_url='https://example.test/v1',
                           model='picture-model', response_format='b64_json', style='natural', output_format='jpeg')
    assert image_request(image_spec, 'A garden.')['output_format'] == 'jpeg'
    assert image_request(image_spec, 'A garden.')['prompt'].endswith(IMAGE_STYLE)
    assert image_request(image_spec, 'A realistic photo of a garden.')['prompt'].endswith(IMAGE_STYLE)
    assert image_request(image_spec, 'A garden', art_style='pencil drawing')['prompt'] == 'A garden, pencil drawing'
    assert extension(b'\xff\xd8\xffjpeg') == 'jpg'
    await expect_async_error(ValueError, download_image('https://127.0.0.1/private'))
    await expect_async_error(ValueError, download_image('file:///etc/passwd'))

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
        root = Path(directory)
        db = open_database(':memory:')
        credentials = {'discord_token': 'private-discord-test', 'ai_api_key': 'private-ai-test', 'image_api_key': 'private-image-test'}
        async with Hearthling(None, db, credentials=credentials, data_dir=root) as bot:
            bot.ai = NS(chat=NS(completions=NS(create=AsyncMock(return_value=ai_reply(content='Hello.')))))
            bot.characters.save({'name': 'Rowan', 'markdown': CHARACTER})
            bot.characters.activate('Rowan')
            assert bot.character_name == 'Rowan'
            assert bot.fallback('failed', detail='Detail.') == 'The greenhouse door jammed. Detail.'
            assert bot.fallback('busy') == 'I am still handling your last request. Give me a moment.'
            bot.active_users.add(OWNER_ID)
            expect_error(ValueError, lambda: bot.characters.save({'name': 'Rowan', 'markdown': CHARACTER + '\nNew text.'}))
            bot.active_users.clear()
            guest = request(author_id=900000000000000033)
            guest.author.name = 'actual_user'
            bot.characters.save_lore({'scope': 'bot', 'text': 'The shared garden has roses.'})
            bot.characters.save_lore({'scope': 'user', 'target': str(OWNER_ID), 'text': 'Private owner lore.'})
            assert len(bot.characters.lore(guest)) == 1
            expect_error(ValueError, lambda: bot.characters.save_lore({'scope': 'server', 'target': '999999999999999999', 'text': 'Wrong server.'}))
            bot.features.remember(guest.author.id, guest.channel.id, 'Likes jasmine tea.')
            bot.db.execute('INSERT INTO memories (author_id,channel_id,fact,created_at,updated_at) VALUES (?,?,?,?,?)',
                           (str(guest.author.id), str(guest.channel.id), 'Likes  jasmine tea.', 1, 1))
            bot.db.commit()
            assert bot.characters.clean_memories(guest.author.id, guest.channel.id)['duplicates_removed'] == 1
            assert bot.features.memories_db is bot.db and bot.characters.lore_db is bot.db
            bot.characters.save_lore({'scope': 'bot', 'target': '900000000000000999', 'text': 'Another bot only.'})
            assert all(item['text'] != 'Another bot only.' for item in bot.characters.lore(guest))
            provider = specs['anthropic'].model_dump() | {'api_key': 'another-private-provider-key', 'headers': {'x-private-token': 'secret-header-value'}}
            bot.providers.save(provider)
            listed = json.dumps(bot.providers.list())
            assert 'another-private-provider-key' not in listed and 'secret-header-value' not in listed
            assert '[redacted]' in bot.features.redact('secret-header-value')
            bot.features.credentials['provider_headers:anthropic']['anthropic-version'] = '2023-06-01'
            assert bot.features.redact('Version 2023-06-01') == 'Version 2023-06-01'
            bot.features.credentials['provider_headers:anthropic']['x-private-number'] = '12'
            assert json.loads(bot.features.redact('{"count": 12, "secret": "12"}')) == {'count': 12, 'secret': '[redacted]'}
            del bot.features.credentials['provider_headers:anthropic']['x-private-number']
            expect_error(ValueError, lambda: bot.characters.save({'name': 'Bad', 'markdown': CHARACTER, 'provider': 'missing'}))
            bot.features.update_settings({'provider_order': ['anthropic', 'current']}, {})
            failure = APIStatusError('Try the next configured provider.', response=httpx.Response(503, request=httpx.Request('POST', 'https://example.test')), body=None)
            with patch.object(bot.providers, 'send', new=AsyncMock(side_effect=[failure, ai_reply(content='Fallback worked.')])) as send:
                answer = await bot.providers.reply([{'role': 'user', 'content': 'Hello.'}])
                assert answer.choices[0].message.content == 'Fallback worked.'
                assert [item.args[0].id for item in send.await_args_list] == ['anthropic', 'current']
            bot.features.update_settings({'provider_order': ['current'], 'ai_reasoning_effort': 'max',
                'ai_fallback_models': ['backup-a', bot.features.settings.ai_model, 'backup-b', 'backup-c'],
                'ai_fallback_reasoning_efforts': {'backup-a': 'low', 'backup-b': None, 'retired': 'high'}}, {})
            assert bot.providers.order() == ['current', 'current-fallback-1', 'current-fallback-2', 'current-fallback-3']
            assert [bot.providers.get(item).model for item in bot.providers.order()[1:]] == ['backup-a', 'backup-b', 'backup-c']
            assert [bot.providers.get(item).reasoning_effort for item in bot.providers.order()[1:]] == ['low', None, 'max']
            assert bot.features.settings.ai_fallback_reasoning_efforts == {'backup-a': 'low', 'backup-b': None}
            assert bot.providers.key(bot.providers.get('current-fallback-1')) == bot.providers.key(bot.providers.current())
            expect_error(ValueError, lambda: bot.features.update_settings({'ai_fallback_models': ['backup-a', ' backup-a']}, {}))
            expect_error(ValueError, lambda: bot.features.update_settings({'ai_fallback_reasoning_efforts': {'backup-a': 'ultra'}}, {}))
            expect_error(ValueError, lambda: bot.providers.save(specs['anthropic'].model_dump() | {'id': 'current-fallback-1'}))
            bot.ai.chat.completions.create = AsyncMock(side_effect=[failure, failure, failure, ai_reply(content='Third fallback worked.')])
            answer = await bot.providers.reply([{'role': 'user', 'content': 'Hello.'}])
            assert answer.choices[0].message.content == 'Third fallback worked.'
            assert [item.kwargs['model'] for item in bot.ai.chat.completions.create.await_args_list] == [bot.features.settings.ai_model, 'backup-a', 'backup-b', 'backup-c']
            assert [item.kwargs.get('reasoning_effort') for item in bot.ai.chat.completions.create.await_args_list] == ['max', 'low', None, 'max']
            bot.features.update_settings({'ai_fallback_models': []}, {})
            assert bot.features.settings.ai_fallback_reasoning_efforts == {}
            from hearthling import check_connections
            client = MagicMock()
            client.__aenter__.return_value = client
            client.login = AsyncMock()
            client.user = NS(id=bot.bot_id)
            client.application_info = AsyncMock(return_value=NS(id=bot.bot_id, flags=NS(gateway_guild_members=True, gateway_message_content=True)))
            member = NS(guild_permissions=NS(administrator=True), top_role=NS(position=2))
            client.fetch_guild = AsyncMock(return_value=NS(id=SERVER_ID, name='Test server', fetch_member=AsyncMock(return_value=member)))
            with patch('hearthling.discord.Client', return_value=client), patch('builtins.print'), \
                    patch.object(bot.providers, 'reply', new=AsyncMock(side_effect=[ai_reply(1), ai_reply(content='Connected.')])) as checked:
                await check_connections(bot, 'private-test-token')
                assert checked.await_count == 2 and checked.call_args.args[0][-1]['role'] == 'tool'
            for family in ('anthropic', 'openai_responses', 'gemini'):
                bot.providers.save(specs[family].model_dump() | {'api_key': 'test-key-for-' + family})
                session = client = http_session(fixtures[family])
                with patch('aiohttp.ClientSession', return_value=session):
                    result = await bot.providers.send(bot.providers.get(family), text_only(messages), {'tools': [tool]})
                assert result.choices[0].message.tool_calls
                headers = client.post.call_args.kwargs['headers']
                if family == 'anthropic':
                    assert headers['anthropic-version'] == '2023-06-01' and headers['x-api-key'] == 'test-key-for-anthropic'
                elif family == 'openai_responses':
                    assert client.post.call_args.args[0].endswith('/responses')
                else:
                    assert headers['x-goog-api-key'] == 'test-key-for-gemini'
                with patch('aiohttp.ClientSession', return_value=http_session({'content': [None], 'output': [None], 'candidates': [None]})):
                    await expect_async_error(APIError, bot.providers.send(bot.providers.get(family), text_only(messages), {}))
            image_session = http_session({})
            image_response = image_session.get.return_value.__aenter__.return_value
            image_response.status, image_response.headers = 200, {'Content-Type': 'image/png'}
            async def image_chunks(_):
                yield PNG
            image_response.content.iter_chunked = image_chunks
            image_body = build_request(specs['gemini'], messages, {})
            with patch('aiohttp.ClientSession', return_value=image_session):
                await bot.providers.gemini_images(image_body)
            assert base64.b64decode(image_body['contents'][0]['parts'][1]['inlineData']['data']) == PNG
            bot.features.settings.image_providers = [image_spec]
            bot.features.credentials['image_key:pictures'] = 'picture-key'
            bot.features.settings.image_provider = 'pictures'
            raw = b'\xff\xd8\xffjpeg'
            session = client = http_session({'data': [{'b64_json': base64.b64encode(raw).decode()}]})
            with patch('aiohttp.ClientSession', return_value=session):
                result = await bot.features.generate_image(FeatureAction(action='generate_image', prompt='A garden.'), guest)
            assert result['filename'].endswith('.jpg') and (root / 'pictures' / result['filename']).read_bytes() == raw
            assert client.post.call_args.args[0].endswith('/images/generations')
            reference = request(author_id=900000000000000033, identity=guest.id + 1)
            reference.attachments = [NS(content_type='image/png', size=len(PNG), filename='source.png',
                                        url='https://cdn.discordapp.com/attachments/1/2/source.png')]
            with patch('features.download_image', new=AsyncMock(return_value=PNG)):
                await expect_async_error(ValueError, bot.features.generate_image(
                    FeatureAction(action='generate_image', prompt='A garden.'), reference))
            assert session.post.call_count == 1
            bot.social.set_zone(guest.author.id, 'Europe/London')
            bot.social.add_reminder(guest, (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(), 'Water the roses.')
            bot.social.add_reminder(request(), (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(), 'Private tea reminder.', dm=True)
            bot.features.remember(guest.author.id, '', 'Server-wide garden trait.')
            exported = export_profile(bot)
            assert 'private-ai-test' not in json.dumps(exported)
            before = bot.db.execute('SELECT count(*) FROM memories').fetchone()[0]
            assert import_profile(bot, exported)['ok']
            assert bot.db.execute('SELECT count(*) FROM memories').fetchone()[0] == before
            imported = import_profile(bot, exported, apply=True)
            assert (root / 'backups' / imported['backup']).is_file()
            assert bot.db.execute('SELECT count(*) FROM lore').fetchone()[0] == 3
            assert any(row[0] == '' and row[1] == 'Server-wide garden trait.'
                       for row in bot.db.execute('SELECT channel_id, fact FROM memories'))
            assert len(bot.social.reminder_list()) == 2 and bot.character_name == 'Rowan'
            assert any(item['channel_id'] == 'dm' for item in bot.social.reminder_list())
            bad = exported | {'settings': exported['settings'] | {'active_character': 'Missing'}}
            expect_error(ValueError, lambda: import_profile(bot, bad, apply=True))
            assert bot.character_name == 'Rowan'

            dashboard = Dashboard(bot, public_url='http://127.0.0.1/')
            async with TestClient(TestServer(dashboard.app)) as web:
                for path in ('providers', 'characters', 'lore', 'reminders', 'diagnostics', 'maintenance', 'export', 'release'):
                    assert (await web.get('/api/' + path)).status == 401
                token = dashboard.issue_link(OWNER_ID).split('login=')[1]
                headers = {'Origin': dashboard.origin}
                assert (await web.post('/api/login', headers=headers, json={'token': token})).status == 200
                assert (await web.post('/api/providers', headers={'Origin': 'https://elsewhere.test'}, json=provider)).status == 403
                assert (await web.get('/api/diagnostics')).status == 200
                assert (await web.post('/api/timezones', headers=headers, json={'author_id': str(OWNER_ID), 'zone': 'Asia/Manila'})).status == 200
                assert bot.social.zone(OWNER_ID) == 'Asia/Manila'
                assert (await web.post('/api/timezones', headers=headers, json={'author_id': str(OWNER_ID), 'zone': 'not-a-timezone'})).status == 400
                assert bot.social.zone(OWNER_ID) == 'Asia/Manila'
                assert (await web.post('/api/timezones', headers=headers, json={'author_id': str(OWNER_ID), 'zone': ''})).status == 200
                assert bot.social.zone(OWNER_ID) == bot.features.settings.timezone
                assert (await web.post('/api/characters/preview', headers=headers, json={'markdown': CHARACTER, 'username': 'actual_user'})).status == 200
                assert (await web.post('/api/characters/activate', headers=headers, json={'name': ''})).status == 200
                assert bot.character_name == 'Hearthling'
                assert (await web.post('/api/providers', headers=headers, json=provider)).status == 200
                data = await (await web.get('/api/state')).json()
                assert 'api_key' not in json.dumps(data['providers']) and 'secret-header-value' not in json.dumps(data)
                assert (await web.post('/api/image-providers', headers=headers, json={'profile': image_spec.model_dump(), 'api_key': 'new-picture-key'})).status == 200
                assert 'new-picture-key' not in await (await web.get('/api/state')).text()
                bundle = await (await web.get('/api/export')).json()
                response = await web.post('/api/import', headers=headers, json={'profile': bundle, 'apply': False})
                assert response.status == 200, await response.text()
                assert (await web.post('/api/maintenance/restart', headers=headers, json={})).status == 400
                assert (await web.post('/api/social/stop', headers=headers, json={'stopped': True})).status == 200
                assert bot.social.interactions_stopped()
                interaction = NS(id=900000000000000009, guild_id=SERVER_ID, guild=NS(id=SERVER_ID), channel_id=guest.channel.id,
                                 channel=guest.channel, created_at=datetime.now(timezone.utc), user=guest.author,
                                 response=NS(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock()), followup=NS(send=AsyncMock()))
                commands = {item.name: item for item in bot.command_tree.get_commands(guild=discord.Object(SERVER_ID))}
                assert {'pause', 'resume', 'status', 'stop', 'speak', 'interact', 'switch', 'reload', 'history', 'recall', 'memory',
                        'memories', 'clearmemories', 'timezone', 'reminders', 'ignore', 'unignore', 'ignorelist', 'lore', 'autonomous', 'nickname-trigger', 'delete_messages'} <= commands.keys()
                await commands['timezone'].get_command('set').callback(interaction, 'Asia/Manila')
                assert bot.social.zone(guest.author.id) == 'Asia/Manila'
                await commands['autonomous'].callback(interaction, True, 20, 10)
                assert not bot.features.settings.channel_rules
                async def deferred_request(**kwargs):
                    assert interaction.user.id in bot.active_users
                    raise RuntimeError('Stopped at the first awaited Discord operation.')
                interaction.response.defer = deferred_request
                await expect_async_error(RuntimeError, commands['interact'].callback(interaction, 'Hello.'))
                assert not bot.active_users
                interaction.user = NS(id=OWNER_ID, bot=False)
                await commands['autonomous'].callback(interaction, True, 20, 10)
                assert bot.features.settings.channel_rules[str(guest.channel.id)].chance == 20
                archive = release_archive()
                identity, contents = inspect_release(archive)
                assert 'hearthling.py' in contents and len(identity) == 24
                assert {'memories.py', 'test_memories.py'} <= set(contents)
                invalid = io.BytesIO()
                with zipfile.ZipFile(invalid, 'w') as zip_file:
                    zip_file.writestr('../credentials.json', '{}')
                expect_error(ValueError, lambda: inspect_release(invalid.getvalue()))
                process = NS(returncode=0, communicate=AsyncMock(return_value=(b'PASS', None)))
                with patch('asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
                    staged = await dashboard.maintenance.stage(archive)
                assert 'test_parity.py' in staged['checks_passed'] and dashboard.maintenance.status()['staged']['id'] == identity
                assert 'test_memories.py' in staged['checks_passed']
        db.close()

        releases = root / 'launcher-releases'
        releases.mkdir()
        candidate = releases / ('a' * 24)
        candidate.mkdir()
        (candidate / 'hearthling.py').write_text('pass\n')
        with patch.object(launcher, 'RELEASES', releases), patch.object(launcher, 'ROOT', root), patch('launcher.signal.signal'):
            launcher.write('pending.json', {'id': 'a' * 24})
            first = NS(poll=lambda: 1, wait=lambda **kwargs: 1)
            second = NS(poll=lambda: None, wait=lambda **kwargs: 0)
            with patch('launcher.subprocess.Popen', side_effect=[first, second]):
                assert launcher.main() == 0
            assert launcher.read('status.json')['ok'] is False and not (releases / 'pending.json').exists()
    print('PASS: character sections/lore/scopes, four provider formats and fallback, private headers, image profiles, slash controls, profile import, diagnostics, protected APIs and release validation/rollback.')


if __name__ == '__main__':
    asyncio.run(main())
