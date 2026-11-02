"""Offline release polling, delivery recovery and lifecycle checks."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

import testenv  # noqa: F401
from hearthling import APPLICATION_ID, SERVER_ID, Hearthling, open_database
from bots import BotManager
from features import Settings
from releases import ReleaseMonitor, published_releases, release_embed
from test_hearthling import expect_error, expect_async_error
from test_bots import CREDENTIALS, fake_ai
from test_features import CHANNEL_ID, request

REPO = 'example/project'


def release(tag, day=1, **changes):
    return {'tag': tag, 'name': f'Project {tag}', 'body': 'Release notes', 'prerelease': False,
            'published_at': f'2026-09-{day:02d}T00:00:00+00:00', **changes}


def github(tag, day=1, **changes):
    return {'tag_name': tag, 'name': f'Project {tag}', 'body': 'Release notes', 'draft': False,
            'prerelease': False, 'published_at': f'2026-09-{day:02d}T00:00:00Z', **changes}


async def main():
    pages, calls, status = {}, [], 200

    async def source(request):
        calls.append(int(request.query['page']))
        assert request.query['per_page'] == '100'
        assert request.headers['Accept'] == 'application/vnd.github+json'
        return web.json_response(pages.get(calls[-1], []), status=status)

    app = web.Application()
    app.router.add_get('/releases', source)
    async with TestServer(app) as server:
        with patch('releases.api_url', return_value=str(server.make_url('/releases'))):
            pages[1] = [github(f'v{i}') for i in range(100)]
            pages[2] = [github('beta', 2, prerelease=True), github('draft', draft=True),
                        github('unpublished', published_at=None)]
            values = await published_releases(REPO)
            assert len(values) == 101 and calls == [1, 2] and values[-1]['prerelease']
            status = 503
            await expect_async_error(ValueError, published_releases(REPO))
            status = 200
            pages[1] = {'message': 'invalid response'}
            await expect_async_error(ValueError, published_releases(REPO))
            pages[1] = [github('')]
            await expect_async_error(ValueError, published_releases(REPO))

    card = release_embed(release('v1/preview', body='notes ' * 2000), 'marker', REPO)
    assert len(card.description) <= 4096 and 'full release notes' in card.description
    assert card.url.endswith('v1%2Fpreview') and card.footer.text == 'marker'

    with tempfile.TemporaryDirectory(prefix='releases-check-') as directory:
        path = Path(directory) / 'test.sqlite3'
        db = open_database(path)
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=directory) as bot:
            assert 'disabled on your account' in bot.releases.context()
            for change in ({'releases_enabled': True}, {'releases_channel': 'invalid'}):
                expect_error(ValueError, lambda change=change: Settings.model_validate(bot.features.settings.model_dump() | change))
            bot.features.update_settings({'releases_enabled': True, 'releases_channel': str(CHANNEL_ID),
                                          'releases_repositories': [REPO]}, {})
            member = NS(id=APPLICATION_ID, _roles=[])
            guild = NS(id=SERVER_ID, me=member)
            permissions = NS(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
            sent = []
            fail_before = fail_after = fail_history = False

            async def send(content, **kwargs):
                if fail_before:
                    raise OSError('Discord unavailable')
                assert kwargs['allowed_mentions'].to_dict()['parse'] == []
                message = NS(id=900000000000000000 + len(sent), author=member, embeds=[kwargs['embed']])
                sent.append((content, message))
                if fail_after:
                    raise OSError('Discord response was lost')
                return message

            async def history(**kwargs):
                assert kwargs['limit'] is None and kwargs['after'].id > 0
                if fail_history:
                    raise OSError('History unavailable')
                for _, message in sent:
                    yield message

            def permissions_for(person):
                assert person is member
                return permissions

            channel = NS(id=CHANNEL_ID, guild=guild, permissions_for=permissions_for,
                         send=AsyncMock(side_effect=send), history=history)
            with patch.object(bot, 'is_ready', return_value=True), patch.object(bot, 'get_channel', return_value=channel), \
                    patch('releases.published_releases', new=AsyncMock()) as fetch:
                # A failed initial fetch must never turn subsequent releases into a historical flood.
                fetch.side_effect = OSError('GitHub unavailable')
                await bot.releases.tick(100)
                assert not db.execute('SELECT 1 FROM release_feeds').fetchone()
                fetch.side_effect = None
                fetch.return_value = [release('v1')]
                await bot.releases.tick(200)
                assert fetch.await_count == 1
                await bot.releases.tick(400)
                assert not sent and db.execute('SELECT message_id FROM release_posts').fetchone()[0] == 'baseline'

                # Conversation must receive current runtime capability, even before any announcement.
                question = request()
                question.content = 'Can you monitor project releases and post them here?'
                with patch.object(bot.features.settings, 'recent_context', 0):
                    preview = await bot.converse(question, preview=True)
                prompt = preview['messages'][0]['content']
                assert 'built-in GitHub release monitor' in prompt and f'<#{CHANNEL_ID}>' in prompt
                assert 'requires no chat tool call' in prompt
                assert '"published_releases": 1' in prompt and '"delivered": 0' in prompt
                bot.features.settings.server_paused = True
                assert '"paused_by_access_or_schedule": true' in bot.releases.context()
                bot.features.settings.server_paused = False

                # Arrival, rather than commit/version order, determines whether a published tag is new.
                fetch.return_value = [release('v1'), release('v3', 3), release('v2', 2)]
                await bot.releases.tick(700)
                assert [m.embeds[0].title for _, m in sent] == ['Project v2', 'Project v3']
                fetch.return_value[1]['body'] = 'Edited release notes'
                bot.releases = ReleaseMonitor(bot)
                await bot.releases.check()
                assert len(sent) == 2

                fetch.return_value += [release('v4', 4, prerelease=True)]
                bot.features.settings.server_paused = True
                await bot.releases.check()
                bot.features.settings.server_paused = False
                bot.features.settings.channel_blacklist = [str(CHANNEL_ID)]
                await bot.releases.check()
                bot.features.settings.channel_blacklist = []
                with patch.object(bot.social, 'available', return_value=False):
                    await bot.releases.check()
                guild.id = SERVER_ID + 1
                await expect_async_error(ValueError, bot.releases.check())
                guild.id = SERVER_ID
                permissions.read_message_history = False
                await expect_async_error(ValueError, bot.releases.check())
                permissions.read_message_history = True
                assert len(sent) == 2

                # Persisted pending sends retry; a success followed by a lost response is recovered from history.
                fail_before = True
                await expect_async_error(OSError, bot.releases.check())
                assert len(sent) == 2
                fail_before, fail_after = False, True
                await expect_async_error(OSError, bot.releases.check())
                assert len(sent) == 3 and 'pre-release' in sent[-1][0]
                fail_after, fail_history = False, True
                await expect_async_error(OSError, bot.releases.check())
                assert len(sent) == 3
                fail_history = False
                # Reopen the actual database to exercise process-restart persistence.
                restored_db = open_database(path)
                restored_bot = NS(db=restored_db, features=bot.features, social=bot.social, is_ready=lambda: True,
                                  accepting_messages=True, get_channel=lambda _: channel, bot_id=APPLICATION_ID, server_id=SERVER_ID)
                restored = ReleaseMonitor(restored_bot)
                await restored.check()
                assert len(sent) == 3
                assert restored_db.execute("SELECT message_id FROM release_posts WHERE tag='v4'").fetchone()[0] == str(sent[-1][1].id)
                restored_db.close()

                # Enable from an empty repository, then catch the first published release.
                db.execute('DELETE FROM release_posts')
                db.execute('DELETE FROM release_feeds')
                db.commit()
                fetch.return_value = []
                await bot.releases.check()
                fetch.return_value = [release('v5', 5)]
                await asyncio.gather(bot.releases.check(), bot.releases.check())
                assert len(sent) == 4

            with patch.object(bot.releases, 'tick', new=AsyncMock()) as tick:
                bot.releases.start()
                original = bot.releases.task
                bot.releases.start()
                assert bot.releases.task is original
                await asyncio.sleep(0)
                tick.assert_awaited_once()
                await bot.releases.close()
                assert original.done()

            # New accounts must not inherit the primary account's enabled feed and duplicate posts.
            manager = BotManager(bot)
            with patch('bots.validate_token', new=AsyncMock(return_value='900000000000000222')):
                identity = await manager.add({'name': 'Another bot', 'server_id': str(SERVER_ID), 'token': 'fake'})
            assert not manager.bots[identity].features.settings.releases_enabled
            await manager.close()
        db.close()
    print('PASS: release pagination, baseline, polling interval, ordering, edits, access, retry, interrupted delivery, restart persistence and lifecycle.')


if __name__ == '__main__':
    asyncio.run(main())
