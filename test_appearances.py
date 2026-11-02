"""Offline regressions for subject separation, grounded images and complete forgetting."""
import asyncio
import base64
import json
import logging
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

import testenv  # noqa: F401
from appearances import Appearances, about_person, grounded_prompt, public_appearance
from bots import BotManager
from conversation import profile
from features import FeatureAction
from hearthling import APPLICATION_ID, Hearthling, OWNER_ID, SERVER_ID, open_database
from test_features import CHANNEL_ID, GUEST_ID, PNG, add_turn, http_session, request
from test_hearthling import ai_reply
from test_memories import fact, reply_with, turn_for


async def appearance_checks(root):
    async with Hearthling(None, open_database(':memory:'), data_dir=root) as bot:
        features = bot.features
        message = request()
        message.author.name = 'brand_new_username'
        message.author.display_name = 'New display name'
        message.content = 'Hearthling, draw me holding a cup.'
        old = features.remember(OWNER_ID, '', f'Robin (oldname, {OWNER_ID}) has pale skin, a round face and green eyes.')['memory_id']
        eyes = features.remember(OWNER_ID, '', f'Robin ({OWNER_ID}) has black eyes and huge round dark-green glasses.')['memory_id']
        corrected = features.remember(OWNER_ID, '', f'Correction 2026-10-09 (overrides memory {old}): Robin (oldname, {OWNER_ID}) has warm brown skin and a heart-shaped face.')['memory_id']
        hair = features.remember(OWNER_ID, CHANNEL_ID, f'Robin ({OWNER_ID}) has shoulder-length black hair with long bangs.')['memory_id']
        other = features.remember(GUEST_ID, '', f'Morgan ({GUEST_ID}) has a beard, brown eyes, round glasses and a chubby belly.')['memory_id']
        private = features.remember(GUEST_ID, CHANNEL_ID, f'Morgan ({GUEST_ID}) has a private facial scar.')['memory_id']
        arthur_id = '900000000000000222'
        arthur = features.remember(OWNER_ID, '', f"Arthur (Robin's OC, bot {arthur_id}) has golden skin and blue eyes.")['memory_id']
        cormac = features.remember(OWNER_ID, '', f"Cormac O'Neill (Robin's OC) has ginger hair and blue eyes.")['memory_id']
        bunny = features.remember(OWNER_ID, '', 'Bunny Arthur has black lop ears and a fluffy tail.')['memory_id']
        assert not about_person(f'Other character is a woman, created by Robin ({OWNER_ID}).', profile(message.author))
        assert not about_person(f"Cormac O'Neill (Robin's OC, {OWNER_ID}) has ginger hair.", profile(message.author))
        assert not about_person(f'Robin ({GUEST_ID}) has green eyes.', {'id': str(OWNER_ID), 'display_name': 'Robin'})
        assert not about_person("Devon's character card 'LUCKY RARE': the character is an android body.",
                                {'id': str(GUEST_ID), 'display_name': 'Devon'})
        assert public_appearance(f'Robin ({OWNER_ID}) has shoulder-length black hair.')
        assert not public_appearance(f'Robin ({OWNER_ID}) has read Big Breasts and Wide Hips.')
        assert not public_appearance(f'Robin ({OWNER_ID}) relates to No Longer Human (Osamu Dazai).')
        catalog = Appearances(features, OWNER_ID, [profile(message.author)])
        appearance = catalog.get(catalog.resolve('oldname'))
        assert {item['id'] for item in appearance['records_newest_first']} == {old, eyes, corrected, hair}
        assert appearance['records_newest_first'][1]['id'] == corrected
        assert {item['id'] for item in catalog.get(catalog.resolve(arthur_id))['records_newest_first']} == {arthur}
        assert {item['id'] for item in catalog.get(catalog.resolve("Cormac O'Neill"))['records_newest_first']} == {cormac}
        assert catalog.resolve("name:cormac o'neill")['display_name'] == "Cormac O'Neill"
        assert {item['id'] for item in catalog.get(catalog.resolve('Bunny Arthur'))['records_newest_first']} == {bunny}
        assert {item['id'] for item in catalog.get(catalog.resolve(str(GUEST_ID)))['records_newest_first']} == {other}
        assert not catalog.get(catalog.resolve('Unknown person'))['found']
        # Appearance retrieval survives a zero general-memory budget, with no other person's private facts.
        features.update_settings({'memory_context_chars': 0}, {})
        preview = await bot.converse(message, preview=True)
        prompt = preview['messages'][0]['content']
        assert 'heart-shaped face' in prompt and 'shoulder-length black hair' in prompt and 'private facial scar' not in prompt
        recalled = await bot.execute(json.dumps({'action': 'recall', 'member': str(GUEST_ID)}), message, tool='feature_tool')
        assert recalled['ok'] and recalled['result']['appearance']['found']
        assert [item['id'] for item in recalled['result']['appearance']['records_newest_first']] == [other]

        # Public appearance corrections cannot accidentally move an existing shared record into personal scope.
        message.id += 1
        message.content = 'Hearthling, remember my corrected appearance.'
        saved = await bot.execute(json.dumps({'action': 'remember', 'memory_id': hair,
                                 'fact': f'Robin ({OWNER_ID}) has shoulder-length black hair and long bangs.'}), message, tool='feature_tool')
        assert saved['ok'] and features.memories_db.execute('SELECT channel_id FROM memories WHERE id=?', (hair,)).fetchone()[0] == ''
        updated = await bot.execute(json.dumps({'action': 'remember', 'memory_id': eyes,
                                   'fact': f'Robin ({OWNER_ID}) has black eyes and green glasses.', 'shared': False}), message, tool='feature_tool')
        assert updated['ok']
        # Background extraction gets the same scope correction even if the model omits shared=true.
        message.id += 1
        message.content = 'My hair is shoulder-length.'
        assert bot.memory.enqueue(message, turn_for(message))
        with patch.object(bot.providers, 'reply', new=AsyncMock(return_value=reply_with(
                fact(f'Robin ({OWNER_ID}) has shoulder-length hair.', 'My hair is shoulder-length.', memory_id=hair)))):
            await bot.memory.process_pending()
        assert features.memories_db.execute('SELECT channel_id FROM memories WHERE id=?', (hair,)).fetchone()[0] == ''
        # Explicit privacy remains personal.
        message.id += 1
        message.content = 'Keep this private: remember my facial scar.'
        saved = await bot.execute(json.dumps({'action': 'remember', 'fact': f'Robin ({OWNER_ID}) has a small facial scar.'}), message, tool='feature_tool')
        assert saved['ok']
        assert features.memories_db.execute('SELECT channel_id FROM memories WHERE id=?', (saved['result']['memory_id'],)).fetchone()[0] != ''


async def picture_checks(root):
    credentials = {'image_api_key': 'FAKE', 'image_base_url': 'https://example.invalid/v1', 'image_family': 'responses'}
    async with Hearthling(None, open_database(':memory:'), data_dir=root, credentials=credentials) as bot:
        message = request()
        message.content = 'Hearthling, draw me and Guest in the garden. Put me in a red coat.'
        first = bot.features.remember(OWNER_ID, '', f'Robin ({OWNER_ID}) has warm brown skin, black eyes, long bangs and green glasses.')['memory_id']
        second = bot.features.remember(OWNER_ID, '', f'Guest ({GUEST_ID}) has pale skin, a beard and brown eyes.')['memory_id']
        args = FeatureAction(action='generate_image', prompt='Robin and Guest in a garden. Robin has green eyes.',
                             subjects=[str(OWNER_ID), str(GUEST_ID)])
        async def compose(messages, **options):
            assert options == {'prefill': False, 'max_tokens': 6000}
            source = json.loads(messages[1]['content'])
            assert [entry['person']['id'] for entry in source['profiles']] == [str(OWNER_ID), str(GUEST_ID)]
            assert [[item['id'] for item in entry['records_newest_first']] for entry in source['profiles']] == [[first], [second]]
            assert source['current_request'] == message.content
            return ai_reply(content=json.dumps({'scene': 'Robin and Guest stand in a sunny garden.', 'subjects': [
                {'id': str(OWNER_ID), 'appearance': 'Warm brown skin, black eyes, long bangs, green glasses, red coat.', 'memory_ids': [first]},
                {'id': str(GUEST_ID), 'appearance': 'Pale skin, beard, brown eyes.', 'memory_ids': [second]}]}))
        session = http_session({'output': [{'type': 'image_generation_call', 'result': base64.b64encode(PNG).decode()}]})
        with patch.object(bot.providers, 'reply', new=AsyncMock(side_effect=compose)), patch('features.aiohttp.ClientSession', return_value=session):
            result = await bot.execute(args.model_dump_json(), message, tool='feature_tool')
        assert result['ok']
        prompt = bot.features.picture_prompt(message.id)
        assert prompt == session.post.call_args.kwargs['json']['input'][0]['content'][0]['text']
        assert 'black eyes' in prompt and 'green eyes' not in prompt and 'red coat' in prompt and 'long bangs' in prompt
        assert str(OWNER_ID) not in prompt
        await bot.deliver(message, 'Here it is.', [])
        assert prompt in message.channel.send.call_args_list[-1].args[0]

        # Mentioning the addressed bot in a teacup request does not insert that bot into the picture.
        message.id += 1
        message.content = 'Hearthling, draw a cup of tea.'
        with patch.object(bot.providers, 'reply', new=AsyncMock()) as compose:
            assert await grounded_prompt(bot.features, FeatureAction(action='generate_image', prompt='A cup of tea'), message) == 'A cup of tea'
            compose.assert_not_awaited()
        # A legacy call with 'draw me' and no subjects still loads the speaker's records.
        message.content = 'Hearthling, draw me.'
        with patch.object(bot.providers, 'reply', new=AsyncMock(return_value=ai_reply(content=json.dumps({
                'scene': 'Robin holds a cup.', 'subjects': [{'id': str(OWNER_ID), 'appearance': 'Warm brown skin, black eyes, long bangs, green glasses.', 'memory_ids': [first]}]})))) as compose:
            assert 'long bangs' in await grounded_prompt(bot.features, FeatureAction(action='generate_image', prompt='A person holds a cup'), message)
            assert len(json.loads(compose.call_args.args[0][1]['content'])['profiles']) == 1

        # Incomplete or crossed subject output is rejected before any paid image call.
        for invalid in ('not JSON', json.dumps({'scene': 'Garden', 'subjects': [
                {'id': str(OWNER_ID), 'appearance': 'A person', 'memory_ids': []},
                {'id': str(GUEST_ID), 'appearance': 'Another person', 'memory_ids': [second]}]}),
                json.dumps({'scene': 'Garden', 'subjects': [
                    {'id': str(GUEST_ID), 'appearance': 'Crossed person', 'memory_ids': [first]},
                    {'id': str(OWNER_ID), 'appearance': 'Crossed person', 'memory_ids': [second]}]})):
            message.id += 1
            with patch.object(bot.providers, 'reply', new=AsyncMock(return_value=ai_reply(content=invalid))), \
                    patch('features.aiohttp.ClientSession') as http:
                result = await bot.execute(args.model_dump_json(), message, tool='feature_tool')
                assert not result['ok'] and 'No picture was generated' in result['error']
                http.assert_not_called()
                assert not bot.db.execute('SELECT 1 FROM pictures WHERE request_id=?', (str(message.id),)).fetchone()
        message.id += 1
        async def deleted_during_composition(*values, **options):
            bot.features.forget(OWNER_ID, '', first)
            return ai_reply(content='{}')
        with patch.object(bot.providers, 'reply', new=AsyncMock(side_effect=deleted_during_composition)), \
                patch('features.aiohttp.ClientSession') as http:
            result = await bot.execute(args.model_dump_json(), message, tool='feature_tool')
            assert not result['ok'] and 'memories changed' in result['error']
            http.assert_not_called()


async def deletion_checks(root):
    async with Hearthling(None, open_database(':memory:'), data_dir=root) as primary:
        manager = BotManager(primary)
        identity = '900000000000000222'
        with patch('bots.validate_token', new=AsyncMock(return_value=identity)):
            await manager.add({'name': 'Zom', 'server_id': str(SERVER_ID), 'enabled': False, 'token': 'FAKE'})
        bot = manager.bots[identity]
        bot.accepting_messages = True
        try:
            text = f'Guest ({GUEST_ID}) has a "crescent" birthmark and café-coloured hair.'
            saved = primary.features.remember(GUEST_ID, '', text)['memory_id']
            personal = primary.features.remember(GUEST_ID, CHANNEL_ID, 'Guest private tea preference')['memory_id']
            other = request(GUEST_ID + 1)
            copied = json.dumps([{'role': 'tool', 'content': json.dumps({'fact': text})}])
            add_turn(bot.db, other, copied)
            untouched = request(GUEST_ID + 2, identity=other.id + 1)
            add_turn(bot.db, untouched, '[{"role":"assistant","content":"Unrelated dialogue"}]')
            pending = request(GUEST_ID + 3, identity=other.id + 2)
            pending.content = 'My favourite animal is a cat.'
            assert bot.memory.enqueue(pending, turn_for(pending))
            # Owner can delete a shared record created by somebody else from any assistant account.
            result = await bot.execute(json.dumps({'action': 'forget', 'memory_id': saved}), request(), tool='feature_tool')
            assert result['ok'] and result['result']['forgotten'] == 1
            assert not primary.db.execute('SELECT 1 FROM memories WHERE id=?', (saved,)).fetchone()
            assert bot.db.execute('SELECT messages FROM turns WHERE request_id=?', (str(other.id),)).fetchone()[0] == '[]'
            assert bot.db.execute('SELECT messages FROM turns WHERE request_id=?', (str(untouched.id),)).fetchone()[0] != '[]'
            assert primary.db.execute('SELECT state,source FROM memory_jobs WHERE request_id=?', (str(pending.id),)).fetchone() == ('cancelled', '')
            assert primary.features.memory_epoch == bot.features.memory_epoch == 1
            stale = await bot.execute(json.dumps({'action': 'remember', 'fact': text, 'shared': True}),
                                      request(identity=other.id + 9), tool='feature_tool', memory_epoch=0)
            assert not stale['ok'] and not primary.db.execute('SELECT 1 FROM memories WHERE fact=?', (text,)).fetchone()
            # A public member may remove a shared fact about them, but not another member's private record.
            self_fact = primary.features.remember(OWNER_ID, '', f'Guest ({GUEST_ID}) has brown hair.')['memory_id']
            guest = request(GUEST_ID, identity=other.id + 10)
            assert (await bot.execute(json.dumps({'action': 'forget', 'memory_id': self_fact}), guest, tool='feature_tool'))['ok']
            assert not (await bot.execute(json.dumps({'action': 'forget', 'memory_id': personal}), other, tool='feature_tool'))['ok']
            # Explicit shared bulk deletion leaves personal facts intact.
            primary.features.remember(GUEST_ID, '', 'Guest has green glasses.')
            bulk = await bot.execute('{"action":"forget","forget_all":true,"shared":true}', request(identity=other.id + 20), tool='feature_tool')
            assert bulk['ok'] and primary.features.memory_count(GUEST_ID, CHANNEL_ID) == {'personal': 1, 'server': 0}
            # The deletion turn itself must not save the deleted fact again, even when it appeared in tool output.
            message = request(GUEST_ID, identity=other.id + 30)
            message.content = 'Hearthling, delete my tea preference.'
            message.channel.typing = lambda: AsyncMock()
            async def converse(*args, **kwargs):
                await primary.execute(json.dumps({'action': 'forget', 'memory_id': personal}), message, tool='feature_tool')
                return 'Deleted.', [{'role': 'assistant', 'content': 'Guest private tea preference'}], []
            with patch.object(primary, 'converse', new=AsyncMock(side_effect=converse)), patch.object(primary, 'deliver', new=AsyncMock()):
                assert await primary.process_message(message) == 'complete'
            assert primary.db.execute('SELECT messages FROM turns WHERE request_id=?', (str(message.id),)).fetchone()[0] == '[]'
            assert not primary.features.memories(GUEST_ID, CHANNEL_ID)
        finally:
            await bot.close()
            await bot.ai.close()


async def main():
    with tempfile.TemporaryDirectory(prefix='hearthling-appearance-', dir='/tmp/opencode') as directory:
        for test in (appearance_checks, picture_checks, deletion_checks):
            await test(Path(directory))
    print('PASS: complete subject-bound appearance retrieval, corrections and privacy, grounded picture payload and posted prompt, '
          'no paid request for incomplete composition, cross-bot deletion permissions, recalled-copy removal and capture cancellation.')


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
