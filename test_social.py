"""Offline checks for unattended behaviour, authority, timing and durable reminders."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import discord
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import testenv  # noqa: F401
from hearthling import APPLICATION_ID, Hearthling, OWNER_ID, SERVER_ID, open_database
from bots import BotManager
from features import FeatureAction, Settings
from social import RANDOM_STATE_CHANCE, Social
from test_hearthling import ai_reply, expect_error
from test_bots import CREDENTIALS, fake_ai
from test_features import CHANNEL_ID, GUEST_ID, PNG, http_session, request


async def main():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix='social-check-') as directory:
        db = open_database(':memory:')
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=directory) as bot:
            bot._connection.user = NS(id=APPLICATION_ID, bot=True, name='Hearthling', display_name='Hearthling')
            manager = BotManager(bot)
            peer_id = 900000000000000222
            manager.bots[str(peer_id)] = NS(bot_id=peer_id, server_id=SERVER_ID, character_name='Alfred')
            config = bot.features
            assert not config.settings.spontaneous_enabled and not config.settings.dm_followups_enabled and not config.settings.random_state_enabled
            for changes in ({'spontaneous_channels': ['../secret']}, {'timezone': 'Not/AZone'},
                            {'spontaneous_min_minutes': 9, 'spontaneous_max_minutes': 2},
                            {'random_state_channels': ['../secret']}, {'random_state_enabled': True},
                            {'unavailable_windows': [{'days': [9], 'start': '23:00', 'end': '06:00'}]}):
                expect_error(ValueError, lambda changes=changes: Settings.model_validate(config.settings.model_dump() | changes))
            guest = request(GUEST_ID, 900000000000000501)
            guest.author.name = 'guest'
            guest.content = '<:Hearthling:900000000000000011>'
            assert not bot.social.consider(guest)
            config.update_settings({'nicknames': ['Companion H'], 'reply_stagger': 0}, {})
            guest.content = 'Companion H, hello'
            assert bot.social.consider(guest)
            config.update_settings({'channel_blacklist': [str(CHANNEL_ID)]}, {})
            assert not bot.addressed(guest)
            config.update_settings({'channel_blacklist': [], 'ignored_users': [str(GUEST_ID)]}, {})
            assert not bot.social.consider(guest)
            config.update_settings({'ignored_users': [], 'channel_rules': {str(CHANNEL_ID): {'enabled': True, 'chance': 50, 'cooldown': 20}}}, {})
            guest.content = 'An ordinary message.'
            bot.social.last_reply.clear()
            with patch('social.random.random', return_value=0):
                assert bot.social.consider(guest)
                guest.id += 1
                assert not bot.social.consider(guest)
            guest.content = 'Hearthling, please reply.'
            assert bot.social.consider(guest)  # Explicit addressing does not wait for the random-reply cooldown.
            bot_message = request(peer_id, guest.id + 1)
            bot_message.author.bot = True
            bot_message.author.name = bot_message.author.display_name = 'Alfred'
            bot_message.content = 'Hearthling, would you like tea?'
            with patch('social.random.random', return_value=0):
                assert bot.social.consider(bot_message)
                assert bot.addressed(bot_message)
                assert not config.allowed_actions(bot_message)
                _, turns, receipts = await bot.converse(bot_message)
                payload = bot.ai.chat.completions.create.call_args.kwargs
                assert not payload.get('tools') and not receipts
                assert 'a conversation with another participant' in payload['messages'][0]['content']
                assert not (await bot.execute('{"action":"server_info"}', bot_message))['ok']
                bot.social.stop_interactions()
                assert not bot.social.consider(bot_message) and not bot.addressed(bot_message)
                bot.social.stop_interactions(False)
                manager.bot_chains[(SERVER_ID, CHANNEL_ID)] = (bot_message.id - 1, 1)
                assert not bot.social.consider(bot_message)
                manager.bot_chains.clear()
                assert not bot.social.consider(bot_message)  # The reservation survives resetting in-memory state.
                original_id = bot_message.id
                guest.id = bot_message.id + 1
                assert bot.social.consider(guest)
                bot_message.id = guest.id + 1
                bot_message.reference = discord.MessageReference(message_id=original_id, channel_id=CHANNEL_ID)
                assert not bot.social.consider(bot_message)  # A human cannot restart an old bot-to-bot reply.
                bot_message.reference = None
                assert bot.social.consider(bot_message)
                assert not bot.social.consider(bot_message)  # The only slot is reserved synchronously.
                bot_message.author.id += 1
                assert not bot.social.consider(bot_message)  # Unmanaged bot.
            bot_message.author.id = peer_id
            guest.id = bot_message.id + 1
            assert bot.social.consider(guest)
            bot_message.id = guest.id + 1
            with patch('social.random.random', return_value=0.10):
                assert not bot.social.consider(bot_message)
            with patch('social.random.random', return_value=0.099):
                assert bot.social.consider(bot_message)
            config.update_settings({'split_replies': True, 'bot_start_chance': 100, 'bot_turn_limit': 30}, {})
            assert 'bot_start_chance' not in config.settings.model_dump()
            await bot.deliver(bot_message, 'Tea.\n\n' + 'More tea. ' * 500 + '[STICKER: 123]', [])
            bot_message.channel.send.assert_awaited_once()
            assert len(bot_message.channel.send.call_args.args[0]) <= 1900
            assert not bot.social.identity_ok('Alfred: I am speaking now.')
            assert bot.social.identity_ok('Alfred asked about tea. I would prefer coffee.')
            config.update_settings({'unavailable_windows': [{'days': [0], 'start': '23:00', 'end': '06:00'}]}, {})
            stamp = lambda text: datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
            assert not bot.social.available(stamp('2026-09-14T23:30:00'))
            assert not bot.social.available(stamp('2026-09-15T05:00:00'))
            assert bot.social.available(stamp('2026-09-15T06:00:00'))
            config.update_settings({'unavailable_windows': []}, {})
            assert bot.social.set_zone(GUEST_ID, 'Asia/Manila') == 'Asia/Manila'
            assert bot.social.zone(OWNER_ID) == 'UTC'
            due = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            reminder = bot.social.add_reminder(guest, due, 'Check the oven.')
            assert bot.social.reminder_list(GUEST_ID)[0]['message'] == 'Check the oven.'
            expect_error(PermissionError, lambda: bot.social.add_reminder(guest, due, 'Private', dm=True))
            expect_error(ValueError, lambda: bot.social.add_reminder(guest, due, CREDENTIALS['ai_api_key']))
            expect_error(ValueError, lambda: bot.social.cancel_reminder(OWNER_ID, reminder['id']))
            assert bot.social.cancel_reminder(GUEST_ID, reminder['id'])['cancelled'] == reminder['id']
            private = bot.social.add_reminder(request(), due, 'A private owner reminder.', dm=True)
            owner_dm = request()
            owner_dm.guild = None
            listed = await config.perform(FeatureAction(action='list_reminders'), owner_dm)
            assert any(item['id'] == private['id'] for item in listed['reminders'])
            public_list = await config.perform(FeatureAction(action='list_reminders'), request())
            assert all(item['id'] != private['id'] for item in public_list['reminders'])
            bot.social.cancel_reminder(OWNER_ID, private['id'])
            first = bot.social.add_reminder(guest, due, 'A one-time reminder.')
            db.execute('UPDATE reminders SET due_at=? WHERE id=?', (time.time() - 1, first['id']))
            db.commit()
            with patch.object(bot, 'is_ready', return_value=True), patch.object(bot.social, 'deliver_reminder', new=AsyncMock(return_value=True)) as send:
                config.update_settings({'server_paused': True}, {})
                await bot.social.tick()
                send.assert_not_awaited()
                config.update_settings({'server_paused': False}, {})
                await bot.social.tick()
                await bot.social.tick()
                send.assert_awaited_once()
            db.execute("UPDATE reminders SET state='sending' WHERE id=?", (first['id'],))
            db.commit()
            reloaded = Social(bot)
            assert next(item for item in reloaded.reminder_list(GUEST_ID) if item['id'] == first['id'])['state'] == 'sending'
            reloaded.start()
            await reloaded.close()
            assert next(item for item in reloaded.reminder_list(GUEST_ID) if item['id'] == first['id'])['state'] == 'uncertain'
            assert reloaded.zone(GUEST_ID) == 'Asia/Manila'
            channel = NS(id=CHANNEL_ID, guild=NS(id=SERVER_ID), send=AsyncMock())
            config.update_settings({'recent_context': 0, 'spontaneous_enabled': True,
                                    'spontaneous_channels': [str(CHANNEL_ID)], 'spontaneous_silence_minutes': 0,
                                    'spontaneous_min_minutes': 1, 'spontaneous_max_minutes': 1}, {})
            with patch.object(bot, 'is_ready', return_value=True), patch.object(bot, 'get_channel', return_value=channel), \
                    patch.object(bot.social, 'speak', new=AsyncMock(return_value=True)) as speak:
                now = time.time()
                await bot.social.tick(now)
                speak.assert_not_awaited()
                await bot.social.tick(now + 61)
                speak.assert_awaited_once()
                config.update_settings({'server_paused': True}, {})
                await bot.social.tick(now + 122)
                speak.assert_awaited_once()
                config.update_settings({'server_paused': False, 'spontaneous_enabled': False}, {})
            owner = request().author
            owner.name = owner.display_name
            dm = NS(id=CHANNEL_ID + 1, guild=None, send=AsyncMock())
            with patch.object(bot, 'converse', wraps=bot.converse), patch.object(bot.ai.chat.completions, 'create', new=AsyncMock(return_value=ai_reply(content='Tea later?'))) as generate:
                assert await bot.social.speak(dm, 'Follow up naturally.', person=owner, kind='dm_followup')
                assert not generate.call_args.kwargs.get('tools')
                dm.send.assert_awaited_once()
                assert not bot.active_users
                payload = {'output': [{'type': 'image_generation_call', 'result': base64.b64encode(PNG).decode()}]}
                with patch('features.aiohttp.ClientSession', return_value=http_session(payload)), patch('social.random.random', return_value=0):
                    assert await bot.social.speak(dm, 'A small follow-up.', person=owner, kind='dm_followup', picture=True)
                assert not generate.call_args.kwargs.get('tools')
                picture = dm.send.call_args_list[-2].kwargs['file']
                assert picture.filename.endswith('.png') and picture.fp.closed
                assert db.execute("SELECT count(*) FROM pictures WHERE state='complete'").fetchone()[0] == 1
                sent_prompt = db.execute("SELECT prompt FROM pictures WHERE state='complete'").fetchone()[0]
                assert sent_prompt.startswith(bot.social.config.dm_followup_image_prompt) and '\nRecent context: ' in sent_prompt
                assert dm.send.call_args.args[0] == f'Prompt sent to the image model:\n```\n{sent_prompt}\n```'
            config.update_settings({'dm_followups_enabled': True, 'dm_followup_after_minutes': 1}, {})
            db.execute('UPDATE activity SET last_in=?,last_out=0,last_followup=0,followups=0 WHERE channel_id=?',
                       (time.time() - 200, str(dm.id)))
            db.commit()
            owner.create_dm = AsyncMock(return_value=dm)
            with patch.object(bot, 'is_ready', return_value=True), patch.object(bot, 'fetch_user', new=AsyncMock(return_value=owner)), \
                    patch.object(bot.social, 'speak', new=AsyncMock(return_value=True)) as follow:
                await bot.social.tick()
                await bot.social.tick()
                follow.assert_awaited_once()
        db.close()
    await random_replies()
    await random_states()
    print('PASS: access/nickname gates, managed bot dialogue without tools, hard conversation stop, channel cooldowns, overnight schedules, private timezones, durable reminders, random speech, owner-only follow-ups, 0.1% unprompted replies and 0.03% spontaneous images.')


async def random_replies():
    quiet_channels = ('900000000000000901', '900000000000000902')
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix='social-check-') as directory:
        db = open_database(':memory:')
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=directory) as bot:
            bot._connection.user = NS(id=APPLICATION_ID, bot=True, name='Hearthling', display_name='Hearthling')
            manager = BotManager(bot)
            peer = NS(bot_id=900000000000000222, server_id=SERVER_ID, character_name='Alfred', accepting_messages=True)
            manager.bots[str(peer.bot_id)] = peer
            peer.social = Social(NS(db=open_database(':memory:'), manager=manager, bot_id=peer.bot_id, server_id=SERVER_ID))
            bot.features.update_settings({'reply_stagger': 0, 'quiet_channels': list(quiet_channels)}, {})
            base = 900000000000100000
            messages = [request(GUEST_ID, base + offset) for offset in range(30000)]
            for message in messages:
                message.content = 'Just chatting about lunch.'
            picks = [[social.random_pick(message) for social in (bot.social, peer.social)] for message in messages]
            hits = [pair for pair in picks if any(pair)]
            assert all(sum(pair) == 1 for pair in hits)  # One shared roll: never two bots at once.
            assert 10 <= len(hits) <= 60 and any(pair[0] for pair in hits) and any(pair[1] for pair in hits)
            chosen = next(message for message, pair in zip(messages, picks) if pair[0])
            missed = next(message for message, pair in zip(messages, picks)
                          if not any(pair) and not bot.social.random_pick(message, image=True))
            bot.social.last_reply[str(CHANNEL_ID)] = time.time()  # No channel rule and a fresh cooldown do not block it.
            assert bot.social.consider(chosen) and bot.social.admitted[str(chosen.id)]
            assert 'not addressed to you' in bot.social.unprompted_note(chosen)
            assert not bot.social.consider(missed) and not bot.social.unprompted_note(missed)
            for skipped in quiet_channels:
                chosen.channel.id = int(skipped)
                assert not bot.social.random_pick(chosen)
            chosen.channel.id = CHANNEL_ID
            chosen.author.bot = True
            assert not bot.social.random_pick(chosen)
            chosen.author.bot = False
            peer.accepting_messages = False
            assert all(bot.social.random_pick(message) == bool(any(pair)) for message, pair in zip(messages, picks))
            peer.accepting_messages = True
            peer.features = NS(allowed_actions=lambda message: bot.features.allowed_actions(message),
                               settings=bot.features.settings)
            for image, chance in ((False, 0.001), (True, 0.0003)):
                with patch('social.random.Random') as randomiser:
                    randomiser.return_value.choice.return_value = bot.bot_id
                    randomiser.return_value.random.return_value = chance - 0.0000001
                    assert bot.social.random_pick(messages[0], image=image)
                    randomiser.return_value.random.return_value = chance
                    assert not bot.social.random_pick(messages[0], image=image)
            image_picks = [[social.random_pick(message, image=True) for social in (bot.social, peer.social)]
                           for message in messages]
            image_hits = [pair for pair in image_picks if any(pair)]
            assert 3 <= len(image_hits) <= 20 and all(sum(pair) == 1 for pair in image_hits)
            image_message = next(message for message, pair in zip(messages, image_picks)
                                 if pair[0] and not bot.social.random_pick(message))
            assert bot.social.consider(image_message)
            assert 'Call generate_image once' in bot.social.unprompted_note(image_message)
            with patch('hearthling.shared_history', new=AsyncMock(return_value=[])), \
                    patch.object(bot.ai.chat.completions, 'create', new=AsyncMock(return_value=ai_reply(content='A little inspiration.'))) as generate:
                await bot.converse(image_message)
            system = generate.call_args.kwargs['messages'][0]['content']
            assert 'Call generate_image once' in system and 'or when the shared random image roll' in system
            for changes in ({'images_enabled': False}, {'images_enabled': True, 'public_images': False},
                            {'public_images': True, 'bot_mode': 'chatbot'}, {'bot_mode': 'assistant', 'server_paused': True}):
                bot.features.update_settings(changes, {})
                assert not bot.social.random_pick(image_message, image=True)
            bot.features.update_settings({'server_paused': False}, {})
            for skipped in quiet_channels:
                image_message.channel.id = int(skipped)
                assert not bot.social.random_pick(image_message, image=True)
            image_message.channel.id = CHANNEL_ID
            image_message.author.bot = True
            assert not bot.social.random_pick(image_message, image=True)
            image_message.author.bot = False
            image_message.guild = None
            assert not bot.social.random_pick(image_message, image=True)
        db.close()


async def random_states():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix='social-state-check-') as directory:
        db = open_database(':memory:')
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=directory) as bot:
            bot._connection.user = NS(id=APPLICATION_ID, bot=True, name='Hearthling', display_name='Hearthling')
            manager = BotManager(bot)
            peer = NS(bot_id=900000000000000222, server_id=SERVER_ID, accepting_messages=True)
            manager.bots[str(peer.bot_id)] = peer
            channel = NS(id=CHANNEL_ID, guild=NS(id=SERVER_ID), send=AsyncMock())
            peer_settings = Settings.model_validate(bot.features.settings.model_dump() | {
                'random_state_enabled': True, 'random_state_channels': [str(CHANNEL_ID)],
                'random_state_silence_minutes': 0, 'recent_context': 0})
            peer.social = Social(NS(db=open_database(':memory:'), manager=manager, bot_id=peer.bot_id,
                                    server_id=SERVER_ID, features=NS(settings=peer_settings),
                                    accepting_messages=True, get_channel=lambda identity: channel))
            config = bot.features
            config.update_settings({'random_state_enabled': True, 'random_state_channels': [str(CHANNEL_ID)],
                                    'random_state_silence_minutes': 0, 'recent_context': 0}, {})
            with patch.object(bot.social, 'speak', new=AsyncMock(return_value=True)) as speak, \
                    patch.object(peer.social, 'speak', new=AsyncMock(return_value=True)) as peer_speak, \
                    patch.object(bot, 'get_channel', return_value=channel):
                base = time.time() - 40000 * 3600
                total = 0
                for hour in range(40000):
                    before = speak.await_count + peer_speak.await_count
                    await bot.social.random_state_tick(base + hour * 3600)
                    await peer.social.random_state_tick(base + hour * 3600)
                    # One shared roll per hour: at most one winner posts, and both bots agree on who.
                    assert speak.await_count + peer_speak.await_count - before <= 1
                    total = speak.await_count + peer_speak.await_count
                assert 2 <= total <= 60
                assert speak.await_count and peer_speak.await_count
            hour = time.time() // 3600 * 3600 + 7200
            with patch.object(bot.social, 'speak', new=AsyncMock(return_value=True)) as speak, \
                    patch.object(peer.social, 'speak', new=AsyncMock(return_value=True)), \
                    patch.object(bot, 'get_channel', return_value=channel), \
                    patch('social.random.Random') as randomiser:
                randomiser.return_value.random.return_value = RANDOM_STATE_CHANCE / 100
                await bot.social.random_state_tick(hour)
                speak.assert_not_awaited()  # Exactly at the threshold is a miss.
                randomiser.return_value.random.return_value = RANDOM_STATE_CHANCE / 100 - 0.0000001
                randomiser.return_value.choice.return_value = peer.bot_id
                await bot.social.random_state_tick(hour)
                speak.assert_not_awaited()  # The peer won the shared roll, not this bot.
                randomiser.return_value.choice.return_value = bot.bot_id
                await bot.social.random_state_tick(hour)
                speak.assert_awaited_once()
                assert speak.await_args.args[0] is channel
                assert speak.await_args.args[1] == config.settings.random_state_prompt
                assert speak.await_args.kwargs['kind'] == 'random state'
                await bot.social.random_state_tick(hour)
                speak.assert_awaited_once()  # The recorded win blocks a second post in the same hour.
                db.execute('INSERT INTO activity (user_id,channel_id,last_in) VALUES (?,?,?)', ('1', str(CHANNEL_ID), hour + 3600 - 60))
                db.commit()
                config.update_settings({'random_state_silence_minutes': 30}, {})
                await bot.social.random_state_tick(hour + 3600)
                speak.assert_awaited_once()  # A channel with recent chat stays quiet.
                db.execute('DELETE FROM activity')
                db.commit()
                await bot.social.random_state_tick(hour + 3600)
                assert speak.await_count == 2
                config.update_settings({'server_paused': True}, {})
                with patch.object(bot, 'is_ready', return_value=True), \
                        patch.object(bot.social, 'speak', new=AsyncMock(return_value=True)) as paused:
                    await bot.social.tick(hour + 7200)
                    paused.assert_not_awaited()
        db.close()
    print('PASS: shared hourly random state roll, single agreed winner, one post per hour, quiet-channel guard and pause gate.')


if __name__ == '__main__':
    asyncio.run(main())
