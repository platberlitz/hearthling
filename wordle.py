"""Play the daily Wordle with a bot's own model and compose a shareable result."""
import asyncio
from datetime import datetime
import json
import random
import re
from zoneinfo import ZoneInfo

import aiohttp
from openai import APIError


ANSWER_URL = 'https://www.nytimes.com/svc/wordle/v2/{day}.json'
WORDS_URL = 'https://raw.githubusercontent.com/tabatkins/wordle-list/main/words'
GREEN, YELLOW, ABSENT = '🟩', '🟨', '⬛'
GUESS_LIMIT = 6
REPLY_TOKENS = 300
OPENER_CHOICES = 6
STARTERS = ('adieu', 'audio', 'bagel', 'blimp', 'brick', 'cloud', 'crane', 'dwarf', 'flint', 'frost', 'ghost', 'glyph',
            'grape', 'heart', 'jumbo', 'knife', 'lemon', 'mango', 'mirth', 'noble', 'ocean', 'piano', 'plumb', 'quiet',
            'raven', 'roast', 'shark', 'slate', 'stern', 'tiger', 'toast', 'trick', 'ultra', 'vapor', 'whisk', 'yacht')
NO_THINKING = {'thinking': {'type': 'disabled'}}
SYSTEM = ('You are playing Wordle. Guess a five-letter English word. Each reply is judged letter by letter: '
          '🟩 means the right letter in the right place, 🟨 means the right letter in the wrong place, '
          '⬛ means the letter is not in the word at all. Reply with exactly one five-letter word and nothing else. '
          'Never repeat a word you already tried.')

_words = None
_words_lock = asyncio.Lock()
_plain = set()


async def _text(url):
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise ValueError(f'The daily Wordle source returned HTTP {response.status}.')
            return await response.text()


def today():
    """The puzzle's official New York date."""
    return datetime.now(ZoneInfo('America/New_York')).date().isoformat()


async def daily():
    """Today's New York date, answer and displayed puzzle number."""
    day = today()
    data = json.loads(await _text(ANSWER_URL.format(day=day)))
    answer = str(data.get('solution', '')).strip().lower()
    if not re.fullmatch(r'[a-z]{5}', answer):
        raise ValueError('The daily source did not return a five-letter answer.')
    return day, answer, int(data['days_since_launch']) + 1


async def word_list():
    """The official guess list, cached; None when unavailable so play still works unchecked."""
    global _words
    if _words is not None:
        return _words
    async with _words_lock:
        if _words is not None:
            return _words
        try:
            lines = (await _text(WORDS_URL)).splitlines()
        except Exception:
            return None
        found = frozenset(word for word in (line.strip().lower() for line in lines) if re.fullmatch(r'[a-z]{5}', word))
        if len(found) < 100:
            return None
        _words = found
    return _words


def marks(guess, answer):
    """Colour one guess: exact passes first so duplicate letters are scored correctly."""
    guess, answer = guess.lower(), answer.lower()
    result = [ABSENT] * 5
    remaining = list(answer)
    for index, letter in enumerate(guess):
        if index < len(answer) and letter == answer[index]:
            result[index] = GREEN
            remaining[index] = None
    for index, letter in enumerate(guess):
        if result[index] == ABSENT and letter in remaining:
            result[index] = YELLOW
            remaining[remaining.index(letter)] = None
    return result


def first_guess(text, used, words):
    for word in re.findall(r'[A-Za-z]{5}', text or ''):
        word = word.lower()
        if word not in used and (not words or word in words):
            return word
    return None


def share(number, history, answer):
    won = bool(history) and history[-1][0] == answer
    header = f'Wordle {number:,} {len(history)}/{GUESS_LIMIT}' if won else f'Wordle {number:,} X/{GUESS_LIMIT}'
    return '\n'.join([header] + [''.join(row) for _, row in history])


def clues(history):
    """Summarise the colour rows so the model does not have to re-derive them."""
    exact, present, seen = {}, set(), set()
    for guess, row in history:
        for index, (letter, colour) in enumerate(zip(guess, row)):
            seen.add(letter)
            if colour == GREEN:
                exact[index] = letter
            elif colour == YELLOW:
                present.add(letter)
    lines = []
    if exact:
        lines.append('right letter, right place: ' + ', '.join(f'{index + 1}={letter}' for index, letter in sorted(exact.items())))
    wrong = present - set(exact.values())
    if wrong:
        lines.append('right letter, wrong place: ' + ', '.join(sorted(wrong)))
    absent = sorted(seen - set(exact.values()) - present)
    if absent:
        lines.append('not in the word: ' + ', '.join(absent))
    return '; '.join(lines)


async def ask(bot, messages):
    """A Wordle reply with hidden reasoning switched off, unless the provider rejects the switch."""
    key = getattr(bot, 'bot_id', None)
    options = {} if key in _plain else {'extra_body': NO_THINKING}
    try:
        return await bot.providers.reply(messages, max_tokens=REPLY_TOKENS, **options)
    except APIError as exc:
        if not options or getattr(exc, 'status_code', None) not in {400, 422}:
            raise
        _plain.add(key)
        return await bot.providers.reply(messages, max_tokens=REPLY_TOKENS)


def correction(text, used, words):
    """Say what was wrong with an unusable reply so the model can fix it on the retry."""
    candidate = first_guess(text, set(), None)
    if candidate is None:
        detail = 'That reply did not contain a five-letter word'
    elif candidate in used:
        detail = f'"{candidate}" was already tried'
    else:
        detail = f'"{candidate}" is not a real five-letter word'
    return detail + '. Reply with exactly one different five-letter English word and nothing else.'


def openers(seed, words, answer):
    """A per-bot, per-day handful of opening words: models without temperature otherwise all open the same way."""
    pool = sorted(word for word in (words or STARTERS) if len(set(word)) == 5 and word != answer)
    return random.Random(seed).sample(pool, min(OPENER_CHOICES, len(pool)))


async def play(bot, answer, words, day=None):
    """Up to six model guesses. Empty history means the model never produced a legal word."""
    history, used = [], set()
    seed = f'{day}:{getattr(bot, "bot_id", "")}' if day else None
    choices = ', '.join(openers(seed, words, answer))
    for turn in range(1, GUESS_LIMIT + 1):
        if history:
            shown = '\n'.join(f'{index}. {guess} {"".join(row)}' for index, (guess, row) in enumerate(history, 1))
            prompt = (f'Your previous guesses:\n{shown}\n\nClues: {clues(history)}\n\n'
                      f'Reply with guess {turn} of {GUESS_LIMIT}: one new five-letter English word, nothing else.')
        else:
            prompt = (f'Guess 1 of {GUESS_LIMIT}. Today you open with one of these words: {choices}. '
                      'Pick whichever you like and reply with that one word, nothing else.')
        base = prompt
        for _ in range(3):
            response = await ask(bot, [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}])
            content = getattr(response.choices[0].message, 'content', '') or ''
            guess = first_guess(content, used, words)
            if guess:
                break
            prompt = base + '\n' + correction(content, used, words)
        if not guess:
            break
        used.add(guess)
        history.append((guess, marks(guess, answer)))
        if guess == answer:
            break
    return history
