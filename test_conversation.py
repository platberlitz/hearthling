"""Runnable checks for real multimodal payloads and truthful text-only handling."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
from openai import BadRequestError

import testenv  # noqa: F401
from hearthling import APPLICATION_ID, OWNER_ID, SERVER_ID, Hearthling, open_database
from conversation import image_url, message_content, model_reply, shared_history, text_only
from test_hearthling import ai_reply
from test_features import request


async def shared_context():
    db = open_database(':memory:')
    async with Hearthling(None, db, bot_id=900000000000000801, character_name='Rowan') as bot:
        message = request()
        message.content = 'how about you rowan'
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = message.channel.id
        channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
        message.channel = channel
        clove = NS(id=900000000000000802, display_name='Clove', bot=True)
        question = NS(id=message.id - 2, author=message.author, content='clove, who is your favourite pony in mlp gen 4',
                      created_at=message.created_at, reference=None)
        answer = NS(id=message.id - 1, author=clove, content='Pinkie Pie, obviously!', created_at=message.created_at,
                    reference=discord.MessageReference(message_id=question.id, channel_id=channel.id))
        earlier = [answer, question]

        async def history(**options):
            assert options == {'limit': bot.features.settings.recent_context, 'before': message.created_at}
            for item in earlier:
                yield item

        channel.history.side_effect = history
        old = [{'role': 'user', 'content': 'Rowan, generate a picture of your room.'},
               {'role': 'assistant', 'content': 'A clean desk and an angled lamp.'}]
        for author, content in ((message.author.id, old), (clove.id, [{'role': 'tool', 'content': 'PRIVATE_TOOL_RESULT'}])):
            db.execute("INSERT INTO turns (request_id,channel_id,author_id,messages,state) VALUES (?,?,?,?,'complete')",
                       (str(author), str(channel.id), str(author), json.dumps(content)))
        preview = await bot.converse(message, preview=True)
        recent = json.loads(preview['messages'][-2]['content'])['recent_channel_messages']
        assert [item['message'] for item in recent] == [question.content, answer.content]
        assert recent[1]['speaker']['is_bot'] and recent[1]['speaker']['display_name'] == 'Clove'
        assert recent[1]['reply_to_message_id'] == str(question.id)
        assert json.loads(preview['messages'][-1]['content'])['message'] == message.content
        assert 'PRIVATE_TOOL_RESULT' not in json.dumps(preview)
        assert 'clean desk' in json.dumps(preview)  # Fresh context must coexist with older personal turns.
        with patch.object(bot, 'authorised_guild', new=AsyncMock()), \
                patch('hearthling.model_reply', new=AsyncMock(return_value=ai_reply(content='Rarity.'))) as reply:
            _, turn, _ = await bot.converse(message)
            assert 'Pinkie Pie' in json.dumps(reply.call_args.args[1])
            assert 'recent_channel_messages' not in json.dumps(turn)  # Do not replay stale snapshots later.

        bot.features.settings.recent_context = 200
        earlier[:] = [NS(**{**vars(question), 'id': message.id - i - 1, 'content': 'x' * 5000}) for i in range(200)]
        payload = json.loads(await message_content(bot, message))
        recent = payload['recent_channel_messages']
        assert 0 < len(recent) < 200 and sum(len(json.dumps(item, ensure_ascii=False)) for item in recent) <= 16000
        assert all(len(item['message']) == 4000 for item in recent)
        assert recent[-1]['message_id'] == str(message.id - 1)

        async def forbidden(**options):
            raise discord.Forbidden(NS(status=403, reason='Forbidden'), 'Missing history permission')
            yield

        channel.history.side_effect = forbidden
        payload = json.loads(await message_content(bot, message))
        assert payload['recent_channel_messages_unavailable'] and 'recent_channel_messages' not in payload
        channel.history.reset_mock()
        channel.permissions_for.return_value.read_message_history = False
        assert 'recent_channel_messages' not in json.loads(await message_content(bot, message))
        channel.history.assert_not_called()
        channel.permissions_for.return_value.read_message_history = True
        bot.features.settings.recent_context = 0
        assert 'recent_channel_messages' not in json.loads(await message_content(bot, message))
        channel.history.assert_not_called()
        message.guild = None
        bot.features.settings.recent_context = 20
        assert 'recent_channel_messages' not in json.loads(await message_content(bot, message))
        channel.history.assert_not_called()
    db.close()


async def cleared_context():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'history.sqlite3'
        db = open_database(path)
        async with Hearthling(None, db, data_dir=directory) as bot:
            message = request()
            message.content = 'Hearthling, hello again.'
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = message.channel.id
            channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
            message.channel = channel
            old = NS(id=message.id - 1, author=message.author, content='OLD_CHANNEL_CONTEXT',
                     created_at=message.created_at, reference=None)
            earlier = [old]

            async def history(**options):
                for item in earlier:
                    yield item

            channel.history.side_effect = history
            saved = json.dumps([{'role': 'assistant', 'content': 'OLD_SAVED_CONTEXT'}])
            for identity, author, where in (('mine', message.author.id, channel.id),
                                            ('other-person', message.author.id + 1, channel.id),
                                            ('other-channel', message.author.id, channel.id + 1)):
                db.execute("INSERT INTO turns (request_id,channel_id,author_id,messages,state) VALUES (?,?,?,?,'complete')",
                           (identity, str(where), str(author), saved))
            db.execute('INSERT INTO actions VALUES (?,?,?,?)', ('mine', 'receipt', '{}', '{"ok":true}'))
            bot.features.remember(message.author.id, channel.id, 'A lasting preference', keywords='preference')
            db.commit()
            before = json.dumps(await bot.converse(message, preview=True))
            assert 'OLD_CHANNEL_CONTEXT' in before and 'OLD_SAVED_CONTEXT' in before

            clear = bot.command_tree.get_command('history', guild=discord.Object(SERVER_ID)).get_command('clear')
            interaction = NS(user=message.author, guild_id=SERVER_ID, channel_id=channel.id,
                             response=NS(is_done=lambda: False, send_message=AsyncMock()))
            bot.active_users.add(message.author.id)
            try:
                await clear.callback(interaction)
            except ValueError as exc:
                assert 'current reply' in str(exc)
            else:
                raise AssertionError('Clearing during a reply would let it restore the old turn.')
            assert bot.characters.history_after(message.author.id, channel.id) == 0
            bot.active_users.clear()
            await clear.callback(interaction)
            assert json.loads(interaction.response.send_message.call_args.args[0])['cleared_turns'] == 1
            assert db.execute('SELECT result FROM actions').fetchone()[0] == '{"ok":true}'
            assert bot.features.memories(message.author.id, channel.id)
            assert db.execute("SELECT count(*) FROM turns WHERE messages <> '[]'").fetchone()[0] == 2
            cutoff = bot.characters.history_after(message.author.id, channel.id)
            assert cutoff and bot.characters.history_after(message.author.id + 1, channel.id) == 0
            assert bot.characters.history_after(message.author.id, channel.id + 1) == 0

        db.close()
        db = open_database(path)
        async with Hearthling(None, db, data_dir=directory) as bot:
            assert bot.characters.history_after(message.author.id, channel.id) == cutoff
            after = json.dumps(await bot.converse(message, preview=True))
            assert 'OLD_CHANNEL_CONTEXT' not in after and 'OLD_SAVED_CONTEXT' not in after
            fresh_time = discord.utils.snowflake_time(cutoff) + timedelta(seconds=1)
            fresh = NS(id=discord.utils.time_snowflake(fresh_time), author=message.author,
                       content='NEW_CHANNEL_CONTEXT', created_at=fresh_time, reference=None)
            earlier.insert(0, fresh)
            message.created_at = fresh_time + timedelta(seconds=1)
            message.id = discord.utils.time_snowflake(message.created_at)
            after = json.dumps(await bot.converse(message, preview=True))
            assert 'NEW_CHANNEL_CONTEXT' in after
            assert 'OLD_CHANNEL_CONTEXT' not in after and 'OLD_SAVED_CONTEXT' not in after
            assert [item['message'] for item in await bot.social.recent_messages(channel, message.author.id)] == ['NEW_CHANNEL_CONTEXT']
            # An explicit recall remains the way to intentionally bring older messages back.
            recalled = await bot.characters.recall_history(message, 50)
            assert recalled['messages_recalled'] == 2
            assert 'OLD_CHANNEL_CONTEXT' in json.dumps(await bot.converse(message, preview=True))
        db.close()


async def main():
    await shared_context()
    await cleared_context()
    db = open_database(':memory:')
    async with Hearthling(None, db) as bot:
        message = request()
        message.author.name = 'actual_username'
        message.author.global_name = 'Global display'
        message.author.nick = 'Server nickname'
        message.author.display_avatar = f'https://cdn.discordapp.com/avatars/{message.author.id}/portrait.png'
        message.attachments = [NS(content_type='image/png', size=500, filename='diagram.png',
                                  url='https://cdn.discordapp.com/attachments/111/222/diagram.png?ex=abc')]
        mentioned = NS(id=900000000000000077, name='someone_else', display_name='Someone', nick='Another nickname', bot=False)
        message.mentions = [mentioned]
        bot._connection.user = NS(id=APPLICATION_ID, name='Hearthling', display_name='Hearthling', bot=True)
        text = json.loads(await message_content(bot, message))
        assert text['speaker']['username'] == 'actual_username'
        assert text['speaker']['server_nickname'] == 'Server nickname'
        assert text['visible_people'][-1]['username'] == 'someone_else'
        assert 'Vision is off' in text['image_status']
        bot.features.settings.vision_enabled = True
        parts = await message_content(bot, message)
        assert parts[0]['type'] == 'text'
        assert 'saved appearance facts' in parts[0]['text']
        pictures = [part for part in parts if part['type'] == 'image_url']
        assert len(pictures) == 2 and 'diagram.png' in pictures[0]['image_url']['url']
        bot.features.settings.vision_max_images = 1
        assert sum(part['type'] == 'image_url' for part in await message_content(bot, message)) == 1
        for url in ('http://127.0.0.1/a.png', 'https://cdn.discordapp.com.evil.test/avatars/x.png',
                    'https://someone:password@cdn.discordapp.com/avatars/x.png', 'file:///etc/passwd'):
            assert image_url(url) is None
        quoted = MagicMock(spec=discord.Message)
        quoted.id, quoted.channel, quoted.author, quoted.content = 900000000000000008, message.channel, mentioned, 'Here is my picture.'
        quoted.attachments = [NS(content_type='image/jpeg', size=400, filename='reference.jpg',
                                 url='https://cdn.discordapp.com/attachments/111/223/reference.jpg')]
        quoted.embeds = []
        reference = discord.MessageReference(message_id=quoted.id, channel_id=message.channel.id)
        reference.resolved = quoted
        message.reference = reference
        message.channel.permissions_for = MagicMock(return_value=discord.Permissions(view_channel=True, read_message_history=True))
        bot.features.settings.vision_max_images = 8
        parts = await message_content(bot, message)
        assert 'reference.jpg' in json.dumps(parts)
        message.channel.permissions_for.return_value.read_message_history = False
        assert 'reference.jpg' not in json.dumps(await message_content(bot, message))
        messages = [{'role': 'user', 'content': parts}]
        assert all(isinstance(item['content'], str) for item in text_only(messages))
        assert 'unavailable' in text_only(messages)[0]['content']
        failure = BadRequestError('This model does not support image input',
                                  response=httpx.Response(400, request=httpx.Request('POST', 'https://example.test')), body=None)
        create = AsyncMock(side_effect=[failure, ai_reply(content='I cannot inspect that image.'), ai_reply(content='Still text only.')])
        bot.ai = NS(chat=NS(completions=NS(create=create)))
        await model_reply(bot, messages, max_tokens=100)
        assert create.await_count == 2 and isinstance(create.call_args.kwargs['messages'][0]['content'], str)
        await model_reply(bot, messages, max_tokens=100)
        assert create.await_count == 3 and isinstance(create.call_args.kwargs['messages'][0]['content'], str)
        message.content, message.reference = '', None
        message.guild = None
        message.channel.typing = MagicMock()
        with patch.object(bot, 'converse', new=AsyncMock(return_value=('Seen.', messages, []))) as converse, \
                patch.object(bot, 'deliver', new=AsyncMock()):
            await bot.on_message(message)
            converse.assert_awaited_once()
            saved = json.loads(db.execute('SELECT messages FROM turns WHERE request_id=?', (str(message.id),)).fetchone()[0])
            assert isinstance(saved[0]['content'], str)
        pause = bot.command_tree.get_command('pause', guild=discord.Object(SERVER_ID))
        resume = bot.command_tree.get_command('resume', guild=discord.Object(SERVER_ID))
        interaction = NS(user=NS(id=900000000000000033, bot=False), guild_id=SERVER_ID,
                         response=NS(send_message=AsyncMock()))
        await pause.callback(interaction)
        assert not bot.features.settings.server_paused
        interaction.user.id = OWNER_ID
        await pause.callback(interaction)
        assert bot.features.settings.server_paused
        assert interaction.response.send_message.call_args.kwargs['ephemeral'] is True
        message.guild, message.content = NS(id=SERVER_ID), 'Hearthling, hello.'
        assert not bot.addressed(message)
        assert not (await bot.execute('{"action":"server_info"}', message))['ok']
        await bot.deliver(message, 'Must not send while paused.', [])
        message.channel.send.assert_not_awaited()
        message.guild = None
        assert bot.addressed(message)
        await resume.callback(interaction)
        assert not bot.features.settings.server_paused
        interaction.guild_id = 123
        await pause.callback(interaction)
        assert not bot.features.settings.server_paused
    db.close()
    print('PASS: persistent history clearing, shared channel context, bot hand-offs, context limits/permissions, transient snapshots, image/profile payloads, vision fallback and owner-only pause/resume.')


async def cross_channel_history():
    with tempfile.TemporaryDirectory() as directory:
        db, new_db = open_database(':memory:'), open_database(':memory:')
        async with Hearthling(None, db, data_dir=directory) as original, \
                Hearthling(None, new_db, data_dir=directory, bot_id=APPLICATION_ID + 1, character_name='Alden') as alden:
            message = request()
            message.content = 'Alden, what did we discuss?'
            message.id = discord.utils.time_snowflake(discord.utils.utcnow(), high=True)
            guild = NS(id=SERVER_ID, me=NS(id=alden.bot_id), get_member=MagicMock(return_value=message.author))
            channel = NS(id=message.channel.id, guild=guild,
                         permissions_for=MagicMock(return_value=discord.Permissions(view_channel=True, read_message_history=True)))
            elsewhere = NS(id=channel.id + 1, guild=guild,
                           permissions_for=MagicMock(return_value=discord.Permissions(view_channel=True, read_message_history=True)))
            private = NS(id=channel.id + 2, guild=guild,
                         permissions_for=MagicMock(return_value=discord.Permissions.none()))
            dm = NS(id=channel.id + 3, guild=None)
            channels = {item.id: item for item in (channel, elsewhere, private, dm)}
            message.guild, message.channel = guild, channel
            alden.manager = NS(bots={str(original.bot_id): original, str(alden.bot_id): alden})
            alden.features.update_settings({'recent_context': 0, 'expressions_enabled': False}, {})
            alden.get_channel = MagicMock(side_effect=channels.get)
            def save(identity, where, author, text):
                turn = [{'role': 'user', 'content': json.dumps({'message': text, 'recent_channel_messages': 'STALE_SNAPSHOT'})},
                        {'role': 'assistant', 'content': 'Hearthling prefers jasmine tea.'},
                        {'role': 'tool', 'content': 'PRIVATE_TOOL_RESULT'}]
                db.execute("INSERT INTO turns (request_id,channel_id,author_id,messages,state) VALUES (?,?,?,?,'complete')",
                           (str(identity), str(where.id), str(author), json.dumps(turn)))
                db.commit()
            save(message.id - 10, elsewhere, message.author.id, 'Earlier garden discussion')
            save(message.id - 9, channel, message.author.id, 'Earlier conversation with Hearthling')
            save(message.id - 8, elsewhere, message.author.id + 1, 'OTHER_PERSON_CONTEXT')
            save(message.id - 7, private, message.author.id, 'PRIVATE_CHANNEL_CONTEXT')
            save(message.id - 6, dm, message.author.id, 'PRIVATE_DM_CONTEXT')
            before = json.dumps(await alden.converse(message, preview=True))
            assert 'Earlier garden discussion' in before and 'Earlier conversation with Hearthling' in before
            for excluded in ('OTHER_PERSON_CONTEXT', 'PRIVATE_CHANNEL_CONTEXT', 'PRIVATE_DM_CONTEXT', 'PRIVATE_TOOL_RESULT', 'STALE_SNAPSHOT'):
                assert excluded not in before
            entries = await shared_history(alden, message)
            assert len(entries) == 2 and all(item['character']['name'] == 'Hearthling' for item in entries)
            assert entries[0]['dialogue'][1]['speaker_id'] == str(original.bot_id)
            assert new_db.execute('SELECT count(*) FROM turns').fetchone()[0] == 0
            assert not await shared_history(alden, message, budget=1)
            elsewhere.permissions_for.return_value = discord.Permissions.none()
            assert 'Earlier garden discussion' not in json.dumps(await shared_history(alden, message))
            elsewhere.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
            alden.characters.clear_history(message.author.id, channel.id)
            assert not await shared_history(alden, message)
            new_db.execute('DELETE FROM settings WHERE name LIKE ?', ('history_after:%',))
            new_db.commit()
            original.characters.clear_history(message.author.id, elsewhere.id)
            assert 'Earlier garden discussion' not in json.dumps(await shared_history(alden, message))
        db.close()
        new_db.close()
    print('PASS: new bot cross-channel dialogue, labelled speakers, permission checks, personal isolation, no tool or DM leakage, budget and history reset.')


if __name__ == '__main__':
    asyncio.run(main())
    asyncio.run(cross_channel_history())
