"""Media selection, upload receipts, fallback honesty and access checks."""
import testenv
import asyncio
import json
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from hearthling import Hearthling, open_database
from test_bots import CREDENTIALS, fake_ai
from test_hearthling import ai_reply
from test_features import GUEST_ID, request
from test_web_sources import image_bytes, resource
import media


MP4 = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 32


def video(url='https://cdn.example.org/video.mp4', **extra):
    return {'url': url, 'ext': 'mp4', 'protocol': 'https', 'vcodec': 'h264',
            'acodec': 'aac', 'height': 720, **extra}


async def main():
    check = lambda url: None
    manifest = b'<MPD><Period><Representation mimeType="video/mp4" height="360"><BaseURL>CMAF_360.mp4</BaseURL></Representation><Representation mimeType="audio/mp4"><BaseURL>CMAF_AUDIO_64.mp4</BaseURL></Representation></Period></MPD>'
    with patch('media.fetch_resource', new=AsyncMock(return_value=resource(manifest, url='https://v.redd.it/example/DASHPlaylist.mpd'))), patch(
            'media.download_video', new=AsyncMock(return_value=MP4)) as download:
        result = await media.resolve_media('https://v.redd.it/example', maximum=1000, check=check)
        assert result['kind'] == 'video'
        formats = download.call_args.args[0]['formats']
        assert formats[0]['url'] == 'https://v.redd.it/example/CMAF_360.mp4'
        assert formats[1]['vcodec'] == 'none' and formats[1]['ext'] == 'm4a'
    info = {'formats': [video(height=1080), video(height=360), video(),
                        video(protocol='m3u8_native', height=480)]}
    assert [v['height'] for v, _ in media.media_formats(info)] == [720, 360, 1080]
    separate = {'formats': [video(acodec='none'), video(vcodec='none', ext='m4a')]}
    assert media.media_formats(separate)[0][1]['ext'] == 'm4a'
    with patch('media.extract_video', new=AsyncMock(return_value=info)), patch(
            'media.fetch_resource', new=AsyncMock(return_value=resource(MP4, 'video/mp4'))) as fetch:
        result = await media.resolve_media('https://www.facebook.com/reel/123', maximum=1000, check=check)
        assert result['kind'] == 'video' and result['body'] == MP4
        assert fetch.call_args.kwargs['maximum'] == 1000
    # A failed large format tries a smaller one instead of silently truncating.
    with patch('media.fetch_resource', new=AsyncMock(side_effect=[ValueError('too large'), resource(MP4)])):
        assert await media.download_video(info, 'https://reddit.com/comments/abc', 1000, check) == MP4
    # Direct images preserve the actual bytes, including their format.
    image = image_bytes()
    with patch('media.fetch_resource', new=AsyncMock(return_value=resource(image, 'image/png'))):
        result = await media.resolve_media('https://example.org/photo', maximum=1000, check=check)
        assert result['body'] == image and result['filename'].endswith('.png')
    # An HTML player cannot be uploaded as a video. Its picture is explicitly a preview.
    page = b'<meta property="og:video" content="/player"><meta property="og:image" content="/pic">'
    with patch('media.fetch_resource', new=AsyncMock(side_effect=[resource(page), resource(b'<html>player</html>'),
                                                               resource(image, 'image/png')])):
        result = await media.resolve_media('https://example.org/post', maximum=1000, check=check)
        assert result['kind'] == 'preview' and 'no playable video' in result['note']
    with patch('media.extract_video', new=AsyncMock(side_effect=ValueError('login required'))), patch(
            'media.fetch_resource', new=AsyncMock(side_effect=ValueError('HTTP 403'))):
        result = await media.resolve_media('https://facebook.com/reel/123', maximum=1000, check=check)
        assert result['kind'] == 'link' and 'body' not in result
    final_url = 'https://www.facebook.com/reel/123'
    with patch('media.extract_video', new=AsyncMock(side_effect=[ValueError('short link'), info])) as extract, patch(
            'media.fetch_resource', new=AsyncMock(side_effect=[resource(url=final_url), resource(MP4, 'video/mp4')])):
        result = await media.resolve_media('https://fb.watch/abc', maximum=1000, check=check)
        assert result['kind'] == 'video' and result['source_url'] == 'https://fb.watch/abc'
        assert extract.call_args.args == (final_url,)
    # Lookalike domains never enter the social extractor.
    with patch('media.extract_video', new=AsyncMock()) as extract, patch(
            'media.fetch_resource', new=AsyncMock(side_effect=ValueError('blocked'))):
        await media.resolve_media('https://facebook.com.example.org/post', maximum=1000, check=check)
        extract.assert_not_awaited()
    for url in ('http://127.0.0.1/video', 'file:///etc/passwd'):
        try:
            await media.resolve_media(url, maximum=1000, check=check)
        except ValueError:
            pass
        else:
            raise AssertionError('Private URL accepted')
    with tempfile.TemporaryDirectory() as directory:
        bot = Hearthling(fake_ai(), open_database(':memory:'), credentials=CREDENTIALS, data_dir=directory)
        async with bot:
            guest = request(GUEST_ID)
            guest.channel.send = AsyncMock(return_value=NS(id=991))
            args = json.dumps({'action': 'post_media', 'url': 'https://facebook.com/reel/123'})
            attachment = {'body': MP4, 'filename': 'social-video.mp4', 'kind': 'video',
                          'source_url': 'https://facebook.com/reel/123'}
            with patch('features.resolve_media', new=AsyncMock(return_value=attachment.copy())):
                result = await bot.execute(args, guest, tool='feature_tool')
                repeated = await bot.execute(args, guest, tool='feature_tool')
                assert result['ok'] and result['result']['media_uploaded'] and result == repeated
                guest.channel.send.assert_awaited_once()
                assert guest.channel.send.call_args.kwargs['file'].filename == 'social-video.mp4'
                assert not guest.channel.send.call_args.kwargs['allowed_mentions'].everyone
            other = request(GUEST_ID, guest.id + 1)
            other.channel.send = AsyncMock(return_value=NS(id=992))
            with patch('features.resolve_media', new=AsyncMock(return_value={'kind': 'link', 'source_url': 'https://facebook.com/reel/123'})):
                result = await bot.execute(args, other, tool='feature_tool')
                assert result['ok'] and not result['result']['media_uploaded']
                assert 'file' not in other.channel.send.call_args.kwargs
                assert '\nhttps://' in other.channel.send.call_args.args[0]
            async def disabled(*args, **kwargs):
                bot.features.settings.public_images = False
                return attachment.copy()
            other = request(GUEST_ID, guest.id + 2)
            other.channel.send = AsyncMock()
            with patch('features.resolve_media', new=AsyncMock(side_effect=disabled)):
                result = await bot.execute(args, other, tool='feature_tool')
                assert not result['ok']
                other.channel.send.assert_not_awaited()
            bot.features.settings.public_images = True
            bot.features.settings.recent_context = 0
            other = request(GUEST_ID, guest.id + 3)
            other.content = 'Hearthling, embed this public video.'
            other.channel.send = AsyncMock(return_value=NS(id=993))
            response = ai_reply(calls=1)
            response.choices[0].message.tool_calls[0].function.name = 'post_media'
            response.choices[0].message.tool_calls[0].function.arguments = '{"url":"https://example.org/video.mp4"}'
            bot.ai.chat.completions.create = AsyncMock(side_effect=[response, ai_reply(content='Video uploaded.')])
            with patch('features.resolve_media', new=AsyncMock(return_value=attachment.copy())):
                await bot.converse(other)
                other.channel.send.assert_awaited_once()
                offered = bot.ai.chat.completions.create.call_args_list[0].kwargs['tools']
                assert any(tool['function']['name'] == 'post_media' for tool in offered)
            bot.features.settings.bot_mode = 'chatbot'
            assert 'post_media' not in bot.features.allowed_actions(other)
    print('PASS: media formats, source validation, bounded downloads, preview fallbacks, delivery, replay and permissions.')


if __name__ == '__main__':
    asyncio.run(main())
