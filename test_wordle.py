"""Offline checks for the daily Wordle game."""
import asyncio
from datetime import datetime
from itertools import permutations
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
from openai import APIStatusError

import testenv  # noqa: F401
from hearthling import APPLICATION_ID, Hearthling, SERVER_ID, open_database
from features import Settings
from test_hearthling import ai_reply, expect_async_error, expect_error
from test_bots import CREDENTIALS, fake_ai
from test_features import CHANNEL_ID
import wordle
from wordle import ABSENT, GREEN, YELLOW, marks, share


WORDS = ['%s%seee' % (chr(97 + number // 26), chr(97 + number % 26)) for number in range(200)] + ['crane', 'nacre', 'snowy']


def routed(answer='nacre'):
    async def fetch(url):
        if 'nytimes' in url:
            return json.dumps({'solution': answer, 'days_since_launch': 1917})
        return '\n'.join(WORDS)
    return fetch


class Scripted:
    """Stands in for bot.providers in direct play() checks."""

    def __init__(self, replies):
        self.replies, self.prompts, self.options = list(replies), [], []

    async def reply(self, messages, **options):
        self.prompts.append(messages[-1]['content'])
        self.options.append(options)
        return ai_reply(content=self.replies.pop(0) if self.replies else 'zzzzz')


class Strict:
    """A provider that rejects the reasoning switch, like strict OpenAI-compatible servers."""

    def __init__(self, replies):
        self.replies, self.attempts = list(replies), 0

    async def reply(self, messages, **options):
        self.attempts += 1
        if 'extra_body' in options:
            raise APIStatusError('Unknown field.', response=httpx.Response(400, request=httpx.Request('POST', 'https://example.test')), body=None)
        return ai_reply(content=self.replies.pop(0))


async def main():
    assert marks('crane', 'crane') == [GREEN] * 5
    assert marks('crane', 'nacre') == [YELLOW, YELLOW, YELLOW, YELLOW, GREEN]
    assert marks('geese', 'eagle') == [YELLOW, YELLOW, ABSENT, ABSENT, GREEN]
    assert marks('eerie', 'eagle') == [GREEN, ABSENT, ABSENT, ABSENT, GREEN]
    won = [('crane', marks('crane', 'nacre')), ('nacre', marks('nacre', 'nacre'))]
    assert share(1918, won, 'nacre') == 'Wordle 1,918 2/6\n🟨🟨🟨🟨🟩\n🟩🟩🟩🟩🟩'
    lost = [(word, marks(word, 'nacre')) for word in ('geese', 'eerie', 'civic', 'bully', 'puppy', 'waltz')]
    assert share(1, lost, 'nacre').startswith('Wordle 1 X/6\n')
    assert share(1, [], 'nacre') == 'Wordle 1 X/6'

    wordle._words = None
    wordle._plain.clear()
    calls = []
    async def fetch(url):
        calls.append(url)
        return await routed()(url)
    with patch.object(wordle, '_text', side_effect=fetch):
        day, answer, number = await wordle.daily()
        assert answer == 'nacre' and number == 1918
        assert day == datetime.now(ZoneInfo('America/New_York')).date().isoformat()
        words = await wordle.word_list()
        assert words and 'crane' in words and len(words) >= 100
        assert await wordle.word_list() is words
        assert sum('raw.githubusercontent' in url for url in calls) == 1
    wordle._words = None
    with patch.object(wordle, '_text', new=AsyncMock(return_value='')):
        assert await wordle.word_list() is None
    wordle._words = None
    with patch.object(wordle, '_text', side_effect=RuntimeError('down')):
        assert await wordle.word_list() is None
    with patch.object(wordle, '_text', side_effect=RuntimeError('down')):
        await expect_async_error(RuntimeError, wordle.daily())

    scripted = Scripted(['nope', 'crane', 'nacre'])
    history = await wordle.play(NS(providers=scripted), 'nacre', None)
    assert history == [('crane', marks('crane', 'nacre')), ('nacre', [GREEN] * 5)]
    assert all('nacre' not in prompt for prompt in scripted.prompts)
    assert all(options['max_tokens'] >= 200 for options in scripted.options)
    assert all('extra_body' in options for options in scripted.options)
    strict = Strict(['crane', 'nacre', 'nacre'])
    strict_bot = NS(providers=strict, bot_id='strict-bot')
    assert [guess for guess, _ in await wordle.play(strict_bot, 'nacre', None)] == ['crane', 'nacre']
    assert strict.attempts == 3
    assert [guess for guess, _ in await wordle.play(strict_bot, 'nacre', None)] == ['nacre']
    assert strict.attempts == 4
    patient = Scripted(['...', '...', 'crane', 'nacre'])
    assert [guess for guess, _ in await wordle.play(NS(providers=patient), 'nacre', None)] == ['crane', 'nacre']
    assert len(patient.prompts) == 4
    assert 'did not contain a five-letter word' in patient.prompts[1]
    assert 'Guess 1 of 6' in patient.prompts[2]
    repeat = Scripted(['crane', 'crane', 'nacre'])
    assert [guess for guess, _ in await wordle.play(NS(providers=repeat), 'nacre', None)] == ['crane', 'nacre']
    assert 'already tried' in repeat.prompts[2]
    assert 'Your previous guesses' in repeat.prompts[2]
    assert 'right letter, right place: 5=e' in repeat.prompts[2]
    assert 'right letter, wrong place: a, c, n, r' in repeat.prompts[2]
    exhausted = Scripted(['...', '...', '...'])
    assert await wordle.play(NS(providers=exhausted), 'nacre', None) == []
    assert len(exhausted.prompts) == 3
    repeated = await wordle.play(NS(providers=Scripted(['aaeee', 'aaeee', 'abeee'])), 'nacre', frozenset(WORDS))
    assert [guess for guess, _ in repeated] == ['aaeee', 'abeee']

    pool = frozenset(''.join(letters) for letters in permutations('abcdefgh', 5)) | {'crane'}
    days = ('2026-10-10', '2026-10-11')
    first = {(day, bot): wordle.openers(f'{day}:{bot}', pool, 'crane') for day in days for bot in range(6)}
    assert len({tuple(choice) for choice in first.values()}) == len(first)
    assert first[(days[0], 1)] == wordle.openers(f'{days[0]}:1', pool, 'crane')
    assert all('crane' not in choice and len(choice) == wordle.OPENER_CHOICES for choice in first.values())
    assert 'nacre' not in wordle.openers('any', None, 'nacre') and wordle.openers('any', None, 'nacre')
    seeded = Scripted(['crane'])
    await wordle.play(NS(providers=seeded, bot_id=7), 'crane', pool, days[0])
    assert ', '.join(wordle.openers(f'{days[0]}:7', pool, 'crane')) in seeded.prompts[0]

    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix='wordle-check-') as directory:
        db = open_database(':memory:')
        async with Hearthling(fake_ai(), db, credentials=CREDENTIALS, data_dir=directory) as bot:
            bot._connection.user = NS(id=APPLICATION_ID, bot=True, name='Hearthling', display_name='Hearthling')
            config = bot.features
            for changes in ({'wordle_enabled': True}, {'wordle_time': '25:00'}, {'wordle_time': '9:00'}, {'wordle_channel': 'nope'}):
                expect_error(ValueError, lambda changes=changes: Settings.model_validate(config.settings.model_dump() | changes))
            assert Settings.model_validate(config.settings.model_dump() | {
                'wordle_enabled': True, 'wordle_channel': str(CHANNEL_ID)}).wordle_enabled
            config.update_settings({'recent_context': 0, 'wordle_enabled': True, 'wordle_channel': str(CHANNEL_ID),
                                    'wordle_time': '00:00'}, {})
            member = NS(id=APPLICATION_ID, bot=True, name='Hearthling', display_name='Hearthling', _roles=[])

            def permissions_for(who):
                if not hasattr(who, '_roles'):
                    raise AttributeError("'%s' object has no attribute '_roles'" % type(who).__name__)
                return NS(view_channel=True, read_message_history=True)

            guild = NS(id=SERVER_ID, members=[member], get_member=lambda uid: member if int(uid) == APPLICATION_ID else None)
            channel = NS(id=CHANNEL_ID, guild=guild, send=AsyncMock(), permissions_for=permissions_for)
            wordle._words = None
            with patch.object(wordle, '_text', side_effect=routed()), \
                    patch.object(bot.ai.chat.completions, 'create',
                                 new=AsyncMock(side_effect=[ai_reply(content='  Crane! '), ai_reply(content='nacRe'),
                                                            ai_reply(content='Wordle 1,918 2/6\n🟨🟨🟨🟨🟩\n🟩🟩🟩🟩🟩\nGot it with ||nacRe|| at the end.')])) as create:
                assert await bot.social.run_wordle(channel)
                assert create.await_count == 3
                text = channel.send.call_args.args[0]
                assert text.startswith('Wordle 1,918 2/6\n🟨🟨🟨🟨🟩\n🟩🟩🟩🟩🟩\n')
                assert text.count('Wordle 1,918') == 1 and text.count('🟩🟩🟩🟩🟩') == 1
                assert 'Got it with ||nacRe|| at the end.' in text
                assert 'spoiler' in str(create.await_args_list[2].kwargs['messages'])
                assert 'your guesses in order were crane, nacre' in str(create.await_args_list[2].kwargs['messages'])
                game = bot.social.state('wordle-words')
                assert game['guesses'] == ['crane', 'nacre'] and game['won'] is True and game['number'] == 1918
                assert 'crane, nacre' in bot.social.wordle_note(CHANNEL_ID)
                assert '||word||' in bot.social.wordle_note(CHANNEL_ID)
                assert bot.social.wordle_note(CHANNEL_ID + 1, 'hello there') == ''
                assert 'crane' in bot.social.wordle_note(CHANNEL_ID + 1, 'how was your Wordle?')
                bot.social.save_state('wordle-words', game | {'day': '2000-01-01'})
                assert bot.social.wordle_note(CHANNEL_ID) == ''
                assert bot.social.state('wordle:' + str(CHANNEL_ID)) is not None
                assert not await bot.social.run_wordle(channel)
                assert create.await_count == 3
            with patch.object(bot, 'is_ready', return_value=True), patch.object(bot, 'get_channel', return_value=channel), \
                    patch.object(bot.social, 'run_wordle', new=AsyncMock(return_value=True)) as run:
                now = time.time()
                await bot.social.tick(now)
                run.assert_awaited_once()
                await bot.social.tick(now + 60)
                run.assert_awaited_once()
            with patch.object(bot, 'is_ready', return_value=True), patch.object(bot, 'get_channel', return_value=channel), \
                    patch.object(bot.social, 'run_wordle', new=AsyncMock(side_effect=ValueError('down'))) as run:
                await bot.social.tick(now + 86401)
                run.assert_awaited_once()
                await bot.social.tick(now + 86401 + 60)
                run.assert_awaited_once()
        db.close()
    print('PASS: Wordle colours with duplicate letters, share cards, daily source, cached word list, model guesses, once-per-day schedule and retry throttle.')


if __name__ == '__main__':
    asyncio.run(main())
