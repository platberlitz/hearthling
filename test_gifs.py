"""Offline GIF encoding, clip conversion, tool delivery and paid-request replay checks."""
import testenv
import asyncio
import base64
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

from hearthling import Hearthling, open_database
from features import FeatureAction
from test_bots import CREDENTIALS, fake_ai
from test_features import GUEST_ID, http_session, request
from test_hearthling import ai_reply
from gifs import convert_gif, native_gif, native_options, picture_gif, sheet_gif, source_url
from images import ImageSpec, extension, image_request
from media import run_bounded


def sample(sheet=False):
    image = Image.new('RGB', (256, 256), 'white')
    draw = ImageDraw.Draw(image)
    if sheet:
        for index in range(16):
            x, y = index % 4 * 64, index // 4 * 64
            draw.rectangle((x + index * 2, y + 16, x + index * 2 + 20, y + 40), fill=(index * 15, 30, 180))
    else:
        draw.rectangle((25, 50, 100, 190), fill='navy')
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def inspect_gif(raw, *, frames=None, width=512):
    with Image.open(io.BytesIO(raw)) as image:
        assert image.format == 'GIF' and image.n_frames >= 2
        assert image.info['loop'] == 0 and max(image.size) <= width
        if frames:
            assert image.n_frames == frames
        first = image.convert('RGB').tobytes()
        changed = False
        for index in range(1, image.n_frames):
            image.seek(index)
            changed |= first != image.convert('RGB').tobytes()
        assert changed


async def main():
    sheet, still = sample(True), sample()
    generated = sheet_gif(sheet)
    inspect_gif(generated, frames=16)
    assert native_gif(generated, maximum=len(generated)) == generated
    assert extension(generated) == 'gif'
    for raw, maximum in ((still, 1000000), (generated, 50), (generated[:30], 1000000)):
        try:
            native_gif(raw, maximum=maximum)
        except (ValueError, OSError, EOFError):
            pass
        else:
            raise AssertionError('Invalid native animation accepted')
    native_spec = ImageSpec(id='native', name='Native', base_url='https://example.org/v1',
                            model='gpt-image-2.6-sunburst', family='responses', local_keyless=True)
    opts = native_options(native_spec, references=[still], seconds=8, fps=15)
    assert opts['frame_count'] == 16 and opts['frame_duration_ms'] == 500
    payload = image_request(native_spec, 'A ball bouncing', references=[still], animation=opts)
    assert payload['tools'][0]['output_format'] == 'gif' and payload['tools'][0]['action'] == 'generate'
    assert payload['input'][0]['content'][1]['image_url'].startswith('data:image/png;base64,')
    direct_spec = native_spec.model_copy(update={'family': 'openai_images'})
    assert native_options(direct_spec, references=[still], seconds=2, fps=10) is None
    direct_opts = native_options(direct_spec, references=[], seconds=8, fps=15)
    assert direct_opts['frame_count'] == 64
    assert image_request(direct_spec, 'A ball bouncing', animation=direct_opts)['output_format'] == 'gif'
    # Explicit still sizes must not force oversized native animation sheets.
    for spec in (native_spec, direct_spec):
        for size in (None, '1024x1024', '1536x1024', '1024x1536'):
            for seconds, fps in ((1, 4), (3, 3), (2, 10)):
                opts = native_options(spec, references=[], seconds=seconds, fps=fps, size=size)
                payload = image_request(spec, 'A ball bouncing', size=size, animation=opts)
                options = payload['tools'][0] if spec.family == 'responses' else payload
                assert options['size'] == 'auto', options
                assert options['frame_count'] == min(16 if spec.family == 'responses' else 64, seconds * fps)
            still_payload = image_request(spec, 'A ball', size=size)
            still_options = still_payload['tools'][0] if spec.family == 'responses' else still_payload
            assert still_options['size'] == (size or spec.size)
    for effect in ('zoom', 'shake', 'spin'):
        inspect_gif(picture_gif(still, effect=effect, seconds=2, fps=10, width=256, maximum=1000000))
    try:
        sheet_gif(sheet, maximum=50)
    except ValueError:
        pass
    else:
        raise AssertionError('Oversized GIF accepted')
    for raw in (b'not an image', sample()[:20]):
        try:
            sheet_gif(raw)
        except ValueError:
            pass
        else:
            raise AssertionError('Broken image accepted')
    check = lambda url: None
    source = 'https://example.org/source.png'
    with patch('gifs.resolve_media', new=AsyncMock(return_value={'kind': 'image', 'body': still})):
        inspect_gif(await convert_gif(source, check=check))
    for kind in ('link', 'preview'):
        with patch('gifs.resolve_media', new=AsyncMock(return_value={'kind': kind, 'body': still})):
            try:
                await convert_gif(source, check=check)
            except ValueError:
                pass
            else:
                raise AssertionError('A fallback was presented as a GIF')
    if shutil.which('ffmpeg'):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'clip.mp4'
            await run_bounded('ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                              'testsrc2=size=128x96:rate=10', '-t', '3', '-c:v', 'mpeg4', str(path))
            with patch('gifs.resolve_media', new=AsyncMock(return_value={'kind': 'video', 'body': path.read_bytes()})):
                clip = await convert_gif(source, check=check, start=1, seconds=1, width=128)
                inspect_gif(clip, frames=10, width=128)
        with patch('gifs.resolve_media', new=AsyncMock(return_value={'kind': 'image', 'body': generated})):
            inspect_gif(await convert_gif(source, check=check, seconds=1, width=128))
    else:
        print('SKIP: ffmpeg clip conversion (ffmpeg is not installed).')
    req = request(GUEST_ID)
    req.attachments = [NS(content_type='video/mp4', url=source)]
    args = FeatureAction(action='make_gif')
    assert await source_url(args, req) == source
    req.attachments = []
    with patch('gifs.resolved_reply', new=AsyncMock(return_value=NS(attachments=[NS(content_type='image/png', url=source)]))):
        assert await source_url(args, req) == source
    with tempfile.TemporaryDirectory() as directory:
        async with Hearthling(fake_ai(), open_database(':memory:'), credentials=CREDENTIALS, data_dir=directory) as bot:
            req = request(GUEST_ID)
            req.channel.send = AsyncMock(return_value=NS(id=991))
            arguments = json.dumps({'action': 'make_gif', 'url': source, 'gif_effect': 'shake'})
            with patch('features.convert_gif', new=AsyncMock(return_value=generated)) as convert:
                result = await bot.execute(arguments, req, tool='feature_tool')
                repeated = await bot.execute(arguments, req, tool='feature_tool')
                assert result['ok'] and result['result']['kind'] == 'gif' and result == repeated
                convert.assert_awaited_once()
                req.channel.send.assert_awaited_once()
                assert req.channel.send.call_args.kwargs['file'].filename.endswith('.gif')
            other = request(GUEST_ID, req.id + 1)
            arguments = json.dumps({'action': 'generate_gif', 'prompt': 'A ball bouncing.'})
            session = http_session({'output': [{'type': 'image_generation_call', 'result': base64.b64encode(sheet).decode()}]})
            with patch('features.aiohttp.ClientSession', return_value=session):
                result = await bot.execute(arguments, other, tool='feature_tool')
                assert result['ok'], result
                filename = result['result']['filename']
                inspect_gif((Path(directory) / 'pictures' / filename).read_bytes(), frames=16)
                assert filename.endswith('.gif') and '4 columns and 4 rows' in bot.features.picture_prompt(other.id)
                assert (await bot.execute(arguments, other, tool='feature_tool')) == result
                session.post.assert_called_once()
                other.channel.send = AsyncMock(return_value=NS(id=992))
                await bot.deliver(other, 'Here is your GIF.', [])
                assert any(call.kwargs.get('file') and call.kwargs['file'].filename == filename
                           for call in other.channel.send.call_args_list)
            # Both native API families preserve the complete GIF, including timing.
            bot.features.settings.image_providers.append(native_spec)
            previous_provider = bot.features.settings.image_provider
            bot.features.settings.image_provider = native_spec.id
            for index, family in enumerate(('responses', 'openai_images')):
                bot.features.settings.image_providers[-1] = native_spec.model_copy(update={'family': family})
                native_req = request(GUEST_ID, req.id + 20 + index)
                native_args = json.dumps({'action': 'generate_gif', 'prompt': 'A ball bouncing.', 'size': '1024x1024'})
                body = ({'output': [{'type': 'image_generation_call', 'result': base64.b64encode(generated).decode()}]}
                        if family == 'responses' else {'data': [{'b64_json': base64.b64encode(generated).decode()}]})
                session = http_session(body)
                with patch('features.aiohttp.ClientSession', return_value=session):
                    result = await bot.execute(native_args, native_req, tool='feature_tool')
                    assert result['ok'], result
                    assert result['result']['animation'] == 'native GIF'
                    assert (Path(directory) / 'pictures' / result['result']['filename']).read_bytes() == generated
                    assert 'sprite sheet' not in bot.features.picture_prompt(native_req.id)
                    assert (await bot.execute(native_args, native_req, tool='feature_tool')) == result
                    session.post.assert_called_once()
                    payload = session.post.call_args.kwargs['json']
                    options = payload['tools'][0] if family == 'responses' else payload
                    assert options['output_format'] == 'gif'
                    assert options['size'] == 'auto'
                    assert session.post.call_args.args[0].endswith('/responses' if family == 'responses' else '/images/generations')
            # A still returned from a native request is terminal, never cut into fake frames or retried.
            native_req = request(GUEST_ID, req.id + 22)
            session = http_session({'data': [{'b64_json': base64.b64encode(still).decode()}]})
            with patch('features.aiohttp.ClientSession', return_value=session), patch('features.sheet_gif') as assemble:
                result = await bot.execute(native_args, native_req, tool='feature_tool')
                assert not result['ok'] and result['outcome_may_be_uncertain']
                await bot.execute(native_args, native_req, tool='feature_tool')
                session.post.assert_called_once()
                assemble.assert_not_called()
            # A bad sheet is terminal: it cannot trigger a second paid request.
            bot.features.settings.image_provider = previous_provider
            bad = request(GUEST_ID, req.id + 2)
            session = http_session({'output': [{'type': 'image_generation_call', 'result': base64.b64encode(still).decode()}]})
            with patch('features.aiohttp.ClientSession', return_value=session), patch('features.sheet_gif', side_effect=ValueError('bad sheet')):
                result = await bot.execute(arguments, bad, tool='feature_tool')
                assert not result['ok'] and result['outcome_may_be_uncertain']
                await bot.execute(arguments, bad, tool='feature_tool')
                session.post.assert_called_once()
            # Permission is checked again after conversion, before posting.
            async def disabled(*args, **kwargs):
                bot.features.settings.public_images = False
                return generated
            other = request(GUEST_ID, req.id + 3)
            other.channel.send = AsyncMock()
            with patch('features.convert_gif', new=AsyncMock(side_effect=disabled)):
                result = await bot.execute(json.dumps({'action': 'make_gif', 'url': source}), other, tool='feature_tool')
                assert not result['ok']
                other.channel.send.assert_not_awaited()
            bot.features.settings.public_images = True
            # Direct action aliases reach the tool and report confirmed upload.
            other = request(GUEST_ID, req.id + 4)
            other.channel.send = AsyncMock(return_value=NS(id=994))
            response = ai_reply(calls=1)
            response.choices[0].message.tool_calls[0].function.name = 'make_gif'
            response.choices[0].message.tool_calls[0].function.arguments = json.dumps({'url': source})
            bot.ai.chat.completions.create = AsyncMock(side_effect=[response, ai_reply(content='GIF uploaded.')])
            with patch('features.convert_gif', new=AsyncMock(return_value=generated)):
                await bot.converse(other)
                other.channel.send.assert_awaited_once()
            for action in ('make_gif', 'generate_gif'):
                for field, value in [('gif_seconds', 50), ('gif_fps', 100), ('gif_width', 4000), ('gif_start', -1)]:
                    result = await bot.execute(json.dumps({'action': action, field: value}), other, tool='feature_tool')
                    assert not result['ok']
            bot.features.settings.bot_mode = 'chatbot'
            assert not {'make_gif', 'generate_gif'} & set(bot.features.allowed_actions(req))
    print('PASS: GIF frames, motion effects, real video conversion, delivery, replay, failure handling and permissions.')


if __name__ == '__main__':
    asyncio.run(main())
