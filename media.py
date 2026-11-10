"""Public social media extraction and bounded Discord attachments."""
import asyncio
import json
import re
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit, urljoin
from xml.etree import ElementTree

from web_sources import PAGE_BYTES, PageParser, fetch_resource, picture_extension, public_url


MEDIA_BYTES = 25 * 1024 * 1024
MEDIA_TOOL = {'type': 'function', 'function': {
    'name': 'post_media',
    'description': 'Upload media from a public social post or direct image/video URL to this conversation. Report the returned delivery kind accurately.',
    'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}},
                   'required': ['url'], 'additionalProperties': False}}}
SOCIAL_HOSTS = ('reddit.com', 'redd.it', 'facebook.com', 'fb.watch', 'instagram.com',
                'twitter.com', 'x.com', 'tiktok.com', 'youtube.com', 'youtu.be', 'vimeo.com')


async def run_bounded(*command, maximum=PAGE_BYTES, timeout=60):
    """Kill and reap children on timeout, cancellation or excessive output."""
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(timeout):
            output = bytearray()
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > maximum:
                    raise ValueError('The media response was too large.')
            if await process.wait():
                raise ValueError('The site did not provide a publicly downloadable video.')
            return bytes(output)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def extract_video(url):
    # Only named social-site extractors run: no generic pages, playlists, user config or cookies.
    output = await run_bounded(
        sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--skip-download',
        '--dump-single-json', '--no-warnings', '--socket-timeout', '10', '--retries', '0',
        '--extractor-retries', '0', '--use-extractors',
        'Reddit,Facebook,FacebookReel,Instagram,InstagramIOS,Twitter,TikTok,Youtube,Vimeo',
        '--', url)
    info = json.loads(output)
    if not isinstance(info, dict) or info.get('_type') in {'playlist', 'multi_video'}:
        raise ValueError('Use a link to one public post or video.')
    return info


def media_formats(info):
    """Prefer a modest MP4 with sound; otherwise pair video and audio locally."""
    formats = [item for item in info.get('formats', []) if isinstance(item, dict)
               and item.get('url') and item.get('protocol') in {None, 'http', 'https'}
               and not item.get('has_drm')]
    combined = [item for item in formats if item.get('vcodec') not in {None, 'none'}
                and item.get('acodec') not in {None, 'none'} and item.get('ext') == 'mp4']
    videos = combined or [item for item in formats if item.get('vcodec') not in {None, 'none'}
                          and item.get('ext') == 'mp4']
    if not videos:
        raise ValueError('No downloadable MP4 video was available.')
    # Prefer <=720p to fit Discord; try smaller formats if the first exceeds the limit.
    videos.sort(key=lambda item: (0 if (item.get('height') or 0) <= 720 else 1,
                                  -(item.get('height') or 0)))
    audio = [item for item in formats if item.get('vcodec') == 'none'
             and item.get('acodec') not in {None, 'none'} and item.get('ext') in {'m4a', 'mp4'}]
    audio.sort(key=lambda item: item.get('abr') or 0, reverse=True)
    if not combined and audio:
        return [(video, audio[0]) for video in videos[:3]]
    return [(video, None) for video in videos[:3]]


def video_file(body):
    return len(body) >= 12 and body[4:8] == b'ftyp'


async def download_video(info, source, maximum, check):
    last_error = None
    for video, audio in media_formats(info):
        try:
            parts = []
            for item in (video, audio):
                if not item:
                    continue
                if (item.get('filesize') or 0) > maximum:
                    raise ValueError('The video exceeds this channel\'s upload limit.')
                resource = await fetch_resource(item['url'], maximum=maximum, check=check, referer=source)
                if not video_file(resource['body']):
                    raise ValueError('The site returned an unsupported video file.')
                parts.append(resource['body'])
            if len(parts) == 1:
                return parts[0]
            with tempfile.TemporaryDirectory(prefix='discord-media-') as directory:
                root = Path(directory)
                (root / 'video.mp4').write_bytes(parts[0])
                (root / 'audio.m4a').write_bytes(parts[1])
                target = root / 'combined.mp4'
                await run_bounded('ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                  '-i', str(root / 'video.mp4'), '-protocol_whitelist', 'file,pipe',
                                  '-i', str(root / 'audio.m4a'), '-map', '0:v:0', '-map', '1:a:0',
                                  '-c', 'copy', '-movflags', '+faststart', str(target))
                if target.stat().st_size > maximum:
                    raise ValueError('The video with sound exceeds this channel\'s upload limit.')
                return target.read_bytes()
        except (ValueError, OSError, TimeoutError) as exc:
            last_error = exc
    raise ValueError(str(last_error) or 'The video could not be downloaded.')


class MediaParser(PageParser):
    def __init__(self, url):
        super().__init__(url)
        self.videos = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        value = None
        if tag == 'meta' and (attrs.get('property') or attrs.get('name')) in {
                'og:video', 'og:video:url', 'og:video:secure_url', 'twitter:player:stream'}:
            value = attrs.get('content')
        elif tag in {'video', 'source'}:
            value = attrs.get('src')
        if value and (url := self.link(value)) and url not in self.videos:
            self.videos.append(url)


async def resolve_media(url, *, maximum, check):
    source = public_url(url)
    check(source)
    maximum = min(maximum, MEDIA_BYTES)
    host = urlsplit(source).hostname.lower().rstrip('.')
    reason = 'The page did not expose a downloadable image or video.'
    if host == 'v.redd.it' and re.fullmatch(r'/[a-zA-Z0-9]+/?', urlsplit(source).path):
        try:
            manifest_url = source.split('?', 1)[0].rstrip('/') + '/DASHPlaylist.mpd'
            resource = await fetch_resource(manifest_url, maximum=PAGE_BYTES, check=check)
            root = ElementTree.fromstring(resource['body'])
            formats = []
            for representation in root.findall('.//{*}Representation'):
                base = representation.find('{*}BaseURL')
                mime = representation.get('mimeType', '')
                if base is None or not base.text or mime not in {'video/mp4', 'audio/mp4'}:
                    continue
                audio = mime == 'audio/mp4'
                formats.append({'url': urljoin(resource['url'], base.text.strip()),
                                'ext': 'm4a' if audio else 'mp4', 'protocol': 'https',
                                'vcodec': 'none' if audio else representation.get('codecs', 'h264'),
                                'acodec': representation.get('codecs', 'aac') if audio else 'none',
                                'height': int(representation.get('height', 0)),
                                'abr': int(representation.get('bandwidth', 0)) / 1000})
            body = await download_video({'formats': formats}, source, maximum, check)
            return {'body': body, 'filename': 'social-video.mp4', 'kind': 'video', 'source_url': source}
        except (ValueError, OSError, TimeoutError, ElementTree.ParseError) as exc:
            reason = str(exc)
    if any(host == domain or host.endswith('.' + domain) for domain in SOCIAL_HOSTS):
        try:
            info = await extract_video(source)
            body = await download_video(info, source, maximum, check)
            return {'body': body, 'filename': 'social-video.mp4', 'kind': 'video', 'source_url': source}
        except (ValueError, OSError, TimeoutError) as exc:
            reason = str(exc)
    try:
        resource = await fetch_resource(source, maximum=maximum, check=check)
        redirected = resource['url']
        redirected_host = urlsplit(redirected).hostname.lower().rstrip('.')
        if redirected != source and any(redirected_host == domain or redirected_host.endswith('.' + domain)
                                        for domain in SOCIAL_HOSTS):
            try:
                info = await extract_video(redirected)
                body = await download_video(info, source, maximum, check)
                return {'body': body, 'filename': 'social-video.mp4', 'kind': 'video', 'source_url': source}
            except (ValueError, OSError, TimeoutError) as exc:
                reason = str(exc)
        mime = resource['content_type']
        if mime.startswith('image/'):
            suffix = await asyncio.to_thread(picture_extension, resource['body'])
            return {'body': resource['body'], 'filename': f'social-image.{suffix}', 'kind': 'image', 'source_url': source}
        if mime in {'video/mp4', 'application/octet-stream'} and video_file(resource['body']):
            return {'body': resource['body'], 'filename': 'social-video.mp4', 'kind': 'video', 'source_url': source}
        if mime not in {'text/html', 'application/xhtml+xml', ''}:
            raise ValueError('The URL did not return a supported image, MP4 video or web page.')
        parser = MediaParser(resource['url'])
        parser.feed(resource['body'][:PAGE_BYTES].decode('utf-8', errors='replace'))
        for video in parser.videos[:3]:
            try:
                candidate = await fetch_resource(video, maximum=maximum, check=check, referer=source)
                if video_file(candidate['body']):
                    return {'body': candidate['body'], 'filename': 'social-video.mp4', 'kind': 'video', 'source_url': source}
            except ValueError as exc:
                reason = str(exc)
        # A video thumbnail is a preview, never a successful video delivery.
        for image in parser.images[:3]:
            try:
                candidate = await fetch_resource(image['image_url'], maximum=maximum, check=check, referer=source)
                suffix = await asyncio.to_thread(picture_extension, candidate['body'])
                return {'body': candidate['body'], 'filename': f'social-image.{suffix}',
                        'kind': 'preview' if parser.videos else 'image', 'source_url': source,
                        'note': 'Image only; no playable video was uploaded.'}
            except ValueError:
                continue
    except ValueError as exc:
        reason = str(exc)
    return {'kind': 'link', 'source_url': source,
            'note': f'Media could not be uploaded: {reason} Only a source link was posted; Discord may show a preview.'}
