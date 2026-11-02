"""Public identities, explicitly requested pings and permission-free personal colours."""
import asyncio
import json
import re
import time

import discord

from conversation import profile
from features import FeatureError


def names(member):
    return {value.casefold() for field in ('name', 'global_name', 'nick', 'display_name')
            if isinstance(value := getattr(member, field, None), str) and value}


def role_ids(member):
    # A fresh member snapshot contains roles not yet present in the gateway cache.
    return set(member._roles) if hasattr(member, '_roles') else {role.id for role in member.roles}


class People:
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        bot.db.executescript('''
            CREATE TABLE IF NOT EXISTS member_pings (
                request_id TEXT, channel_id TEXT, target_id TEXT, author_id TEXT, created_at INTEGER, result TEXT,
                PRIMARY KEY(request_id, channel_id, target_id));
            CREATE TABLE IF NOT EXISTS personal_colours (
                guild_id TEXT, user_id TEXT, role_id TEXT, state TEXT, PRIMARY KEY(guild_id, user_id));
        ''')

    @property
    def db(self):
        return self.bot.manager.db if getattr(self.bot, 'manager', None) else self.bot.db

    def guild(self, request, action):
        if action not in self.bot.features.allowed_actions(request):
            raise PermissionError('This member service is unavailable to this speaker or bot type.')
        guild = self.bot.get_guild(self.bot.server_id)
        if guild is None:
            raise ValueError('The configured server is unavailable.')
        return guild

    async def lookup(self, request, query, limit=10):
        guild = self.guild(request, 'lookup_members')
        query = query.strip()
        if not query:
            raise ValueError('Give a username, global name, server nickname, mention or member ID.')
        match = re.fullmatch(r'(?:<@!?)?([0-9]{15,21})>?', query)
        if match:
            candidates = [await guild.fetch_member(int(match[1]))]
        else:
            candidates = list(guild.members) if guild.chunked else await guild.query_members(query=query, limit=100)
            exact = [member for member in candidates if query.casefold() in names(member)]
            candidates = exact or [member for member in candidates if any(query.casefold() in value for value in names(member))]
        if request.guild:
            candidates = [member for member in candidates if request.channel.permissions_for(member).view_channel]
        return {'members': [profile(member) for member in candidates[:limit]], 'ambiguous': len(candidates) != 1,
                'instruction': 'Use an exact name or returned ID. Never guess between matching members.'}

    async def ping(self, request, target):
        async with self.lock:
            return await self._ping(request, target)

    async def _ping(self, request, target):
        guild = self.guild(request, 'ping_member')
        if not re.search(r'\b(ping|tag|mention|summon|notify|alert)\b', request.content, re.I):
            raise PermissionError('The current speaker must explicitly request a member ping.')
        member = await self.bot.member(guild, target)
        if member.bot:
            raise ValueError('Requested member pings are for people, not bot accounts.')
        if not request.channel.permissions_for(member).view_channel:
            raise PermissionError('That person cannot see this channel; choose a shared conversation.')
        key = (str(request.id), str(request.channel.id), str(member.id))
        previous = self.db.execute('SELECT result FROM member_pings WHERE request_id=? AND channel_id=? AND target_id=?', key).fetchone()
        if previous:
            if previous[0]:
                return json.loads(previous[0])
            raise FeatureError('That ping has an unconfirmed outcome and will not be repeated.', uncertain=True)
        if self.db.execute('SELECT count(*) FROM member_pings WHERE request_id=?', (key[0],)).fetchone()[0] >= 3:
            raise ValueError('At most three people can be pinged by one request.')
        last = self.db.execute('SELECT max(created_at) FROM member_pings WHERE author_id=? AND target_id=?',
                               (str(request.author.id), str(member.id))).fetchone()[0]
        if last and time.time() - last < 60:
            raise ValueError('Wait one minute before asking to ping that person again.')
        reference = request.to_reference(fail_if_not_exists=False) if isinstance(request, discord.Message) else None
        if not reference and getattr(request, 'interaction', None):
            reference = (await request.interaction.original_response()).to_reference(fail_if_not_exists=False)
        self.guild(request, 'ping_member')
        self.db.execute('INSERT INTO member_pings VALUES (?,?,?,?,?,NULL)', (*key, str(request.author.id), int(time.time())))
        self.db.commit()
        sent = await request.channel.send(f'<@{member.id}>', reference=reference, mention_author=False,
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(member.id)], roles=False, everyone=False, replied_user=False))
        result = {'member': profile(member), 'message_id': str(sent.id), 'notification_sent': True}
        self.db.execute('UPDATE member_pings SET result=? WHERE request_id=? AND channel_id=? AND target_id=?', (json.dumps(result), *key))
        self.db.commit()
        return result

    async def colour(self, request, colour):
        guild = self.guild(request, 'set_my_colour')
        if not colour or not re.fullmatch(r'#[0-9a-fA-F]{6}', colour):
            raise ValueError('Choose a colour as #RRGGBB.')
        number = int(colour[1:], 16)
        if not number:
            raise ValueError('Discord uses #000000 for no colour. Choose #010101 for near-black.')
        if not re.search(r'\b(colou?r|recolou?r|role|name)\b', request.content, re.I):
            raise PermissionError('Ask explicitly to change your personal role colour.')
        async with self.lock:
            member = await guild.fetch_member(request.author.id)
            me = await guild.fetch_member(self.bot.bot_id)
            if not me.guild_permissions.manage_roles:
                raise PermissionError('This assistant needs Manage Roles permission.')
            roles = await guild.fetch_roles()
            top = max(role for role in roles if role.id in role_ids(me))
            key = (str(guild.id), str(member.id))
            stored = self.db.execute('SELECT role_id,state FROM personal_colours WHERE guild_id=? AND user_id=?', key).fetchone()
            if stored and not stored[0]:
                raise FeatureError('A previous personal-role creation is unconfirmed. Ask the owner to inspect it before another attempt.', uncertain=True)
            role = next((role for role in roles if stored and str(role.id) == stored[0]), None)
            if stored and not role:
                raise ValueError('Your recorded personal colour role was removed. Ask the owner to inspect the saved assignment.')
            coloured = [item for item in roles if item.id in role_ids(member) and item.colour.value and item != role]
            if any(item >= top for item in coloured):
                raise PermissionError('A higher coloured role would override your personal colour. The owner must move this assistant above it first.')
            if role:
                await self.check_role(guild, role, member.id, top)
            else:
                name = f'colour-{member.id}'
                if any(item.name == name for item in roles):
                    raise ValueError('An untracked role already has your personal-role name. Ask the owner to inspect it; it was not changed.')
                self.guild(request, 'set_my_colour')
                self.db.execute('INSERT INTO personal_colours VALUES (?,?,NULL,?)', (*key, 'creating'))
                self.db.commit()
                try:
                    role = await guild.create_role(name=name, permissions=discord.Permissions.none(), colour=discord.Colour(number),
                                                   hoist=False, mentionable=False, reason=f'Personal colour requested by {member.id}; request {request.id}')
                except discord.HTTPException as exc:
                    if exc.status < 500:
                        self.db.execute('DELETE FROM personal_colours WHERE guild_id=? AND user_id=? AND role_id IS NULL', key)
                        self.db.commit()
                    raise
                self.db.execute('UPDATE personal_colours SET role_id=?,state=? WHERE guild_id=? AND user_id=?', (str(role.id), 'created', *key))
                self.db.commit()
            try:
                roles = await guild.fetch_roles()
                top = max(item for item in roles if item.id in role_ids(me))
                current = next((item for item in roles if item.id == role.id), None)
                if current is None:
                    raise ValueError('The personal role disappeared before its colour could be changed.')
                role = current
                await self.check_role(guild, role, member.id, top)
                self.guild(request, 'set_my_colour')
                reason = f'Own personal colour requested by {member.id}; request {request.id}'
                # Role.edit(position=...) can omit a new role when the gateway cache is stale.
                await guild.edit_role_positions(positions={role: max(1, top.position - 1)}, reason=reason)
                role = await role.edit(colour=discord.Colour(number), reason=reason)
                await self.check_role(guild, role, member.id, top)
                self.guild(request, 'set_my_colour')
                if role.id not in role_ids(member):
                    await member.add_roles(role, reason=f'Own permission-free colour role; request {request.id}')
                fresh = await guild.fetch_member(member.id)
                roles = await guild.fetch_roles()
                visible = max((item for item in roles if item.id in role_ids(fresh) and item.colour.value), default=None)
                actual = next((item for item in roles if item.id == role.id), None)
                if actual is None:
                    raise ValueError('The personal role disappeared before its final state could be verified.')
                self.db.execute('UPDATE personal_colours SET state=? WHERE guild_id=? AND user_id=?', ('assigned', *key))
                self.db.commit()
                return {'member_id': str(member.id), 'role_id': str(role.id), 'colour': f'#{actual.colour.value:06x}',
                        'visible': bool(visible and visible.id == role.id and actual.colour.value == number),
                        'shared_roles_changed': False}
            except (discord.HTTPException, OSError, asyncio.TimeoutError, ValueError, PermissionError) as exc:
                raise FeatureError(f'Personal role {role.id} exists, but the remaining colour/assignment steps could not be confirmed: '
                                   + self.bot.features.clean_error(exc), uncertain=True) from None

    async def check_role(self, guild, role, member_id, top):
        if role.managed or role.is_default() or role >= top or role.permissions.value or role.hoist or role.mentionable:
            raise PermissionError('That role is no longer a permission-free personal colour role. It was not changed.')
        async for other in guild.fetch_members(limit=None):
            if other.id != member_id and role.id in role_ids(other):
                raise PermissionError('That role is shared with another member. Shared role colours are never changed.')
