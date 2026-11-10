"""Image key failover across custom models, with no paid retry on uncertain errors."""
import testenv  # noqa: F401
import asyncio
import base64
import tempfile
from unittest.mock import patch

from hearthling import Hearthling as Bot, open_database
from features import FeatureAction, FeatureError
from images import ImageSpec, key_alternatives
from test_bots import CREDENTIALS, fake_ai
from test_features import PNG, http_session, request


async def main():
    with tempfile.TemporaryDirectory() as directory:
        db = open_database(':memory:')
        credentials = CREDENTIALS | {'image_api_key': 'PRIMARY', 'image_fallback_keys': ['DEAD', 'LIVE', 'LAST']}
        async with Bot(fake_ai(), db, credentials=credentials, data_dir=directory) as bot:
            feature = bot.features
            feature.credentials['image_key:custom-image'] = 'PRIMARY'
            provider = {'id': 'custom-image', 'name': 'Custom image', 'family': 'responses',
                        'base_url': 'https://smolproxy.org/v1', 'model': 'gpt-image-2.6-sunburst'}
            feature.update_settings({'image_providers': [provider], 'image_provider': provider['id'],
                                     'image_base_url': 'https://smolproxy.org/v1',
                                     'image_fallback_providers': []}, {})
            success = {'output': [{'type': 'image_generation_call', 'result': base64.b64encode(PNG).decode()}]}
            args = FeatureAction(action='generate_image', prompt='A cup of tea')
            client = http_session(success)
            client.post.side_effect = [http_session({'error': 'Daily quota exceeded'}, 429).post.return_value,
                                       http_session({'error': 'Unknown key'}, 404).post.return_value,
                                       http_session(success).post.return_value]
            with patch('features.aiohttp.ClientSession', return_value=client):
                result = await feature.generate_image(args, request())
            assert result['bytes'] == len(PNG)
            assert [call.kwargs['headers']['Authorization'] for call in client.post.call_args_list] == [
                'Bearer PRIMARY', 'Bearer DEAD', 'Bearer LIVE']

            # The configured Gemini key can already be a shared fallback: try each distinct key once.
            provider.update(family='gemini', model='gemini-3-pro-image', base_url='https://api.smolproxy.org/google')
            feature.credentials['image_key:custom-image'] = 'LIVE'
            feature.update_settings({'image_providers': [provider]}, {})
            gemini = {'candidates': [{'content': {'parts': [{'inlineData': {
                'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode()}}]}}]}
            client = http_session(gemini)
            client.post.side_effect = [http_session({'error': 'Rate limit'}, 429).post.return_value,
                                       http_session({'error': 'Bad key'}, 401).post.return_value,
                                       http_session({'error': 'Disabled key'}, 403).post.return_value,
                                       http_session(gemini).post.return_value]
            with patch('features.aiohttp.ClientSession', return_value=client):
                await feature.generate_image(args, request(identity=900000000000009101))
            assert [call.kwargs['headers']['x-goog-api-key'] for call in client.post.call_args_list] == [
                'LIVE', 'PRIMARY', 'DEAD', 'LAST']

            for host in ('example.invalid', 'smolproxy.org.example.invalid', 'not-smolproxy.org'):
                spec = ImageSpec(**(provider | {'base_url': 'https://' + host + '/v1'}))
                assert key_alternatives(feature, spec, 'LOCAL') == ['LOCAL']
            feature.update_settings({'image_base_url': 'https://example.invalid/v1'}, {})
            assert key_alternatives(feature, ImageSpec(**provider), 'LOCAL') == ['LOCAL']
            feature.update_settings({'image_base_url': 'https://smolproxy.org/v1'}, {})

            # A bad final key must not erase an earlier temporary limit and strand the request.
            client = http_session({})
            client.post.side_effect = [http_session({'error': 'Rate limit'}, 429).post.return_value] + [
                http_session({'error': 'Bad key'}, 401).post.return_value for _ in range(3)]
            with patch('features.aiohttp.ClientSession', return_value=client):
                try:
                    await feature.generate_image(args, request(identity=900000000000009102))
                    raise AssertionError('Expected rate limit')
                except FeatureError as exc:
                    assert exc.retry_after == 60 and not exc.uncertain

            # Timeouts and unknown server errors must not spend another key.
            for index, failure in enumerate((asyncio.TimeoutError(), None)):
                client = http_session({'error': 'Server error'}, 500)
                if failure:
                    client.post.side_effect = failure
                with patch('features.aiohttp.ClientSession', return_value=client):
                    try:
                        await feature.generate_image(args, request(identity=900000000000009103 + index))
                        raise AssertionError('Expected an uncertain failure')
                    except FeatureError as exc:
                        assert exc.uncertain and not exc.retry_after
                assert client.post.call_count == 1
        db.close()
    print('PASS: custom Responses/Gemini key rotation, dead-key bypass, host isolation, retryable limits and uncertain outcomes.')


if __name__ == '__main__':
    asyncio.run(main())
