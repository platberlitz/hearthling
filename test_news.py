"""Offline checks for the RSS/Atom news roundup."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import testenv  # noqa: F401
import news as news_module
from news import parse_feed, source_lines
from hearthling import Hearthling, open_database
from features import Settings
from test_hearthling import expect_error, expect_async_error
from test_bots import CREDENTIALS, fake_ai
from test_features import CHANNEL_ID


def verdict(text):
    return NS(choices=[NS(message=NS(content=text))])


NOW = datetime.now(timezone.utc)
RSS = f'''<?xml version="1.0"?><rss version="2.0"><channel><title>Lab</title>
<item><title>Release X &amp; friends</title><link>https://lab.example/x</link>
<pubDate>{(NOW - timedelta(hours=2)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
<description>&lt;p&gt;A &lt;b&gt;new&lt;/b&gt; release.&lt;/p&gt;</description></item>
<item><title>Old news</title><link>https://lab.example/old</link><pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
<item><title>No link</title><pubDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
</channel></rss>'''.encode()
ATOM = f'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Build dropped</title><link rel="alternate" href="https://blog.example/w"/>
<updated>{(NOW - timedelta(hours=1)).isoformat()}</updated><summary>Open build.</summary></entry>
</feed>'''.encode()


async def main():
    items = parse_feed('Lab', RSS)
    assert [item['title'] for item in items] == ['Release X & friends', 'Old news']
    assert items[0]['summary'] == 'A new release.' and items[0]['url'] == 'https://lab.example/x'
    atom = parse_feed('Blog', ATOM)
    assert atom[0]['url'] == 'https://blog.example/w' and atom[0]['summary'] == 'Open build.'
    expect_error(ValueError, lambda: parse_feed('Broken', b'<rss><item>'))

    many = [{'title': 'T' * 150, 'source': 'S', 'url': 'https://e.example/' + 'p' * 200 + str(n)} for n in range(20)]
    pieces = source_lines(many)
    assert len(pieces) > 1 and all(len(piece) <= 1900 for piece in pieces) and pieces[0].startswith('**Sources**')
    assert sum(piece.count('<https://') for piece in pieces) == 20

    async def fake_fetch(source, url):
        if source == 'blog.example':
            raise ValueError('down')
        return parse_feed(source, RSS if source == 'lab.example' else ATOM)
    with patch('news.fetch_feed', new=fake_fetch):
        found, failed = await news_module.collect(('https://lab.example/rss', 'https://blog.example/atom', 'https://other.example/atom'))
        assert failed == ['blog.example'] and len(found) == 3
        await expect_async_error(ValueError, news_module.collect(('https://blog.example/atom',)))

    with tempfile.TemporaryDirectory(prefix='news-check-') as directory:
        db = open_database(Path(directory) / 'test.sqlite3')
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=directory) as bot:
            news = bot.news
            assert 'disabled on your account' in news.context()
            for change in ({'news_enabled': True}, {'news_channel': 'invalid'}, {'news_hours': 0}):
                expect_error(ValueError, lambda change=change: Settings.model_validate(bot.features.settings.model_dump() | change))
            bot.features.update_settings({'news_enabled': True, 'news_channel': str(CHANNEL_ID),
                                          'news_feeds': ['https://lab.example/rss', 'https://blog.example/atom']}, {})
            assert f'<#{CHANNEL_ID}>' in news.context() and 'every 6 hours' in news.context()

            stories = items + atom
            channel = NS(id=CHANNEL_ID)
            news.channel = lambda: channel
            bot.providers.reply = AsyncMock(return_value=verdict('Sure: {"items": [2, 1, 2, 99, "x"]}'))
            bot.social.speak = AsyncMock(return_value=True)
            with patch('news.collect', new=AsyncMock(return_value=(stories, ['Blog']))):
                await news.check()
                # Old items are dropped before the model sees them; duplicates and invalid numbers are ignored.
                listing = bot.providers.reply.await_args.args[0][1]['content']
                assert 'Old news' not in listing and 'Build dropped' in listing
                args, kwargs = bot.social.speak.await_args
                assert args[0] is channel and kwargs['kind'] == 'news roundup'
                assert 'Build dropped' in args[1] and 'Do not write links' in args[1]
                # Candidates are newest first, and the model's order sets the numbering.
                assert '1. Release X & friends (Lab) <https://lab.example/x>' in kwargs['after'][0]
                assert '2. Build dropped (Blog) <https://blog.example/w>' in kwargs['after'][0]
                status = news.status()
                assert status['posted_stories'] == 2 and status['first_check'] and status['unreadable_feeds'] == ['Blog']

                # Seen stories are never offered again, so nothing new means no model call and no post.
                bot.providers.reply.reset_mock()
                bot.social.speak.reset_mock()
                await news.check()
                bot.providers.reply.assert_not_awaited()
                bot.social.speak.assert_not_awaited()
                assert news.status()['new_items'] == 0 and not news.status()['first_check']

            # A busy bot keeps the stories unseen and retries soon.
            fresh = [{**atom[0], 'url': 'https://blog.example/next', 'title': 'Next build'}]
            bot.providers.reply = AsyncMock(return_value=verdict('{"items": [1]}'))
            bot.social.speak = AsyncMock(return_value=False)
            with patch('news.collect', new=AsyncMock(return_value=(fresh, []))):
                await news.check()
                assert news.status()['waiting'] and not db.execute(
                    "SELECT 1 FROM news_seen WHERE url='https://blog.example/next'").fetchone()
                # A selection of nothing marks the candidates as seen without posting.
                bot.providers.reply = AsyncMock(return_value=verdict('{"items": []}'))
                bot.social.speak.reset_mock()
                await news.check()
                bot.social.speak.assert_not_awaited()
                assert db.execute("SELECT posted FROM news_seen WHERE url='https://blog.example/next'").fetchone() == (0,)

            # Failures are recorded and retried within half an hour.
            with patch('news.collect', new=AsyncMock(side_effect=ValueError('No news feed could be read'))):
                news.next_check = 0
                await news.tick(now=1000)
                assert news.status()['ok'] is False and news.next_check == 1000 + 1800
            bot.providers.reply = AsyncMock(return_value=verdict('nonsense'))
            with patch('news.collect', new=AsyncMock(return_value=([{**fresh[0], 'url': 'https://blog.example/z'}], []))):
                await expect_async_error(ValueError, news.check())

            # Disabled accounts never fetch.
            bot.features.update_settings({'news_enabled': False}, {})
            with patch('news.collect', new=AsyncMock()) as collect:
                news.next_check = 0
                await news.tick(now=5000)
                collect.assert_not_awaited()
    print('News checks passed.')


if __name__ == '__main__':
    asyncio.run(main())
