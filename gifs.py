"""Short animated GIFs from generated frame sheets and existing media."""
import asyncio
import io
import math
from pathlib import Path
import re
import tempfile

from PIL import Image, ImageOps, UnidentifiedImageError

from media import MEDIA_BYTES, resolve_media, run_bounded, video_file
from conversation import message_images, resolved_reply


def native_options(spec, *, references, seconds, fps, size=None):
    """Use the documented GIF controls for each API; Images edits are still-only."""
    if (references and spec.family == 'openai_images') or spec.family not in {'responses', 'openai_images'} or not re.fullmatch(
            r'gpt-image-2\.6-(?:sunburst|flare)(?:-\d{4}-\d{2}-\d{2})?', spec.model):
        return None
    frames = min(16 if spec.family == 'responses' else 64, max(2, round(seconds * fps)))
    duration = max(20, min(2000, round(seconds * 100 / frames) * 10))
    # Still-image dimensions apply to each frame and can exceed the animation's
    # total pixel budget. Let the provider fit the whole animation, even when
    # the caller explicitly supplies a still-image size.
    return dict(output_format='gif', frame_count=frames, frame_duration_ms=duration, size='auto')


def native_gif(raw, *, maximum):
    """Validate the entire animation and preserve the provider's bytes and timing."""
    if len(raw) > maximum:
        raise ValueError('The generated GIF exceeds this channel\'s upload limit.')
    with open_picture(raw) as image:
        if image.format != 'GIF' or not 2 <= getattr(image, 'n_frames', 1) <= 64:
            raise ValueError('The provider did not return an animated GIF with 2 to 64 frames.')
        for index in range(image.n_frames):
            image.seek(index)
            image.load()
    return raw


def frame_prompt(prompt):
    return (f'{prompt}\n\nCreate one animation sprite sheet: exactly 4 columns and 4 rows of equally sized '
            'frames, filling the whole canvas edge to edge. The 16 frames show consecutive moments of the '
            'requested action, read left to right and then top to bottom. Keep the camera, subject scale, '
            'identity and background consistent. Use gradual movement and a final pose that flows into '
            'the first for a seamless loop. Each cell contains only its scene, with clean adjoining edges '
            'and all artwork contained inside its own cell.')


def open_picture(raw):
    try:
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > 20_000_000:
            raise ValueError('The source image has too many pixels for GIF creation.')
        return image
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        raise ValueError('The source is not a readable image.') from None


def encode_frames(frames, seconds, maximum):
    """Shrink locally when needed; never request another paid image."""
    delay = max(20, round(seconds * 100 / len(frames)) * 10)
    for width in (None, 320, 240, 160):
        resized = [frame.copy() for frame in frames]
        if width:
            for frame in resized:
                frame.thumbnail((width, width), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        resized[0].save(output, format='GIF', save_all=True, append_images=resized[1:],
                        duration=delay, loop=0, disposal=2, optimize=False)
        raw = output.getvalue()
        if len(raw) <= maximum:
            with Image.open(io.BytesIO(raw)) as result:
                if result.n_frames < 2:
                    raise ValueError('The frames contain no visible movement; no animated GIF was created.')
            return raw
    raise ValueError('The GIF could not fit this channel\'s upload limit.')


def sheet_gif(raw, *, seconds=2, width=480, maximum=10 * 1024 * 1024):
    with open_picture(raw) as sheet:
        sheet = ImageOps.exif_transpose(sheet).convert('RGB')
        if min(sheet.size) < 64:
            raise ValueError('The image provider returned a frame sheet too small to animate.')
        frames = []
        for row in range(4):
            for column in range(4):
                frame = sheet.crop((column * sheet.width // 4, row * sheet.height // 4,
                                    (column + 1) * sheet.width // 4, (row + 1) * sheet.height // 4))
                frame.thumbnail((width, width), Image.Resampling.LANCZOS)
                frames.append(frame)
        return encode_frames(frames, seconds, maximum)


def picture_gif(raw, *, effect, seconds, fps, width, maximum):
    with open_picture(raw) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((width, width), Image.Resampling.LANCZOS)
        frames = []
        for index in range(max(2, round(seconds * fps))):
            phase = index / max(2, round(seconds * fps)) * 2 * math.pi
            if effect == 'spin':
                frame = image.rotate(-math.degrees(phase), Image.Resampling.BICUBIC, fillcolor='white')
            elif effect == 'shake':
                frame = image.transform(image.size, Image.Transform.AFFINE,
                                        (1, 0, round(math.sin(phase * 3) * image.width * .035),
                                         0, 1, round(math.cos(phase * 2) * image.height * .035)),
                                        Image.Resampling.BICUBIC, fillcolor='white')
            else:
                scale = 1 + .12 * (1 - math.cos(phase)) / 2
                frame = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
                left, top = (frame.width - image.width) // 2, (frame.height - image.height) // 2
                frame = frame.crop((left, top, left + image.width, top + image.height))
            frames.append(frame)
        return encode_frames(frames, seconds, maximum)


async def source_url(args, request):
    if args.url or args.image_url:
        return args.url or args.image_url
    replied = await resolved_reply(request)
    for message in (request, replied):
        if message is None:
            continue
        for item in getattr(message, 'attachments', []) or []:
            if (getattr(item, 'content_type', '') or '').startswith(('image/', 'video/')):
                return item.url
        pictures = message_images(message, 'GIF source')
        if pictures:
            return pictures[0]['url']
        links = re.findall(r'https://[^\s<>\[\]()"\']+', message.content or '')
        if links:
            return links[0].rstrip('.,!?')
    raise ValueError('Attach or link a picture or video, or reply to one, to make a GIF.')


async def convert_gif(source, *, check, effect='auto', seconds=2, start=0, fps=10,
                      width=480, maximum=10 * 1024 * 1024):
    maximum = min(maximum, MEDIA_BYTES)
    async with asyncio.timeout(150):
        media = await resolve_media(source, maximum=MEDIA_BYTES, check=check)
        if media['kind'] not in {'image', 'video'} or not media.get('body'):
            raise ValueError('The source media could not be downloaded; a link or thumbnail cannot be converted into a GIF.')
        raw = media['body']
        is_video = video_file(raw)
        animated = False
        if not is_video:
            def inspect():
                with open_picture(raw) as image:
                    return getattr(image, 'is_animated', False)
            animated = await asyncio.to_thread(inspect)
        if not is_video and not animated:
            if start:
                raise ValueError('A still picture has no clip start time. Use gif_start=0.')
            return await asyncio.to_thread(picture_gif, raw, effect=effect, seconds=seconds,
                                           fps=fps, width=width, maximum=maximum)
        if effect != 'auto':
            raise ValueError('Use gif_effect=auto to convert a video or an already animated image.')
        with tempfile.TemporaryDirectory(prefix='discord-gif-') as directory:
            path = Path(directory) / ('source.mp4' if is_video else 'source.image')
            path.write_bytes(raw)
            for output_width in dict.fromkeys((width, min(width, 320), min(width, 240), min(width, 160))):
                filters = (f'fps={fps},scale={output_width}:{output_width}:force_original_aspect_ratio=decrease:flags=lanczos,'
                           'split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer')
                try:
                    result = await run_bounded('ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
                        '-protocol_whitelist', 'file,pipe', '-ss', str(start), '-i', str(path),
                        '-t', str(seconds), '-an', '-filter_complex_threads', '1', '-filter_complex', filters,
                        '-loop', '0', '-f', 'gif', 'pipe:1', maximum=maximum, timeout=35)
                except FileNotFoundError:
                    raise ValueError('Install ffmpeg on the host to convert videos and animated images into GIFs.') from None
                except ValueError:
                    continue
                with open_picture(result) as image:
                    if getattr(image, 'n_frames', 1) < 2:
                        raise ValueError('Choose a clip with at least two frames and visible movement.')
                return result
    raise ValueError('The clip could not be converted within this channel\'s upload limit. Try a shorter clip or an earlier start time.')
