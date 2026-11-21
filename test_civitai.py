"""Check Civitai listings, safety filtering, sign-in and errors offline."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp

import testenv  # noqa: F401
import civitai
from civitai import Civitai, CivitaiError, adult_allowed, image_entry, model_id, sized
from hearthling import Hearthling, OWNER_ID, open_database
from test_hearthling import expect_async_error, expect_error
from test_features import GUEST_ID, request


ORIGINAL = 'https://image.civitai.com/abc/def/original=true/{}.jpeg'


def picture(identity, level='None', kind='image'):
    return {'id': identity, 'url': ORIGINAL.format(identity), 'nsfwLevel': level, 'type': kind, 'username': 'maker',
            'baseModel': 'Flux.3', 'createdAt': '2026-11-20T10:00:00Z', 'meta': {'prompt': '<b>a cat</b> in a hat'},
            'stats': {'likeCount': 3, 'heartCount': 2, 'commentCount': 50}}


def record(identity, nsfw=False):
    return {'id': identity, 'name': f'Model {identity}', 'type': 'Checkpoint', 'nsfw': nsfw, 'creator': {'username': 'maker'},
            'stats': {'downloadCount': 10, 'thumbsUpCount': 4}, 'tags': ['anime', 7], 'description': '<p>Nice &amp; sharp</p>',
            'allowCommercialUse': ['Image'],
            'modelVersions': [{'id': identity * 10, 'name': 'v2', 'baseModel': 'Flux.3', 'publishedAt': '2026-11-19T00:00:00Z',
                               'trainedWords': ['cat style', 3], 'stats': {'downloadCount': 8}},
                              {'id': identity * 10 - 1, 'name': 'v1', 'baseModel': 'Flux.2'}]}


class FakeCivitai:
    """Answers like Civitai's API and records every request."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, *args, **kwargs):
        fake = self

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, url, headers=None, allow_redirects=True):
                address = urlsplit(url)
                fake.calls.append({'path': address.path.removeprefix('/api/v1/'), 'query': parse_qs(address.query), 'headers': headers})
                answer = fake.routes[address.path.removeprefix('/api/v1/')]
                if isinstance(answer, Exception):
                    raise answer
                status, body = answer if isinstance(answer, tuple) else (200, answer)

                async def chunks(size):
                    yield body if isinstance(body, bytes) else json.dumps(body).encode()

                context = MagicMock()
                context.__aenter__.return_value = NS(status=status, reason='Status', content=NS(iter_chunked=chunks))
                return context
        return Session()


def channel_request(nsfw, guild=True):
    return NS(guild=NS(id=1) if guild else None, channel=NS(is_nsfw=lambda: nsfw))


async def main():
    assert model_id(5) == 5 and model_id('see https://civitai.green/models/618692/flux?x=1') == 618692
    expect_error(CivitaiError, lambda: model_id('https://example.com/models/1'))
    assert sized(ORIGINAL.format(1), 'image').endswith('/width=1024/1.jpeg')
    assert sized(ORIGINAL.format(1), 'video').endswith('/transcode=true,width=450/1.jpeg')
    entry = image_entry(picture(4, 'Mature'))
    assert entry['adult'] and entry['reactions'] == 5 and entry['prompt'] == 'a cat in a hat'
    assert entry['source_url'] == 'https://civitai.com/images/4' and entry['original_url'] == ORIGINAL.format(4)
    assert adult_allowed(channel_request(True)) and not adult_allowed(channel_request(False))
    assert not adult_allowed(channel_request(True, guild=False)) and not adult_allowed(NS(guild=NS(id=1), channel=NS()))

    keys = {}
    client = Civitai(lambda: keys)
    fake = FakeCivitai({
        'models': {'items': [record(1), record(2, nsfw=True), record(3), 'junk', {'id': 'bad'}]},
        'models/7': record(7),
        'models/8': record(8, nsfw=True),
        'images': {'items': [picture(11), picture(12, 'X'), picture(13, kind='video'),
                             {'id': 14, 'url': 'https://elsewhere.example/a.png'}]},
        'enums': {'ActiveBaseModel': ['Flux.3', 'Qwen 3']},
        'me': {'username': 'owner-name', 'tier': 'supporter'},
    })
    with patch('civitai.aiohttp.ClientSession', new=fake):
        listing = await client.models(query='  flux ', limit=2)
        call = fake.calls[-1]
        assert call['query']['limit'] == ['4'] and call['query']['nsfw'] == ['false'] and call['query']['query'] == ['flux']
        assert call['query']['sort'] == ['Highest Rated'] and call['query']['period'] == ['AllTime']
        assert 'Authorization' not in call['headers'] and not listing['signed_in']
        assert [model['id'] for model in listing['models']] == [1, 3] and listing['adult_hidden'] == 1
        assert listing['source'] == 'https://civitai.com/models?query=flux' and listing['models'][0]['base_model'] == 'Flux.3'
        cached = await client.models(query='flux', limit=2)
        assert cached == listing and len(fake.calls) == 1

        adult = await client.models(limit=5, sort='newest', model_type='LORA', base_model='Flux.3', adult=True)
        call = fake.calls[-1]
        assert 'nsfw' not in call['query'] and call['query']['limit'] == ['5'] and call['query']['types'] == ['LORA']
        assert call['query']['sort'] == ['Newest'] and call['query']['period'] == ['Week'] and call['query']['baseModels'] == ['Flux.3']
        assert [model['id'] for model in adult['models']] == [1, 2, 3] and adult['adult_hidden'] == 0
        await expect_async_error(CivitaiError, client.models(sort='popular'))

        details = await client.model('https://civitai.com/models/7')
        images_call = fake.calls[-1]
        assert images_call['path'] == 'images' and images_call['query']['modelVersionId'] == ['70']
        assert 'modelId' not in images_call['query'] and images_call['query']['nsfw'] == ['None']
        assert details['tags'] == ['anime'] and details['description'] == 'Nice & sharp' and details['commercial_use'] == ['Image']
        assert [version['id'] for version in details['versions']] == [70, 69] and details['versions'][0]['trigger_words'] == ['cat style']
        assert [image['source_url'] for image in details['sample_images']] == ['https://civitai.com/images/11', 'https://civitai.com/images/13']
        await expect_async_error(CivitaiError, client.model(8))
        assert (await client.model(8, adult=True))['adult']

        pictures = await client.images(limit=3, sort='newest', period='week', adult=True)
        assert [image['kind'] for image in pictures['images']] == ['image', 'image', 'video']
        assert pictures['images'][2]['image_url'].endswith('/transcode=true,width=450/13.jpeg')
        await expect_async_error(CivitaiError, client.images(period='forever'))
        assert await client.base_models() == ['Flux.3', 'Qwen 3']

        await expect_async_error(CivitaiError, client.account())
        keys['civitai_api_key'] = 'FAKE_CIVITAI_KEY'
        assert await client.account() == {'signed_in': True, 'username': 'owner-name', 'tier': 'supporter'}
        assert fake.calls[-1]['headers']['Authorization'] == 'Bearer FAKE_CIVITAI_KEY'
        calls = len(fake.calls)
        assert (await client.models(query='flux', limit=2))['signed_in'] and len(fake.calls) == calls + 1

    failures = (((401, b'{}'), 'rejected the saved API key', None), ((404, b'{}'), 'no public record', None),
                ((429, b'{}'), 'HTTP 429', 60), ((503, b'{}'), 'HTTP 503', 60), ((200, b'<html>'), 'instead of its listing', None),
                (aiohttp.ClientConnectionError(), 'could not be reached', 60), ((200, {'items': 'nope'}), 'changed its model', None))
    for answer, text, retry in failures:
        client.cache.clear()
        with patch('civitai.aiohttp.ClientSession', new=FakeCivitai({'models': answer})):
            try:
                await client.models()
            except CivitaiError as error:
                assert text in str(error) and error.retry_after == retry, (text, str(error))
            else:
                raise AssertionError(text)
    keys.clear()
    client.cache.clear()
    with patch('civitai.aiohttp.ClientSession', new=FakeCivitai({'models': (401, b'{}')})):
        try:
            await client.models()
        except CivitaiError as error:
            assert 'needs a signed-in API key' in str(error)
    with patch('civitai.aiohttp.ClientSession', new=FakeCivitai({'enums': {'ActiveBaseModel': [1]}})):
        await expect_async_error(CivitaiError, client.base_models())

    with tempfile.TemporaryDirectory(prefix='hearthling-civitai-', dir=Path(__file__).resolve().parent) as directory:
        db = open_database(Path(directory) / 'hearthling.sqlite3')
        async with Hearthling(None, db, credentials={'discord_token': 'FAKE_TOKEN'}, data_dir=directory) as bot:
            features = bot.features
            guest = request(GUEST_ID)
            guest.channel.is_nsfw = lambda: False
            assert {'civitai_models', 'civitai_model', 'civitai_images', 'civitai_base_models'} <= set(features.allowed_actions(guest))
            features.update_settings({'public_civitai': False}, {})
            assert 'civitai_models' not in features.allowed_actions(guest) and 'civitai_models' in features.allowed_actions(request(OWNER_ID))
            features.update_settings({'public_civitai': True, 'civitai_enabled': False}, {})
            assert not any(action.startswith('civitai_') for action in features.allowed_actions(request(OWNER_ID)))
            features.update_settings({'civitai_enabled': True}, {'civitai_api_key': 'FAKE_CIVITAI_SECRET'})
            saved = json.loads((Path(directory) / 'connections.json').read_text())
            assert saved['civitai_api_key'] == 'FAKE_CIVITAI_SECRET' and features.civitai.key() == 'FAKE_CIVITAI_SECRET'
            assert features.redact('oops FAKE_CIVITAI_SECRET') == 'oops [redacted]'

            routes = FakeCivitai({'models': {'items': [record(1), record(2, nsfw=True)]}, 'images': {'items': [picture(11), picture(12, 'X')]},
                                  'enums': {'ActiveBaseModel': ['Flux.3']}})
            with patch('civitai.aiohttp.ClientSession', new=routes):
                found = await features.civitai_lookup(features.validate(json.dumps(
                    {'action': 'civitai_models', 'query': 'flux', 'limit': 5, 'civitai_sort': 'newest', 'model_type': 'Checkpoint'}), guest), guest)
                assert [model['id'] for model in found['models']] == [1] and routes.calls[-1]['headers']['Authorization'] == 'Bearer FAKE_CIVITAI_SECRET'
                guest.channel.is_nsfw = lambda: True
                shown = await features.civitai_lookup(features.validate(json.dumps({'action': 'civitai_images', 'period': 'week'}), guest), guest)
                assert len(shown['images']) == 2 and routes.calls[-1]['query']['period'] == ['Week']
                names = await features.civitai_lookup(features.validate(json.dumps({'action': 'civitai_base_models'}), guest), guest)
                assert names['base_models'] == ['Flux.3']
                await expect_async_error(ValueError, features.civitai_lookup(features.validate(json.dumps(
                    {'action': 'civitai_models', 'query': 'FAKE_CIVITAI_SECRET'}), guest), guest))
            features.update_settings({'bot_mode': 'chatbot'}, {})
            assert not any(action.startswith('civitai_') for action in features.allowed_actions(guest))
        db.close()
    print('Civitai checks passed.')


if __name__ == '__main__':
    asyncio.run(main())
