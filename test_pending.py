"""Offline queue checks: real persistence, retries, cancellation and no repeated effects."""
import asyncio
import base64
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from openai import APIError

import testenv  # noqa: F401
from hearthling import APPLICATION_ID, Hearthling, SERVER_ID, open_database
from pending import QUEUED_NOTICE, RequestQueue
from test_hearthling import ai_reply
from test_bots import CREDENTIALS, fake_ai
from test_features import CHANNEL_ID, GUEST_ID, PNG, http_session, request


async def main():
    with tempfile.TemporaryDirectory(prefix='queue-check-', dir=Path(__file__).resolve().parent) as directory:
        path = Path(directory) / 'queue.sqlite'
        db = open_database(path)
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=directory) as bot:
            bot._connection.user = NS(id=APPLICATION_ID, bot=True, name='Hearthling', display_name='Hearthling')
            bot.features.update_settings({'recent_context': 0, 'expressions_enabled': False, 'reply_stagger': 0}, {})
            messages = {}
            person = request(GUEST_ID).author
            guild = NS(id=SERVER_ID, fetch_member=AsyncMock(return_value=person))
            permissions = NS(view_channel=True, read_message_history=True)
            channel = NS(id=CHANNEL_ID, guild=guild, send=AsyncMock(), typing=MagicMock(),
                         permissions_for=MagicMock(return_value=permissions),
                         fetch_message=AsyncMock(side_effect=lambda identity: messages[identity]))

            def message():
                item = request(GUEST_ID, 900000000000010000 + len(messages))
                item.author, item.channel, item.guild = person, channel, guild
                messages[item.id] = item
                return item

            def state(item):
                return db.execute('SELECT state FROM request_queue WHERE request_id=? ORDER BY rowid DESC', (str(item.id),)).fetchone()[0]

            async def tick():
                db.execute("UPDATE request_queue SET due_at=0 WHERE state='pending'")
                db.commit()
                await bot.social.tick()

            with patch.object(bot, 'get_channel', return_value=channel), patch.object(bot, 'is_ready', return_value=True):
                first = message()
                bot.active_users.add(GUEST_ID)
                await bot.on_message(first)
                await bot.on_message(first)
                assert state(first) == 'pending' and channel.send.await_count == 0
                with sqlite3.connect(path) as saved:
                    assert saved.execute('SELECT count(*) FROM request_queue').fetchone()[0] == 1
                bot.social.queue = RequestQueue(bot)
                bot.active_users.clear()
                await tick()
                assert state(first) == 'complete' and channel.send.await_count == 1
                await tick()
                assert channel.send.await_count == 1 and not bot.active_users

                quiet = message()
                with patch.object(bot.social, 'available', return_value=False):
                    await bot.on_message(quiet)
                    await bot.social.tick()
                assert state(quiet) == 'pending' and channel.send.await_count == 1
                await tick()
                assert state(quiet) == 'complete'

                failure = APIError('Temporary connection failure', NS(), body=None)
                outage = message()
                with patch.object(bot.ai.chat.completions, 'create', side_effect=failure), patch('hearthling.logging.warning'):
                    await bot.on_message(outage)
                    assert state(outage) == 'pending'
                    notices = channel.send.await_count
                    await tick()
                    assert state(outage) == 'pending' and channel.send.await_count == notices
                    assert db.execute('SELECT due_at FROM request_queue WHERE request_id=?', (str(outage.id),)).fetchone()[0] > time.time()
                await tick()
                assert state(outage) == 'complete'

                picture = message()
                args = '{"action":"generate_image","prompt":"A cup of tea"}'
                async with bot.features.image_lock:
                    result = await bot.execute(args, picture, tool='feature_tool')
                    assert result['queued'] and not result['outcome_may_be_uncertain']
                    assert await bot.execute(args, picture, tool='feature_tool') == result
                assert db.execute('SELECT count(*) FROM request_queue WHERE request_id=?', (str(picture.id),)).fetchone()[0] == 1
                payload = {'output': [{'type': 'image_generation_call', 'result': base64.b64encode(PNG).decode()}]}
                session = http_session(payload)
                with patch('features.aiohttp.ClientSession', return_value=session), \
                        patch.object(bot.ai.chat.completions, 'create', side_effect=failure):
                    await tick()
                assert state(picture) == 'pending'
                assert db.execute('SELECT state FROM pictures WHERE request_id=?', (str(picture.id),)).fetchone()[0] == 'complete'
                session.post.assert_called_once()
                # Simulate a restart after the effect completed, before the final report was ready.
                db.execute("UPDATE request_queue SET state='running' WHERE request_id=?", (str(picture.id),))
                db.commit()
                bot.social.queue = RequestQueue(bot)
                bot.social.queue.recover()
                with patch.object(bot.features, 'perform', wraps=bot.features.perform) as perform:
                    await tick()
                    perform.assert_not_awaited()
                assert state(picture) == 'complete'
                assert not bot.ai.chat.completions.create.call_args.kwargs.get('tools')
                assert 'queued_result' in bot.ai.chat.completions.create.call_args.kwargs['messages'][0]['content']
                assert 'file' in channel.send.call_args_list[-2].kwargs
                sent_prompt = session.post.call_args.kwargs['json']['input'][0]['content'][0]['text']
                assert sent_prompt == 'A cup of tea, anime style, cel-shaded, flat colors, clean wordless artwork'
                assert channel.send.call_args.args[0] == f'Prompt sent to the image model:\n```\n{sent_prompt}\n```'
                assert channel.send.call_args.kwargs['allowed_mentions'].users is False

                limited = message()
                with patch('features.aiohttp.ClientSession', return_value=http_session({'error': 'Rate limit'}, 429)):
                    result = await bot.execute(args, limited, tool='feature_tool')
                assert result['queued'] and state(limited) == 'pending'
                assert db.execute('SELECT state FROM pictures WHERE request_id=?', (str(limited.id),)).fetchone()[0] == 'retryable'
                with patch('features.aiohttp.ClientSession', return_value=http_session(payload)):
                    await tick()
                assert state(limited) == 'complete'

                interrupted = message()
                broken = http_session(payload)
                broken.post.side_effect = asyncio.TimeoutError()
                with patch('features.aiohttp.ClientSession', return_value=broken):
                    result = await bot.execute(args, interrupted, tool='feature_tool')
                assert result['outcome_may_be_uncertain'] and not result.get('queued')
                assert not db.execute('SELECT 1 FROM request_queue WHERE request_id=?', (str(interrupted.id),)).fetchone()
                with patch.object(bot.features, 'perform', wraps=bot.features.perform) as perform:
                    assert await bot.execute(args, interrupted, tool='feature_tool', retry=True) == result
                    perform.assert_not_awaited()

                edited = message()
                bot.social.queue.add(edited)
                edited.content += ' Actually, never mind.'
                await tick()
                assert state(edited) == 'cancelled'
                removed = message()
                bot.social.queue.add(removed)
                with patch.object(channel, 'fetch_message', side_effect=discord.NotFound(NS(status=404, reason='Not Found'), 'Deleted')):
                    await tick()
                assert state(removed) == 'cancelled'
                revoked = message()
                bot.social.queue.add(revoked)
                permissions.view_channel = False
                await tick()
                assert state(revoked) == 'cancelled'
                permissions.view_channel = True
                paused = message()
                bot.social.queue.add(paused)
                bot.features.update_settings({'server_paused': True}, {})
                await tick()
                assert state(paused) == 'pending'
                bot.features.update_settings({'server_paused': False}, {})
                await tick()
                assert state(paused) == 'complete'

                # A slash command resumes using a saved public reply, never an expired interaction token.
                anchor = message()
                anchor.author = bot.user
                slash = NS(id=anchor.id + 1000, user=person, guild=guild, guild_id=SERVER_ID, channel=channel,
                           created_at=anchor.created_at, response=NS(defer=AsyncMock()),
                           original_response=AsyncMock(return_value=anchor), edit_original_response=AsyncMock())
                bot.active_users.add(GUEST_ID)
                command = bot.command_tree.get_command('interact', guild=discord.Object(SERVER_ID))
                await command.callback(slash, 'Tell me about tea.')
                assert state(slash) == 'pending'
                slash.edit_original_response.assert_not_awaited()
                bot.active_users.clear()
                await tick()
                assert state(slash) == 'complete'
                assert channel.send.call_args.kwargs['reference'].message_id == anchor.id

                uncertain = message()
                bot.social.queue.add(uncertain)
                db.execute("UPDATE request_queue SET state='delivering' WHERE request_id=?", (str(uncertain.id),))
                db.commit()
                bot.social.queue.recover()
                await tick()
                assert state(uncertain) == 'uncertain' and not bot.active_users
        db.close()
    print('PASS: durable busy/quiet/chat/slash queues, image retries, restart recovery, no repeated effects, access checks and cancellation.')


async def notice_timing():
    with tempfile.TemporaryDirectory() as directory:
        db, other_db = open_database(':memory:'), open_database(':memory:')
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=directory) as bot, \
                Hearthling(fake_ai(), other_db, credentials=CREDENTIALS, data_dir=directory, bot_id=APPLICATION_ID + 1) as other:
            other.social.queue.notice_db = db
            person = request(GUEST_ID).author
            guild = NS(id=SERVER_ID, fetch_member=AsyncMock(return_value=person))
            messages = {}
            channel = NS(id=CHANNEL_ID, guild=guild, send=AsyncMock(), typing=MagicMock(),
                         permissions_for=MagicMock(return_value=NS(view_channel=True, read_message_history=True)),
                         fetch_message=AsyncMock(side_effect=lambda identity: messages[identity]))
            def message():
                item = request(GUEST_ID, 900000000000020000 + len(messages))
                item.author, item.channel, item.guild = person, channel, guild
                messages[item.id] = item
                return item
            for account in (bot, other):
                account.features.update_settings({'recent_context': 0, 'expressions_enabled': False, 'reply_stagger': 0}, {})
                account.get_channel = MagicMock(return_value=channel)
                account.is_ready = MagicMock(return_value=True)
                account.active_users.add(GUEST_ID)
            with patch('pending.time.time', return_value=1800000000) as clock:
                first, second = message(), message()
                await bot.on_message(first)
                await other.on_message(second)
                assert not channel.send.await_count
                await bot.social.queue.notices(clock.return_value + 299)
                assert not channel.send.await_count
                clock.return_value += 300
                # A busy bot must still deliver its timer, without another user message.
                await asyncio.gather(bot.social.queue.notices(clock.return_value), other.social.queue.notices(clock.return_value))
                assert channel.send.await_count == 1 and channel.send.call_args.args[0] == QUEUED_NOTICE
                bot.social.queue = RequestQueue(bot)
                clock.return_value += 299
                await bot.social.queue.notices(clock.return_value)
                await other.social.queue.notices(clock.return_value)
                assert channel.send.await_count == 1
                clock.return_value += 1
                await other.social.queue.notices(clock.return_value)
                assert channel.send.await_count == 2
                clock.return_value += 600
                await bot.social.queue.notices(clock.return_value)
                await other.social.queue.notices(clock.return_value)
                assert channel.send.await_count == 2  # Each saved request is announced at most once.
                bot.social.queue.add(first, tool='feature_tool', arguments='{}')
                assert db.execute('SELECT notice_due FROM request_queue WHERE request_id=? AND tool<>?', (str(first.id), '')).fetchone()[0] is None

                answered, waiting = message(), message()
                bot.social.queue.add(answered)
                other.social.queue.add(waiting)
                clock.return_value += 290
                await bot.deliver(answered, 'Here is your answer.', [])
                clock.return_value += 10
                await bot.social.queue.notices(clock.return_value)
                await other.social.queue.notices(clock.return_value)
                assert channel.send.await_count == 3
                clock.return_value += 300
                await bot.social.queue.notices(clock.return_value)
                await other.social.queue.notices(clock.return_value)
                assert channel.send.await_count == 4  # Only the still-unanswered request remains eligible.

                # A queued tool stops before the model can generate an immediate queue announcement.
                tool_request = message()
                tool = MagicMock(id='picture-call', function=NS(name='feature_tool', arguments='{"action":"generate_image","prompt":"Tea"}'))
                tool.model_dump.return_value = {'id': tool.id, 'type': 'function', 'function': vars(tool.function)}
                answer = ai_reply()
                answer.choices[0].message.tool_calls = [tool]
                with patch.object(bot.ai.chat.completions, 'create', return_value=answer) as reply:
                    async with bot.features.image_lock:
                        await bot.process_message(tool_request)
                    assert reply.await_count == 1 and channel.send.await_count == 4
                clock.return_value += 300
                await bot.social.queue.notices(clock.return_value)
                assert channel.send.await_count == 5
                assert bot.active_users == set() and other.active_users == {GUEST_ID}
                bot.active_users.add(GUEST_ID)
                with patch.object(bot.social.queue, 'notices', new=AsyncMock()) as notices:
                    bot.social.start()
                    await asyncio.sleep(0)
                    notices.assert_awaited_once()
                    await bot.social.close()
        db.close()
        other_db.close()
    print('PASS: five-minute first notice, background busy delivery, shared cooldown, restart persistence, answered suppression and silent queued tools.')


if __name__ == '__main__':
    asyncio.run(main())
    asyncio.run(notice_timing())
