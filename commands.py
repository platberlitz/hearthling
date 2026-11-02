"""Discord slash commands; authority comes from Discord metadata, never model text."""
import json
from types import SimpleNamespace

import discord
from discord import app_commands
from images import prompt_messages
from pending import temporary_chat_error


async def permitted(bot, interaction, *, owner=False):
    allowed = (not interaction.user.bot
               and (interaction.guild_id == bot.server_id or
                    (interaction.guild_id is None and interaction.user.id == bot.features.owner_id))
               and (not owner or interaction.user.id == bot.features.owner_id))
    if not allowed:
        await interaction.response.send_message('Only my configured owner can use that control here.', ephemeral=True)
    return allowed


def register_commands(bot):
    tree = app_commands.CommandTree(bot)
    guild = discord.Object(bot.server_id)

    @tree.command(name='pause', description='Owner: pause this bot’s server replies; /resume brings them back.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def pause(interaction: discord.Interaction):
        if not await permitted(bot, interaction, owner=True):
            return
        bot.features.update_settings({'server_paused': True}, {})
        await interaction.response.send_message(
            f'{bot.character_name} is paused in this server, darling. Use /resume when you want me back. (｡•̀ᴗ-)✧', ephemeral=True)

    @tree.command(name='resume', description='Owner: resume this bot’s paused server replies.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def resume(interaction: discord.Interaction):
        if not await permitted(bot, interaction, owner=True):
            return
        bot.features.update_settings({'server_paused': False}, {})
        await interaction.response.send_message(f'{bot.character_name} is answering in this server again. (｡•̀ᴗ-)✧', ephemeral=True)

    @tree.command(name='status', description='Show this bot’s current reply and connection status.', guild=guild)
    async def status(interaction: discord.Interaction):
        if not await permitted(bot, interaction):
            return
        paused = bot.features.settings.server_paused
        await interaction.response.send_message(
            f'{bot.character_name}: {"server replies paused" if paused else "server replies enabled"}; '
            f'{len(bot.active_users)} active request(s).', ephemeral=True)

    def request_for(interaction, text=''):
        return SimpleNamespace(id=interaction.id, author=interaction.user, guild=interaction.guild,
                               channel=interaction.channel, created_at=interaction.created_at,
                               content=text, mentions=[], reference=None, attachments=[], embeds=[], interaction=interaction)

    async def answer(interaction, text):
        text = bot.features.redact(str(text))
        for start in range(0, len(text) or 1, 1900):
            send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await send(text[start:start + 1900] or 'Nothing to show yet.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='autonomous', description='Owner: configure unprompted replies to messages in this channel.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def autonomous(interaction: discord.Interaction, enabled: bool, chance: app_commands.Range[int, 1, 50] = 10,
                         cooldown: app_commands.Range[int, 0, 86400] = 120):
        if not await permitted(bot, interaction, owner=True):
            return
        rules = bot.features.settings.model_dump()['channel_rules']
        rules[str(interaction.channel_id)] = rules.get(str(interaction.channel_id), {}) | {'enabled': enabled, 'chance': chance, 'cooldown': cooldown}
        bot.features.update_settings({'channel_rules': rules}, {})
        await answer(interaction, f'Unprompted replies in this channel: {"on" if enabled else "off"}; {chance}% chance, {cooldown}s cooldown.')

    @tree.command(name='nickname-trigger', description='Owner: enable or disable name triggers in this channel.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def nickname_trigger(interaction: discord.Interaction, enabled: bool):
        if not await permitted(bot, interaction, owner=True):
            return
        rules = bot.features.settings.model_dump()['channel_rules']
        rules[str(interaction.channel_id)] = rules.get(str(interaction.channel_id), {}) | {'nickname_trigger': enabled}
        bot.features.update_settings({'channel_rules': rules}, {})
        await answer(interaction, f'Name triggers in this channel: {"on" if enabled else "off"}. Mentions and direct replies still work.')

    @tree.command(name='stop', description='Owner: stop or resume conversations between all managed bots.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def stop(interaction: discord.Interaction, paused: bool = True):
        if await permitted(bot, interaction, owner=True):
            bot.social.stop_interactions(paused)
            await answer(interaction, 'Conversations between bots are ' + ('stopped.' if paused else 'enabled again.'))

    @tree.command(name='speak', description='Owner: ask this character to start a conversation here, without action tools.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def speak(interaction: discord.Interaction, prompt: app_commands.Range[str, 1, 2000] = 'Start a short, natural conversation in your own character.'):
        if not await permitted(bot, interaction, owner=True):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        sent = await bot.social.speak(interaction.channel, prompt)
        await answer(interaction, 'Message sent.' if sent else 'This channel is paused, unavailable or busy. Resume it or let the current reply finish.')

    @tree.command(name='interact', description='Talk to this character using the normal conversation and permitted tools.', guild=guild)
    async def interact(interaction: discord.Interaction, message: app_commands.Range[str, 1, 4000]):
        if not await permitted(bot, interaction):
            return
        request = request_for(interaction, f'{bot.character_name}, {message}')
        if not bot.social.access(request, check_schedule=False):
            await answer(interaction, 'This conversation is paused or unavailable.')
            return
        waiting = not bot.social.available() or bool(bot.active_users)
        if not waiting:
            bot.active_users.add(request.author.id)
        try:
            await interaction.response.defer(thinking=True)
            request.queue_anchor = (await interaction.original_response()).id
            if waiting:
                bot.social.queue.add(request)
                return
            cursor = bot.db.execute('INSERT OR IGNORE INTO turns (request_id,channel_id,author_id) VALUES (?,?,?)',
                                    (str(request.id), str(request.channel.id), str(request.author.id)))
            bot.db.commit()
            if not cursor.rowcount:
                await answer(interaction, 'That request was already handled; it will not be repeated.')
                return
            from conversation import text_only
            memory_epoch = bot.features.memory_epoch
            text, turn, receipts = await bot.converse(request)
            bot.db.execute("UPDATE turns SET messages=?,state='complete' WHERE request_id=?",
                           (bot.features.redact(json.dumps(text_only(turn) if memory_epoch == bot.features.memory_epoch else [])), str(request.id)))
            bot.db.commit()
            if not text and not receipts:
                return
            if not bot.social.response_allowed(request) or not bot.social.identity_ok(text):
                await answer(interaction, 'Delivery stopped after a pause or character change. ' + '\n'.join(receipts))
                return
            text, effects = bot.expressions.prepare(request, text)
            text = text or '…'
            sent_message = None
            pieces = [part[start:start + 1900] for part in [text] for start in range(0, len(part), 1900)]
            for piece in pieces:
                if bot.social.response_allowed(request):
                    sent_message = await interaction.followup.send(piece, allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False, users=[discord.Object(id=bot.features.owner_id)], replied_user=False), wait=True)
                    bot.social.queue.answered(request)
            picture = bot.db.execute("SELECT filename FROM pictures WHERE request_id=? AND state='complete'", (str(request.id),)).fetchone()
            if picture and bot.social.response_allowed(request):
                image = discord.File(bot.features.data_dir / 'pictures' / picture[0])
                try:
                    await interaction.followup.send(file=image, allowed_mentions=discord.AllowedMentions.none())
                finally:
                    image.close()
                for piece in prompt_messages(bot.features.picture_prompt(request.id) or ''):
                    if bot.social.response_allowed(request):
                        await interaction.followup.send(piece, allowed_mentions=discord.AllowedMentions.none())
            await bot.expressions.after(request, effects, sent_message=sent_message)
        except Exception as exc:
            actions = bot.db.execute('SELECT 1 FROM actions WHERE request_id=?', (str(request.id),)).fetchone()
            if not actions and temporary_chat_error(exc) and bot.social.queue.add(request):
                bot.db.execute("UPDATE turns SET state='queued' WHERE request_id=?", (str(request.id),))
                bot.db.commit()
                return
            bot.db.execute("UPDATE turns SET state='failed' WHERE request_id=? AND state='started'", (str(request.id),))
            bot.db.commit()
            raise
        finally:
            if not waiting:
                bot.active_users.discard(request.author.id)

    @tree.command(name='switch', description='Owner: switch to a saved character, or default.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def switch(interaction: discord.Interaction, character: str):
        if await permitted(bot, interaction, owner=True):
            bot.characters.activate('' if character.casefold() == 'default' else character)
            await answer(interaction, 'Active character: ' + bot.character_name)

    @switch.autocomplete('character')
    async def character_choices(interaction, current):
        return [app_commands.Choice(name=name, value=name) for name in ['default', *[item['name'] for item in bot.characters.list()]]
                if current.casefold() in name.casefold()][:25]

    @tree.command(name='reload', description='Owner: reload saved settings and character prompts without clearing history.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def reload(interaction: discord.Interaction):
        if await permitted(bot, interaction, owner=True):
            from maintenance import reload_bot
            reload_bot(bot)
            await answer(interaction, 'Saved settings and prompts reloaded. Your history is still here.')

    @tree.command(name='ignore', description='Owner: stop replies to a member.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def ignore(interaction: discord.Interaction, member: discord.Member):
        if await permitted(bot, interaction, owner=True):
            bot.features.update_settings({'ignored_users': list(dict.fromkeys([*bot.features.settings.ignored_users, str(member.id)]))}, {})
            await answer(interaction, 'Replies to ' + member.display_name + ' are disabled.')

    @tree.command(name='unignore', description='Owner: allow replies to a previously ignored member.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def unignore(interaction: discord.Interaction, member: discord.Member):
        if await permitted(bot, interaction, owner=True):
            bot.features.update_settings({'ignored_users': [item for item in bot.features.settings.ignored_users if item != str(member.id)]}, {})
            await answer(interaction, 'Replies to ' + member.display_name + ' are enabled.')

    @tree.command(name='ignorelist', description='Owner: show ignored member IDs.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def ignorelist(interaction: discord.Interaction):
        if await permitted(bot, interaction, owner=True):
            await answer(interaction, '\n'.join(bot.features.settings.ignored_users) or 'No members are ignored.')

    @tree.command(name='delete_messages', description='Owner: remove up to 100 earlier messages from this channel.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def delete_messages(interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]):
        if not await permitted(bot, interaction, owner=True):
            return
        if bot.active_users:
            await answer(interaction, 'Let the current request finish before deleting messages.')
            return
        bot.active_users.add(interaction.user.id)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            request = request_for(interaction, f'Delete {count} earlier messages from this channel.')
            bot.db.execute("INSERT OR IGNORE INTO turns (request_id,channel_id,author_id,messages,state) VALUES (?,?,?,'[]','complete')",
                            (str(request.id), str(request.channel.id), str(request.author.id)))
            bot.db.commit()
            result = await bot.execute(json.dumps({'action': 'purge_messages', 'limit': count}), request)
            await answer(interaction, json.dumps(result, ensure_ascii=False))
        finally:
            bot.active_users.discard(interaction.user.id)

    history = app_commands.Group(name='history', description='Manage your own conversation context.', guild_ids=[bot.server_id])

    @history.command(name='clear', description='Clear this person’s recent context here, retaining action receipts.')
    async def history_clear(interaction: discord.Interaction):
        if await permitted(bot, interaction):
            await answer(interaction, json.dumps(bot.characters.clear_history(interaction.user.id, interaction.channel_id)))

    tree.add_command(history, guild=guild)

    @tree.command(name='recall', description='Read visible messages into your own context in this channel.', guild=guild)
    async def recall(interaction: discord.Interaction, count: app_commands.Range[int, 1, 200] = 50):
        if await permitted(bot, interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await answer(interaction, json.dumps(await bot.characters.recall_history(request_for(interaction), count)))

    @tree.command(name='memory', description='Save a lasting fact for you across channels and bots.', guild=guild)
    async def memory(interaction: discord.Interaction, fact: app_commands.Range[str, 1, 400]):
        if await permitted(bot, interaction):
            if not bot.features.settings.memory_enabled:
                raise ValueError('Memory is disabled for this bot.')
            await answer(interaction, json.dumps(bot.features.remember(interaction.user.id, interaction.channel_id, fact), ensure_ascii=False))

    @tree.command(name='memories', description='Show your saved facts from every channel.', guild=guild)
    async def memories(interaction: discord.Interaction, search: str = ''):
        if await permitted(bot, interaction):
            rows = bot.features.memories(interaction.user.id, interaction.channel_id, search, 500)
            counts = bot.features.memory_count(interaction.user.id, interaction.channel_id)
            body = '\n'.join(f'{item["id"]}: {item["fact"]}' + (' (server-wide)' if item.get('scope') == 'server' else '') for item in rows)
            note = f'Saved: {counts["personal"]} personal facts across channels, {counts["server"]} server-wide.'
            if len(rows) == 500:
                note += ' Showing the newest 500; add a search word to narrow them.'
            await answer(interaction, (body or 'No saved facts yet.') + '\n' + note)

    @tree.command(name='clearmemories', description='Forget your personal facts and recent context across all channels and bots.', guild=guild)
    async def clearmemories(interaction: discord.Interaction):
        if await permitted(bot, interaction):
            await answer(interaction, json.dumps(bot.features.forget(interaction.user.id, interaction.channel_id, forget_all=True)))

    @tree.command(name='lore', description='Owner: add manual lore for every bot, this server, this bot or one person.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(scope=[app_commands.Choice(name='Every bot, everywhere', value='global'), app_commands.Choice(name='This server', value='server'), app_commands.Choice(name='This bot everywhere', value='bot'), app_commands.Choice(name='Me in this channel', value='user')])
    async def lore(interaction: discord.Interaction, text: app_commands.Range[str, 1, 4000], scope: str = 'server'):
        if await permitted(bot, interaction, owner=True):
            result = bot.characters.save_lore({'scope': scope, 'target': str(interaction.guild_id) if scope == 'server' else str(interaction.user.id) if scope == 'user' else '',
                                                'channel_id': str(interaction.channel_id) if scope == 'user' else '', 'text': text})
            await answer(interaction, f'Lore entry {result["id"]} saved.')

    timezone = app_commands.Group(name='timezone', description='Your personal timezone for dates and reminders.', guild_ids=[bot.server_id])

    @timezone.command(name='set', description='Set your IANA timezone, such as Europe/London.')
    async def timezone_set(interaction: discord.Interaction, zone: str):
        if await permitted(bot, interaction):
            await answer(interaction, 'Your timezone is ' + bot.social.set_zone(interaction.user.id, zone))

    @timezone.command(name='show', description='Show your effective timezone.')
    async def timezone_show(interaction: discord.Interaction):
        if await permitted(bot, interaction):
            await answer(interaction, 'Your timezone is ' + bot.social.zone(interaction.user.id))

    @timezone.command(name='clear', description='Return to this bot’s default timezone.')
    async def timezone_clear(interaction: discord.Interaction):
        if await permitted(bot, interaction):
            await answer(interaction, 'Your timezone is ' + bot.social.set_zone(interaction.user.id, None))

    tree.add_command(timezone, guild=guild)
    reminders = app_commands.Group(name='reminders', description='Your durable reminders.', guild_ids=[bot.server_id])

    @reminders.command(name='list', description='List your reminders and their delivery status.')
    async def reminders_list(interaction: discord.Interaction):
        if await permitted(bot, interaction):
            bot.features.require_assistant()
            items = bot.social.reminder_list(interaction.user.id)
            await answer(interaction, '\n'.join(f'{item["id"]}: <t:{int(item["due_at"])}:f> [{item["state"]}] {item["message"]}' for item in items) or 'No reminders yet.')

    @reminders.command(name='cancel', description='Cancel one of your pending reminders by ID.')
    async def reminders_cancel(interaction: discord.Interaction, identity: app_commands.Range[int, 1]):
        if await permitted(bot, interaction):
            bot.features.require_assistant()
            await answer(interaction, json.dumps(bot.social.cancel_reminder(interaction.user.id, identity)))

    tree.add_command(reminders, guild=guild)

    @tree.command(name='wordle', description='Owner: play today’s Wordle now in the chosen channel.', guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def wordle_command(interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not await permitted(bot, interaction, owner=True):
            return
        target = (channel or (bot.get_channel(int(bot.features.settings.wordle_channel))
                              if bot.features.settings.wordle_channel else None) or interaction.channel)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await bot.social.run_wordle(target, force=True)
        except Exception as exc:
            await answer(interaction, bot.features.clean_error(exc))
            return
        await answer(interaction, f'Wordle played in {getattr(target, "mention", target)}.')

    @tree.error
    async def command_error(interaction, error):
        original = getattr(error, 'original', error)
        bot.features.record_status('command_error', {'ok': False, 'error': bot.features.clean_error(original)})
        await answer(interaction, bot.fallback('command_failed', detail=bot.features.clean_error(original)))

    bot.command_tree = tree


async def sync_commands(bot):
    if getattr(bot, 'commands_synced', False):
        return
    try:
        commands = await bot.command_tree.sync(guild=discord.Object(bot.server_id))
        bot.commands_synced = True
        bot.features.record_status('commands', {'ok': True, 'count': len(commands)})
    except discord.HTTPException as exc:
        bot.features.record_status('commands', {'ok': False, 'error': f'Discord command sync returned HTTP {exc.status}. '
                                   'Check the configured server and invite with the applications.commands scope.'})
