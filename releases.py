"""Announce published GitHub releases with durable tracking and interrupted-send recovery."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import time
from urllib.parse import quote

import aiohttp
import discord


CHECK_SECONDS = 300


def api_url(repository):
    return f'https://api.github.com/repos/{repository}/releases'


async def published_releases(repository):
    """Fetch every page before changing the baseline; a partial response is not a snapshot."""
    releases = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers={
            'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'Hearthling-release-monitor'}) as session:
        for page in range(1, 101):
            async with session.get(api_url(repository), params={'per_page': 100, 'page': page}) as response:
                if response.status != 200:
                    raise ValueError(f'GitHub releases for {repository} returned HTTP {response.status}.')
                items = await response.json()
            if not isinstance(items, list):
                raise ValueError('GitHub did not return a release list.')
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get('draft'), bool):
                    raise ValueError('GitHub returned an invalid release.')
                if item['draft'] or not item.get('published_at'):
                    continue
                tag = item.get('tag_name')
                if not isinstance(tag, str) or not tag.strip():
                    raise ValueError('GitHub returned a release without a tag.')
                published = datetime.fromisoformat(item['published_at'].replace('Z', '+00:00'))
                releases[tag] = {'tag': tag, 'name': item.get('name') or tag,
                                 'body': item.get('body') or '', 'prerelease': bool(item.get('prerelease')),
                                 'published_at': published.isoformat()}
            if len(items) < 100:
                return sorted(releases.values(), key=lambda item: (item['published_at'], item['tag']))
    raise ValueError('GitHub release pagination exceeded 100 pages; no snapshot was saved.')


def release_embed(release, marker, repository):
    url = f'https://github.com/{repository}/releases/tag/{quote(release["tag"], safe="")}'
    body = release['body'].strip() or 'Open the release for details and downloads.'
    if len(body) > 4000:
        body = body[:3900].rstrip() + '\n\nMore in the full release notes; open the title above.'
    embed = discord.Embed(title=release['name'][:256], url=url, description=body,
                          colour=0xE5A56F, timestamp=datetime.fromisoformat(release['published_at']))
    embed.set_footer(text=marker)
    return embed


class ReleaseMonitor:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db
        self.task = None
        self.next_check = 0
        self.lock = asyncio.Lock()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS release_feeds (scope TEXT PRIMARY KEY, initialised_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS release_posts (
                scope TEXT NOT NULL, tag TEXT NOT NULL, payload TEXT NOT NULL,
                message_id TEXT, attempted_at REAL, PRIMARY KEY (scope, tag)
            );
        ''')

    def context(self):
        """Give conversation the account's real automation state, without asking it to poll."""
        config = self.bot.features.settings
        if not config.releases_enabled:
            return 'GitHub release announcements are disabled on your account. The owner can enable them in dashboard Automation.'
        identity = config.releases_channel
        initialised = {repository: bool(self.db.execute('SELECT 1 FROM release_feeds WHERE scope=?',
                                                        (f'{repository}:{self.bot.server_id}:{identity}',)).fetchone())
                       for repository in config.releases_repositories}
        paused = (config.server_paused or not config.server_replies or not self.bot.social.available()
                  or identity in config.channel_blacklist
                  or (config.channel_whitelist_enabled and identity not in config.channel_whitelist))
        row = self.db.execute("SELECT value FROM settings WHERE name='status:releases'").fetchone()
        status = json.loads(row[0]) if row else None
        if status and status.get('channel_id', identity) != identity:
            status = None
        state = {'enabled': True, 'channel_id': identity, 'baseline_saved': initialised,
                 'paused_by_access_or_schedule': paused, 'last_check': status}
        links = ', '.join(f'https://github.com/{repository}' for repository in config.releases_repositories)
        return (f'You have a built-in GitHub release monitor for {links}. '
                f'It checks every five minutes and automatically posts new published releases, release notes and links to <#{identity}>. '
                'It includes labelled pre-releases; drafts and bare tags wait for a published GitHub release. '
                'Existing releases are recorded on the first check without posting; saved tag tracking prevents repeat posts after restarts. '
                'This background task runs independently of conversation and requires no chat tool call. '
                'Use this current runtime state over older conversation claims about your capabilities. '
                'When asked to monitor these repositories, explain the existing setup and its actual state. '
                'The owner changes its settings in dashboard Automation. Claim a manual check, setting change or announcement only when actually performed. '
                'Current release monitor state: ' + json.dumps(state))

    def channel(self):
        config = self.bot.features.settings
        if (not config.releases_enabled or not config.releases_channel or not config.releases_repositories
                or not self.bot.is_ready() or not self.bot.accepting_messages
                or config.server_paused or not config.server_replies or not self.bot.social.available()):
            return None
        identity = config.releases_channel
        if identity in config.channel_blacklist or (config.channel_whitelist_enabled and identity not in config.channel_whitelist):
            return None
        channel = self.bot.get_channel(int(identity))
        if channel is None or getattr(channel, 'guild', None) is None or channel.guild.id != self.bot.server_id:
            raise ValueError('Choose a release channel in the configured server.')
        permissions = channel.permissions_for(channel.guild.me)
        if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links, permissions.read_message_history)):
            raise ValueError('Release announcements need View Channel, Send Messages, Embed Links and Read Message History.')
        return channel

    async def check(self):
        async with self.lock:
            for repository in list(self.bot.features.settings.releases_repositories):
                if await self.check_repository(repository) is None:
                    return

    async def check_repository(self, repository):
        channel = self.channel()
        if channel is None:
            return None
        scope = f'{repository}:{channel.guild.id}:{channel.id}'
        releases = await published_releases(repository)
        initialised = self.db.execute('SELECT 1 FROM release_feeds WHERE scope=?', (scope,)).fetchone()
        with self.db:
            for release in releases:
                self.db.execute('INSERT OR IGNORE INTO release_posts (scope,tag,payload,message_id) VALUES (?,?,?,?)',
                                (scope, release['tag'], json.dumps(release), None if initialised else 'baseline'))
            if not initialised:
                self.db.execute('INSERT INTO release_feeds VALUES (?,?)', (scope, time.time()))
        sent = 0
        rows = self.db.execute('SELECT tag,payload,attempted_at FROM release_posts WHERE scope=? AND message_id IS NULL '
                               'ORDER BY json_extract(payload, \'$.published_at\'),tag', (scope,)).fetchall()
        for tag, payload, attempted_at in rows:
            current = self.channel()
            if current is None or current.id != channel.id or current.guild.id != channel.guild.id:
                return None
            marker = 'Release ' + hashlib.sha256(f'{scope}:{tag}'.encode()).hexdigest()[:24]
            message = None
            if attempted_at is not None:
                # A send can reach Discord before its response or local commit reaches us.
                # If history is unavailable, fail closed and retry the lookup next time.
                after = discord.Object(id=discord.utils.time_snowflake(
                    datetime.fromtimestamp(attempted_at - 60, timezone.utc)))
                async for candidate in channel.history(limit=None, after=after):
                    if candidate.author.id == self.bot.bot_id and any(embed.footer.text == marker for embed in candidate.embeds):
                        message = candidate
                        break
            if message is None:
                current = self.channel()
                if current is None or current.id != channel.id or current.guild.id != channel.guild.id:
                    return None
                with self.db:
                    self.db.execute('UPDATE release_posts SET attempted_at=? WHERE scope=? AND tag=?', (time.time(), scope, tag))
                release = json.loads(payload)
                label = 'pre-release' if release['prerelease'] else 'release'
                message = await channel.send(f'A new {repository} {label} is out.',
                                             embed=release_embed(release, marker, repository), allowed_mentions=discord.AllowedMentions.none())
            with self.db:
                self.db.execute('UPDATE release_posts SET message_id=? WHERE scope=? AND tag=?', (str(message.id), scope, tag))
            sent += 1
        self.bot.features.record_status('releases', {'ok': True, 'repository': repository,
            'channel_id': str(channel.id), 'initialised': not bool(initialised), 'published_releases': len(releases), 'delivered': sent})
        logging.info('Releases checked: repository=%s channel=%s releases=%s delivered=%s baseline=%s',
                     repository, channel.id, len(releases), sent, not bool(initialised))
        return sent

    async def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if now < self.next_check or not self.bot.features.settings.releases_enabled:
            return
        self.next_check = now + CHECK_SECONDS
        try:
            await self.check()
        except Exception as exc:
            error = self.bot.features.clean_error(exc)
            self.bot.features.record_status('releases', {'ok': False, 'error': error})
            logging.warning('Release check failed: %s', error)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name=f'releases-{self.bot.bot_id}')

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(30)

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
