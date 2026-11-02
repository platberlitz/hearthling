"""Offline regressions for basic identity recall and independent durable memory capture."""
import asyncio
import json
import logging
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIConnectionError

import testenv  # noqa: F401
from hearthling import APPLICATION_ID, Hearthling, OWNER_ID, SERVER_ID, open_database
from bots import BotManager
from conversation import memory_focus, memory_text, profile
from test_hearthling import ai_reply
from test_features import CHANNEL_ID, GUEST_ID, add_turn, request


def reply_with(*facts):
    return ai_reply(content=json.dumps({'facts': list(facts)}))


def fact(text, evidence, *, shared=False, memory_id=None):
    return {'fact': text, 'evidence': evidence, 'shared': shared, 'memory_id': memory_id}


def turn_for(message, **context):
    return [{'role': 'user', 'content': json.dumps({'speaker': profile(message.author),
             'message': message.content, **context})}, {'role': 'assistant', 'content': 'A character reply, not factual evidence.'}]


async def recall_checks(root):
    async with Hearthling(None, open_database(':memory:'), data_dir=root) as bot:
        speaker = request()
        speaker.author.name = 'oldhandle'
        speaker.author.display_name = 'An entirely new display name'
        speaker.content = 'Hearthling, hello.'
        feature = bot.features
        nationality = feature.remember(OWNER_ID, '', 'Alex (oldhandle, an old nickname) is Canadian.')['memory_id']
        pronouns = feature.remember(OWNER_ID, '', f'Alex (user {OWNER_ID}) is he/him.')['memory_id']
        identity = feature.remember(GUEST_ID, '', f'Hearthling (bot {APPLICATION_ID}) is a man.')['memory_id']
        unrelated = feature.remember(GUEST_ID, '', f'Other character is a woman, created by Alex (user {OWNER_ID}).')['memory_id']
        private = feature.remember(GUEST_ID, CHANNEL_ID, 'Guest-private identity: Canadian man.')['memory_id']
        tea = feature.remember(OWNER_ID, CHANNEL_ID, 'Alex drinks jasmine tea.')['memory_id']
        data = json.loads(turn_for(speaker)[0]['content'])
        data['recent_channel_messages'] = [{'speaker': {'id': str(GUEST_ID), 'display_name': 'Guest'},
                                           'message': 'Guest talks about goldfish.'}]
        for index in range(45):
            feature.remember(GUEST_ID, '', f'Guest goldfish record {index}: ' + 'old chatter ' * 15)
        text = json.dumps(data)
        focus, people = memory_focus(text)
        people.append({'id': str(APPLICATION_ID), 'display_name': 'Hearthling'})
        selected = feature.activated_memories(OWNER_ID, CHANNEL_ID, memory_text(text), 1800, focus=focus, people=people)
        ids = {item['id'] for item in selected}
        assert {nationality, pronouns, identity, tea} <= ids and private not in ids and unrelated not in ids
        assert len(json.dumps(selected, ensure_ascii=False)) <= 1800
        assert feature.activated_memories(OWNER_ID, CHANNEL_ID, 'unrelated words') == []
        preview = await bot.converse(speaker, preview=True)
        prompt = preview['messages'][0]['content']
        assert 'is Canadian' in prompt and 'he/him' in prompt and 'use recall' in prompt
        assert 'Guest-private' not in prompt
        # Current topics outrank unrelated channel chatter, even when many old facts match it.
        selected = feature.activated_memories(OWNER_ID, CHANNEL_ID, 'goldfish ' * 20, 400,
                                             focus='jasmine tea', people=[])
        assert tea in {item['id'] for item in selected}
        add_turn(bot.db, speaker, json.dumps([{'role': 'assistant', 'content': 'Keep this dialogue.'}]))
        speaker.content = 'Hearthling, my tea is oolong now.'
        result = await bot.execute(json.dumps({'action': 'remember', 'fact': 'Alex drinks oolong tea.', 'memory_id': tea}),
                                   speaker, tool='feature_tool')
        assert result['ok'] and result['result']['memory_id'] == tea
        assert bot.db.execute('SELECT messages FROM turns WHERE request_id=?', (str(speaker.id),)).fetchone()[0] != '[]'
        assert not (await bot.execute(json.dumps({'action': 'remember', 'fact': 'Not mine', 'memory_id': private}),
                                     speaker, tool='feature_tool'))['ok']


async def capture_checks(root):
    async with Hearthling(None, open_database(':memory:'), data_dir=root) as bot:
        message = request(GUEST_ID)
        message.content = "I'm Canadian and my favourite tea is oolong."
        turn = turn_for(message)
        assert bot.memory.enqueue(message, turn)
        assert not bot.memory.enqueue(message, turn)
        response = reply_with(
            fact(f'Guest ({GUEST_ID}) is Canadian.', "I'm Canadian", shared=True),
            fact(f'Guest ({GUEST_ID}) prefers oolong tea.', 'my favourite tea is oolong'),
            fact('Invented residence in Paris.', 'I live in Paris'),
            fact('Wrong account (999999999999999999) is Canadian.', "I'm Canadian", shared=True),
            fact('password: never-store-this', "I'm Canadian"))
        with patch.object(bot.providers, 'reply', new=AsyncMock(return_value=response)) as reply:
            assert await bot.memory.process_pending()
            args, options = reply.call_args
            assert options == {'prefill': False, 'max_tokens': 1500}
            assert 'roleplay' in args[0][0]['content'] and 'curious companion' not in args[0][0]['content']
            assert 'tools' not in options
        assert bot.features.memory_count(GUEST_ID, CHANNEL_ID) == {'personal': 1, 'server': 1}
        assert not await bot.memory.process_pending()
        assert bot.db.execute('SELECT state,source FROM memory_jobs').fetchone() == ('complete', '')
        shared = bot.features.memories(GUEST_ID, CHANNEL_ID, 'Canadian')[0]['id']
        message.id += 1
        message.content = 'Actually, I am Australian.'
        assert bot.memory.enqueue(message, turn_for(message))
        with patch.object(bot.providers, 'reply', new=AsyncMock(return_value=reply_with(
                fact(f'Guest ({GUEST_ID}) is Australian.', 'I am Australian', shared=True, memory_id=shared)))):
            await bot.memory.process_pending()
        assert bot.features.memory_count(GUEST_ID, CHANNEL_ID) == {'personal': 1, 'server': 1}
        assert 'Australian' in bot.db.execute('SELECT fact FROM memories WHERE id=?', (shared,)).fetchone()[0]

        # Invalid JSON retries durably, while a normal chat reply remains unaffected.
        message.id += 1
        message.content = 'My favourite colour is green.'
        assert bot.memory.enqueue(message, turn_for(message))
        with patch.object(bot.providers, 'reply', new=AsyncMock(return_value=ai_reply(content='not JSON'))):
            await bot.memory.process_pending()
        assert bot.db.execute('SELECT state,attempts FROM memory_jobs WHERE request_id=?', (str(message.id),)).fetchone() == ('pending', 1)
        bot.db.execute('UPDATE memory_jobs SET next_at=0')
        bot.db.commit()
        with patch.object(bot.providers, 'reply', new=AsyncMock(return_value=reply_with(
                fact('Guest prefers green.', 'My favourite colour is green')))):
            await bot.memory.process_pending()
        assert bot.features.memories(GUEST_ID, CHANNEL_ID, 'green')

        for change in ({'auto_memory': False}, {'memory_enabled': False}):
            bot.features.update_settings(change, {})
            assert not bot.memory.enqueue(message, turn_for(message))
            bot.features.update_settings({'auto_memory': True, 'memory_enabled': True}, {})
        for content in ('Forget that I drink tea.', 'Do not remember my favourite colour.', 'My password: private-test'):
            message.content = content
            assert not bot.memory.enqueue(message, turn_for(message))
        message.author.bot = True
        message.content = 'My favourite colour is blue.'
        assert not bot.memory.enqueue(message, turn_for(message))
        message.author.bot = False
        message.guild.id = SERVER_ID + 1
        assert not bot.memory.enqueue(message, turn_for(message))

        # Extended provider outages keep the source pending rather than losing the fact after five attempts.
        message.guild.id = SERVER_ID
        message.id += 1
        assert bot.memory.enqueue(message, turn_for(message))
        bot.db.execute('UPDATE memory_jobs SET attempts=5 WHERE request_id=?', (str(message.id),))
        bot.db.commit()
        error = APIConnectionError(request=httpx.Request('POST', 'https://example.invalid/v1'))
        with patch.object(bot.providers, 'reply', new=AsyncMock(side_effect=error)):
            await bot.memory.process_pending()
        assert bot.db.execute('SELECT state FROM memory_jobs WHERE request_id=?', (str(message.id),)).fetchone()[0] == 'pending'

        # Brief answers still count as evidence; nearby questions resolve what '27' is referring to.
        message.id += 1
        message.content = '27'
        bot.memory.contexts[str(message.id)] = [{'speaker': {'id': str(APPLICATION_ID), 'display_name': 'Hearthling'},
                                               'message': 'How old are you?'}]
        assert bot.memory.enqueue(message, turn_for(message))
        source = json.loads(bot.db.execute('SELECT source FROM memory_jobs WHERE request_id=?', (str(message.id),)).fetchone()[0])
        assert source['recent_dialogue'][0]['message'] == 'How old are you?'
        assert str(message.id) not in bot.memory.contexts
        with patch.object(bot.providers, 'reply', new=AsyncMock(return_value=reply_with(
                fact(f'Guest ({GUEST_ID}): age 27 years old.', '27', shared=True)))):
            await bot.memory.process_pending()
        assert bot.features.memories(GUEST_ID, CHANNEL_ID, '27')


async def cancellation_checks(root):
    async with Hearthling(None, open_database(':memory:'), data_dir=root) as bot:
        message = request(GUEST_ID)
        message.content = 'My favourite animal is a cat.'
        known = bot.features.remember(GUEST_ID, CHANNEL_ID, 'Guest likes cats.')['memory_id']
        assert bot.memory.enqueue(message, turn_for(message))
        async def forgotten(*args, **kwargs):
            bot.features.forget(GUEST_ID, CHANNEL_ID, known)
            return reply_with(fact('Guest likes cats.', 'My favourite animal is a cat'))
        with patch.object(bot.providers, 'reply', new=AsyncMock(side_effect=forgotten)):
            await bot.memory.process_pending()
        assert not bot.features.memories(GUEST_ID, CHANNEL_ID)
        assert bot.db.execute('SELECT state FROM memory_jobs').fetchone()[0] == 'cancelled'

        message.id += 1
        kept = bot.features.remember(GUEST_ID, CHANNEL_ID, 'Guest keeps an established fact.')['memory_id']
        assert bot.memory.enqueue(message, turn_for(message))
        other = request(OWNER_ID, identity=message.id + 1)
        other.content = 'My favourite animal is a dog.'
        assert bot.memory.enqueue(other, turn_for(other))
        async def cleared(*args, **kwargs):
            bot.characters.clear_history(GUEST_ID, CHANNEL_ID)
            return reply_with(fact('Guest likes cats.', 'My favourite animal is a cat'))
        with patch.object(bot.providers, 'reply', new=AsyncMock(side_effect=cleared)):
            await bot.memory.process_pending()
        assert {item['id'] for item in bot.features.memories(GUEST_ID, CHANNEL_ID)} == {kept}
        assert bot.db.execute('SELECT state FROM memory_jobs WHERE request_id=?', (str(other.id),)).fetchone()[0] == 'pending'
        async def disabled(*args, **kwargs):
            bot.features.update_settings({'auto_memory': False}, {})
            return reply_with(fact('Owner likes dogs.', 'My favourite animal is a dog'))
        with patch.object(bot.providers, 'reply', new=AsyncMock(side_effect=disabled)):
            await bot.memory.process_pending()
        assert not bot.features.memories(OWNER_ID, CHANNEL_ID)


async def restart_checks(root):
    path = root / 'persistent.sqlite3'
    message = request(GUEST_ID)
    message.content = 'I gave Kirby a blue blanket yesterday.'
    async with Hearthling(None, open_database(path), data_dir=root) as bot:
        assert bot.memory.enqueue(message, turn_for(message))
        bot.db.execute("UPDATE memory_jobs SET state='running'")
        bot.db.commit()
    async with Hearthling(None, open_database(path), data_dir=root) as restarted:
        with patch.object(restarted.memory, 'run', new=AsyncMock()):
            restarted.memory.start()
            await restarted.memory.task
        with patch.object(restarted.providers, 'reply', new=AsyncMock(return_value=reply_with(
                fact('Guest gave Kirby a blue blanket.', 'I gave Kirby a blue blanket yesterday')))):
            assert await restarted.memory.process_pending()
        assert restarted.features.memories(GUEST_ID, CHANNEL_ID, 'blanket')
        assert not restarted.memory.enqueue(message, turn_for(message))


async def shared_checks(root):
    async with Hearthling(None, open_database(':memory:'), data_dir=root) as primary:
        manager = BotManager(primary)
        secondary_id = '900000000000000222'
        with patch('bots.validate_token', new=AsyncMock(return_value=secondary_id)):
            await manager.add({'name': 'Zom', 'server_id': str(SERVER_ID), 'enabled': False, 'token': 'FAKE_TOKEN'})
        secondary = manager.bots[secondary_id]
        secondary.accepting_messages = True
        try:
            assert primary.memory.db is secondary.memory.db and primary.memory.lock is secondary.memory.lock
            message = request(GUEST_ID)
            message.content = 'My favourite tea is jasmine.'
            assert secondary.memory.enqueue(message, turn_for(message))
            assert not primary.memory.enqueue(message, turn_for(message))
            with patch.object(secondary.providers, 'reply', new=AsyncMock(return_value=reply_with(
                    fact('Guest prefers jasmine tea.', 'My favourite tea is jasmine')))):
                await secondary.memory.process_pending()
            assert primary.features.memories(GUEST_ID, CHANNEL_ID + 1, 'jasmine')
        finally:
            await secondary.close()
            await secondary.ai.close()


async def chat_checks(root):
    async with Hearthling(None, open_database(':memory:'), data_dir=root) as bot:
        message = request(GUEST_ID)
        message.content = 'Hearthling, my favourite tea is oolong.'
        message.channel.typing = lambda: AsyncMock()
        with patch.object(bot, 'converse', new=AsyncMock(return_value=('Reply unchanged.', turn_for(message), []))), \
                patch.object(bot, 'deliver', new=AsyncMock()) as deliver:
            assert await bot.process_message(message) == 'complete'
            deliver.assert_awaited_once_with(message, 'Reply unchanged.', [])
        assert bot.db.execute('SELECT state FROM memory_jobs').fetchone()[0] == 'pending'
        message.id += 1
        with patch.object(bot, 'converse', new=AsyncMock(return_value=('Reply still unchanged.', turn_for(message), []))), \
                patch.object(bot.memory, 'enqueue', side_effect=OSError('Simulated storage failure')), \
                patch.object(bot, 'deliver', new=AsyncMock()) as deliver:
            assert await bot.process_message(message) == 'complete'
            deliver.assert_awaited_once_with(message, 'Reply still unchanged.', [])


async def main():
    with tempfile.TemporaryDirectory(prefix='hearthling-memory-', dir='/tmp/opencode') as directory:
        root = Path(directory)
        for test in (recall_checks, capture_checks, cancellation_checks, restart_checks, shared_checks, chat_checks):
            await test(root)
    print('PASS: identity without repeated keywords, nickname changes, focus/budget/privacy, in-place tool corrections, '
          'independent capture/evidence checks, durable retry/restart, cross-bot deduplication and recall, '
          'forget/history-clear/disabled-memory cancellation.')


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
