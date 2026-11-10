"""Direct feature names keep validation, permissions and replay protection."""
import testenv
import asyncio
import json
import tempfile
from unittest.mock import AsyncMock, patch

from hearthling import Hearthling, open_database
from test_hearthling import ai_reply
from test_bots import CREDENTIALS, fake_ai
from test_features import GUEST_ID, request


def tool_reply(name, arguments, identity='direct-call'):
    response = ai_reply(calls=1)
    call = response.choices[0].message.tool_calls[0]
    call.id, call.function.name = identity, name
    call.function.arguments = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return response


def results(turn):
    return [json.loads(item['content']) for item in turn if item['role'] == 'tool']


async def main():
    with tempfile.TemporaryDirectory() as directory:
        db = open_database(':memory:')
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=directory) as bot:
            bot.features.settings.recent_context = 0
            guest = request(GUEST_ID)
            responses = [
                tool_reply('recall', {'member': str(GUEST_ID)}, 'recall-call'),
                tool_reply('generate_image', {'prompt': 'A cup of tea', 'subjects': [str(GUEST_ID)]}, 'image-call'),
                tool_reply('feature_tool', {'action': 'generate_image', 'prompt': 'A cup of tea', 'subjects': [str(GUEST_ID)]}, 'repeat-call'),
                tool_reply('web_fetch', {'action': 'web_fetch', 'url': 'https://example.org/article'}, 'fetch-call'),
                ai_reply(content='Done.'),
            ]
            bot.ai.chat.completions.create = AsyncMock(side_effect=responses)
            with patch.object(bot.features, 'perform', new=AsyncMock(return_value={'confirmed': True})) as perform:
                text, turn, receipts = await bot.converse(guest)
                assert text == 'Done.' and not receipts
                assert all(result['ok'] for result in results(turn))
                assert [call.args[0].action for call in perform.await_args_list] == ['recall', 'generate_image', 'web_fetch']
                assert perform.await_args_list[1].args[0].subjects == [str(GUEST_ID)]
                assert results(turn)[1] == results(turn)[2]
                assert db.execute('SELECT count(*) FROM actions').fetchone()[0] == 3
                assert [item['tool_call_id'] for item in turn if item['role'] == 'tool'] == [
                    'recall-call', 'image-call', 'repeat-call', 'fetch-call']
                assert [item['tool_calls'][0]['function']['name'] for item in turn if item.get('tool_calls')] == [
                    'recall', 'generate_image', 'feature_tool', 'web_fetch']

            # Malformed or contradictory arguments never reach an action.
            for arguments in ('{', '[]', 'null', '{"action":"web_search","query":"tea"}',
                              '{"prompt":"Tea","size":"99999x99999"}', '{"prompt":"Tea","owner_id":"123"}'):
                bot.ai.chat.completions.create = AsyncMock(side_effect=[
                    tool_reply('generate_image', arguments), ai_reply(content='Unable to do that.')])
                with patch.object(bot.features, 'perform', new=AsyncMock()) as perform:
                    _, turn, _ = await bot.converse(request(GUEST_ID, guest.id + 1))
                    assert not results(turn)[0]['ok']
                    perform.assert_not_awaited()

            settings = bot.features.settings.model_copy()
            for changes, name in (({'images_enabled': False}, 'generate_image'),
                                  ({'public_images': False}, 'generate_image'),
                                  ({'bot_mode': 'chatbot'}, 'generate_image'),
                                  ({}, 'host_status'), ({}, 'ban'), ({}, 'invented_tool')):
                bot.features.settings = settings.model_copy(update=changes)
                bot.ai.chat.completions.create = AsyncMock(side_effect=[
                    tool_reply(name, {}), ai_reply(content='Unavailable.')])
                with patch.object(bot.features, 'perform', new=AsyncMock()) as perform:
                    _, turn, _ = await bot.converse(request(GUEST_ID, guest.id + 2))
                    assert not results(turn)[0]['ok']
                    perform.assert_not_awaited()

            # Permissions can change after tools were offered; execute rechecks them.
            bot.features.settings = settings.model_copy()
            async def revoke(*args, **kwargs):
                if not bot.features.settings.public_images:
                    return ai_reply(content='Unavailable.')
                bot.features.settings.public_images = False
                return tool_reply('generate_image', {'prompt': 'Tea'})

            bot.ai.chat.completions.create = AsyncMock(side_effect=revoke)
            with patch.object(bot.features, 'perform', new=AsyncMock()) as perform:
                _, turn, _ = await bot.converse(request(GUEST_ID, guest.id + 3))
                assert 'disabled' in results(turn)[0]['error']
                perform.assert_not_awaited()

            # A direct remember call also honours a memory deletion during the reply.
            bot.features.settings = settings.model_copy()
            async def clear_memories(*args, **kwargs):
                if bot.features.memory_epoch:
                    return ai_reply(content='Unavailable.')
                bot.features.memory_epoch += 1
                return tool_reply('remember', {'fact': 'The speaker likes tea.'})

            bot.ai.chat.completions.create = AsyncMock(side_effect=clear_memories)
            with patch.object(bot.features, 'perform', new=AsyncMock()) as perform:
                _, turn, _ = await bot.converse(request(GUEST_ID, guest.id + 4))
                assert 'Memories were deleted' in results(turn)[0]['error']
                perform.assert_not_awaited()

            # Automatic conversations have no feature tool and accept no aliases.
            bot.ai.chat.completions.create = AsyncMock(side_effect=[
                tool_reply('generate_image', {'prompt': 'Tea'}), ai_reply(content='Unavailable.')])
            with patch.object(bot.features, 'perform', new=AsyncMock()) as perform:
                _, turn, _ = await bot.converse(request(GUEST_ID, guest.id + 5), automatic='reminder')
                assert not results(turn)[0]['ok']
                perform.assert_not_awaited()
        db.close()
    print('PASS: direct feature calls, canonical replay, tool history, argument validation, permissions and memory deletion.')


if __name__ == '__main__':
    asyncio.run(main())
