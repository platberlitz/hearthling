"""Offline checks for authorisation, targeting, partial results and replay protection."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from openai import APIError
from openai.types.chat import ChatCompletionMessage
from pydantic import ValidationError

import testenv  # noqa: F401
from hearthling import Action, APPLICATION_ID, Hearthling, FAKE_PICTURE_NOTE, MAX_TOOL_CALLS, OWNER_ID, SERVER_ID, open_database, permitted_request, should_reply, target_id, unique


def expect_error(error, callback):
    try:
        callback()
    except error:
        return
    raise AssertionError(f'Expected {error.__name__}')


async def expect_async_error(error, callback):
    try:
        await callback
    except error:
        return
    raise AssertionError(f'Expected {error.__name__}')


def ai_reply(calls=0, content=None):
    message = ChatCompletionMessage(role='assistant', content=content, tool_calls=[
        {'id': f'call_{number}', 'type': 'function', 'function': {
            'name': 'discord_action', 'arguments': '{"action":"server_info"}'}}
        for number in range(calls)
    ] or None)
    return NS(choices=[NS(message=message)])


async def main():
    owner = NS(id=OWNER_ID, display_name='Owner', bot=False, top_role=5,
               guild_permissions=discord.Permissions(administrator=True), create_dm=AsyncMock())
    guild = NS(id=SERVER_ID, owner_id=OWNER_ID, me=NS(top_role=10), fetch_member=AsyncMock(return_value=owner))
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 900000000000000001
    channel.guild = guild
    channel.typing.return_value = AsyncMock()
    channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
    request = NS(id=900000000000000002, author=owner, guild=guild, channel=channel,
                 content='Hearthling, check the server.', created_at=datetime.now(timezone.utc), mentions=[], reference=None)
    request.to_reference = lambda **kwargs: discord.MessageReference(
        message_id=request.id, channel_id=channel.id, guild_id=SERVER_ID, **kwargs)
    assert permitted_request(request)
    for author, place, expected in (
        (owner, None, True), (owner, NS(id=123), False),
        (NS(id=123, bot=False), guild, False), (NS(id=OWNER_ID, bot=True), guild, False),
    ):
        assert permitted_request(NS(author=author, guild=place)) is expected
    for value in (str(OWNER_ID), f'<@{OWNER_ID}>', f'<@!{OWNER_ID}>', f'<@&{OWNER_ID}>'):
        assert target_id(value) == OWNER_ID
    for value in (f'<@{OWNER_ID}', f'{OWNER_ID}>', '123', 'alice'):
        assert target_id(value) is None
    expect_error(ValueError, lambda: unique([NS(name='Alice'), NS(name='alice')], 'ALICE'))
    for payload in (
        {'action': 'shell'}, {'action': 'kick', 'owner_id': '123'},
        {'action': 'timeout', 'seconds': True}, {'action': 'timeout', 'seconds': -1},
        {'action': 'timeout', 'seconds': 2419201}, {'action': 'purge_messages', 'limit': 101},
        {'action': 'delete_messages', 'message_ids': ['not-an-id']},
    ):
        expect_error(ValidationError, lambda: Action.model_validate_json(json.dumps(payload)))

    db = sqlite3.connect(':memory:')
    db.execute("CREATE TABLE turns (request_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'started')")
    db.execute('INSERT INTO turns VALUES (?, ?, ?, ?)', (
        'legacy', str(channel.id), json.dumps([{'role': 'assistant', 'content': 'owner-only history sentinel'}]), 'complete'))
    with patch('hearthling.sqlite3.connect', return_value=db):
        open_database(':memory:')
        open_database(':memory:')
    assert db.execute("SELECT author_id FROM turns WHERE request_id='legacy'").fetchone()[0] == str(OWNER_ID)
    async with Hearthling(None, db) as bot:
        bot._connection.user = NS(id=APPLICATION_ID, edit=AsyncMock())
        with patch.object(bot, 'application_info', new=AsyncMock(return_value=NS(id=APPLICATION_ID))):
            await bot.setup_hook()
            await bot.setup_hook()
            bot.user.edit.assert_awaited_once()
            assert bot.user.edit.call_args.kwargs['username'] == 'Hearthling'
        request.mentions = [bot.user]
        with patch.object(bot, 'get_guild', return_value=guild):
            assert (await bot.authorised_guild(request))[1] is owner
            owner.guild_permissions.administrator = False
            await expect_async_error(PermissionError, bot.authorised_guild(request))
            owner.guild_permissions.administrator = True

            outsider = NS(**{**vars(request), 'id': 900000000000000005,
                             'author': NS(id=123, display_name='Guest', bot=False)})
            for content, expected in (('Hello HEARTHLING!', True), ('hearthling, tea?', True), ('Earthling day.', False), ('Hearthlings', False)):
                assert should_reply(NS(**{**vars(outsider), 'content': content, 'mentions': []})) is expected
            for author, place, expected in ((outsider.author, guild, True), (owner, None, True),
                                           (outsider.author, None, False), (owner, NS(id=123), False),
                                           (NS(id=APPLICATION_ID, bot=True), guild, False)):
                assert should_reply(NS(**{**vars(request), 'author': author, 'guild': place})) is expected
            assert should_reply(NS(**{**vars(outsider), 'content': 'Hello!', 'mentions': [bot.user]}))
            await expect_async_error(PermissionError, bot.authorised_guild(outsider))
            with patch.object(bot, 'perform', new=AsyncMock()) as perform:
                assert not (await bot.execute('{"action":"server_info"}', outsider))['ok']
                perform.assert_not_awaited()
            assert db.execute('SELECT count(*) FROM actions').fetchone()[0] == 0

            duplicate_members = NS(chunked=True, members=[
                NS(id=11, name='alice', display_name='Alice'),
                NS(id=12, name='different', display_name='ALICE'),
            ])
            await expect_async_error(ValueError, bot.member(duplicate_members, 'Alice'))
            with patch.object(bot, 'fetch_channel', new=AsyncMock(return_value=NS(guild=NS(id=123)))):
                await expect_async_error(PermissionError, bot.channel(guild, str(channel.id), request))

            target = NS(id=900000000000000003, top_role=10, guild_permissions=discord.Permissions())
            with patch.object(bot, 'member', new=AsyncMock(return_value=target)):
                await expect_async_error(ValueError, bot.moderate_target(guild, 'Alice', 'kick', owner))
                target.top_role = 1
                target.guild_permissions.administrator = True
                await expect_async_error(ValueError, bot.moderate_target(guild, 'Alice', 'timeout', owner))
                target.guild_permissions.administrator = False
                guild.owner_id = 123
                target.top_role = 5
                await expect_async_error(ValueError, bot.moderate_target(guild, 'Alice', 'ban', owner))
                guild.owner_id = OWNER_ID

            role = MagicMock(spec=discord.Role)
            role.id, role.name = 900000000000000004, 'Member'
            role.is_default.return_value = False
            role.permissions = discord.Permissions(view_channel=True, send_messages=True)
            role.edit = AsyncMock(return_value=role)
            overwrite = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            channel.overwrites_for.return_value = overwrite
            with patch.object(bot, 'role', new=AsyncMock(return_value=role)), patch.object(bot, 'channel', new=AsyncMock(return_value=channel)):
                await bot.perform(Action(action='edit_role', role='Member', permissions={'send_messages': False}), request)
                permissions = role.edit.call_args.kwargs['permissions']
                assert permissions.view_channel and not permissions.send_messages
                assert role.permissions.send_messages  # The original permissions were not mutated.
                await bot.perform(Action(action='set_channel_permission', role='Member', permission_name='send_messages', permission_value='deny'), request)
                changed = channel.set_permissions.call_args.kwargs['overwrite']
                assert changed.view_channel is True and changed.send_messages is False

            messages = [NS(id=900000000000000010 + n, author=NS(id=42), delete=AsyncMock(),
                           content='A recent channel message.', created_at=request.created_at, reference=None) for n in range(3)]
            messages[1].delete.side_effect = TimeoutError('test interruption')

            async def history(**kwargs):
                assert kwargs['before'] == request.created_at
                for message in messages:
                    yield message

            channel.history.side_effect = history
            with patch.object(bot, 'channel', new=AsyncMock(return_value=channel)):
                result = await bot.execute('{"action":"purge_messages","limit":3}', request)
                assert result['result']['deleted_count'] == 1
                assert result['result']['deleted_ids'] == [str(messages[0].id)]
                assert result['result']['stopped_because']
                messages[2].delete.assert_not_awaited()
                repeat = await bot.execute('{"action":"purge_messages","limit":3,"reason":"a reworded reason"}', request)
                assert repeat == result
                messages[0].delete.assert_awaited_once()

            with patch.object(bot, 'perform', new=AsyncMock(side_effect=TimeoutError('interrupted'))) as perform:
                outcome = await bot.execute('{"action":"create_role","name":"Test"}', request)
                assert outcome['outcome_may_be_uncertain']
                db.execute("UPDATE actions SET result=NULL WHERE arguments LIKE '%create_role%'")
                retry = await bot.execute('{"action":"create_role","name":"Test"}', request)
                assert retry['outcome_may_be_uncertain']
                perform.assert_awaited_once()

            bot.ai = NS(chat=NS(completions=NS(create=AsyncMock(side_effect=[ai_reply(calls=12), ai_reply(content='Done.')]))))
            with patch.object(bot, 'execute', new=AsyncMock(return_value={'ok': True, 'action': 'server_info'})) as execute:
                await bot.converse(request)
                assert execute.await_count == MAX_TOOL_CALLS
                assert bot.ai.chat.completions.create.call_args.kwargs['tool_choice'] == 'none'
            bot.ai.chat.completions.create = AsyncMock(side_effect=[ai_reply(calls=3), ai_reply(content='Stopped.')])
            with patch.object(bot, 'execute', new=AsyncMock(return_value={'ok': False, 'action': 'ban', 'outcome_may_be_uncertain': True})) as execute:
                await bot.converse(request)
                execute.assert_awaited_once()
            bot.features.settings = bot.features.settings.model_copy(update={'images_enabled': True})
            bot.ai.chat.completions.create = AsyncMock(side_effect=[ai_reply(content='I made you a cozy tempura scene!'), ai_reply(content='Oops, no picture yet.')])
            text, turn, _ = await bot.converse(NS(**{**vars(request), 'content': 'Hearthling, gen a picture of me eating tempura'}))
            assert text == 'Oops, no picture yet.' and 'tempura scene' not in json.dumps(turn)
            assert bot.ai.chat.completions.create.call_args.kwargs['messages'][-1]['content'] == FAKE_PICTURE_NOTE

            db.execute('INSERT INTO turns (request_id, channel_id, author_id, messages, state) VALUES (?, ?, ?, ?, ?)',
                       ('guest-history', str(channel.id), '123', json.dumps([{'role': 'assistant', 'content': 'guest history sentinel'}]), 'complete'))
            quoted = MagicMock(spec=discord.Message)
            quoted.id, quoted.channel, quoted.author, quoted.content = 900000000000000050, channel, bot.user, 'How do you take your tea?'
            bot.user.display_name = 'Hearthling'
            reference = discord.MessageReference(message_id=quoted.id, channel_id=channel.id, guild_id=SERVER_ID)
            reference.resolved = quoted
            follow_up = NS(**{**vars(outsider), 'content': 'With milk, please.', 'mentions': [], 'reference': reference})
            assert should_reply(follow_up)
            bot.ai.chat.completions.create = AsyncMock(return_value=ai_reply(content='Tea it is.'))
            with patch.object(bot, 'authorised_guild', new=AsyncMock()) as authorise:
                await bot.converse(follow_up)
                authorise.assert_not_awaited()
            sent = bot.ai.chat.completions.create.call_args.kwargs
            assert [tool['function']['name'] for tool in sent['tools']] == ['feature_tool']
            assert 'host_status' not in sent['tools'][0]['function']['parameters']['properties']['action']['enum']
            system = sent['messages'][0]['content']
            assert all(section in system for section in ('<character_identity>', '<knowledge_limits>', '<psychological_boundaries>', '<conversation_style>'))
            assert '{{' not in system
            assert f'<@{APPLICATION_ID}> and <@!{APPLICATION_ID}> are mentions of you' in system
            assert 'administrator and moderator' in system and 'not your owner' in system
            assert request.created_at.isoformat() in system
            assert 'owner-only history sentinel' not in json.dumps(sent)
            assert 'guest history sentinel' in json.dumps(sent)
            payload = json.loads(sent['messages'][-1]['content'])
            assert payload['message'] == follow_up.content
            assert payload['replying_to']['message'] == quoted.content
            channel.permissions_for.return_value.read_message_history = False
            await bot.converse(follow_up)
            payload = json.loads(bot.ai.chat.completions.create.call_args.kwargs['messages'][-1]['content'])
            assert payload['replying_to']['unavailable'] is True
            channel.fetch_message.assert_not_awaited()
            channel.permissions_for.return_value.read_message_history = True
            reference.channel_id = 900000000000000099
            assert not should_reply(follow_up)
            await bot.converse(NS(**{**vars(follow_up), 'content': 'Hearthling, look at that.'}))
            assert 'replying_to' not in json.loads(bot.ai.chat.completions.create.call_args.kwargs['messages'][-1]['content'])
            reference.channel_id = channel.id
            reference.resolved = None
            channel.fetch_message.return_value = quoted
            await bot.converse(NS(**{**vars(follow_up), 'content': 'Hearthling, look at that.'}))
            channel.fetch_message.assert_awaited_once_with(quoted.id)
            bot.ai.chat.completions.create = AsyncMock(return_value=ai_reply(calls=1))
            with patch.object(bot, 'execute', new=AsyncMock()) as execute:
                text, turn, receipts = await bot.converse(outsider)
                execute.assert_not_awaited()
                assert 'owner' in text and not receipts and turn[-1]['role'] == 'assistant'
            failure = APIError('Test connection failure', NS(), body=None)
            bot.ai.chat.completions.create = AsyncMock(side_effect=failure)
            with patch('hearthling.logging.warning'):
                await expect_async_error(APIError, bot.converse(outsider))
                bot.ai.chat.completions.create = AsyncMock(side_effect=[ai_reply(calls=1), failure])
                with patch.object(bot, 'execute', new=AsyncMock(return_value={'ok': True, 'action': 'timeout'})):
                    text, _, receipts = await bot.converse(request)
                    assert 'confirmed server changes' in text and len(receipts) == 1

            channel.send.reset_mock()
            await bot.deliver(request, 'x' * 2000, ['{"ok":true}'])
            assert channel.send.await_count == 3
            for call in channel.send.call_args_list:
                assert call.kwargs['reference'].message_id == request.id
                assert call.kwargs['reference'].fail_if_not_exists is False
                assert call.kwargs['mention_author'] is False
                assert call.kwargs['allowed_mentions'].replied_user is False
            dm = NS(id=channel.id + 1, send=AsyncMock())
            owner.create_dm.return_value = dm
            channel.send.side_effect = discord.Forbidden(NS(status=403, reason='Forbidden'), 'No access')
            await bot.deliver(request, 'Fallback reply.', [])
            assert 'reference' not in dm.send.call_args.kwargs
            channel.send.side_effect = None

            channel.send.reset_mock()
            await bot.deliver(outsider, f'Paging <@{OWNER_ID}> about this.', [])
            assert OWNER_ID in channel.send.call_args.kwargs['allowed_mentions'].to_dict()['users']

            with patch.object(bot, 'converse', new=AsyncMock(return_value=('Ready.', [], []))) as converse, patch.object(bot, 'deliver', new=AsyncMock()) as deliver:
                request.mentions = []
                request.content = 'Unaddressed conversation.'
                await bot.on_message(request)
                converse.assert_not_awaited()
                request.content = 'Hearthling, check the server.'
                await bot.on_message(request)
                await bot.on_message(request)
                converse.assert_awaited_once()
                bot.active_users.add(OWNER_ID)
                await bot.on_message(outsider)
                assert converse.await_count == 2
                assert db.execute('SELECT author_id FROM turns WHERE request_id=?', (str(outsider.id),)).fetchone()[0] == '123'
                delivered = deliver.await_count
                await bot.on_message(NS(**{**vars(request), 'id': 900000000000000007}))
                assert deliver.await_count == delivered
                assert converse.await_count == 2 and OWNER_ID in bot.active_users and 123 not in bot.active_users
                bot.active_users.clear()
                converse.side_effect = ValueError('Test failure')
                with patch('hearthling.logging.error'):
                    await bot.on_message(NS(**{**vars(outsider), 'id': 900000000000000006}))
                assert not bot.active_users and 'could not finish' in deliver.call_args.args[1]
    db.close()
    print('PASS: public name/mention/reply triggers, owner-only actions, private history migration, reply context, linked replies, independent chats, targeting, permissions, partial results and replay limits.')


if __name__ == '__main__':
    logging.getLogger('discord.client').setLevel(logging.ERROR)
    asyncio.run(main())
