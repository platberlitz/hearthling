"""Read public web pages and download original pictures for requested Discord posts."""
import asyncio
from datetime import datetime, timezone
from html.parser import HTMLParser
import io
import ipaddress
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import warnings
import xml.etree.ElementTree as ElementTree

import aiohttp
from PIL import Image, UnidentifiedImageError

from images import PublicResolver


PAGE_BYTES = 4 * 1024 * 1024
IMAGE_BYTES = 25 * 1024 * 1024
READER_URL = 'https://r.jina.ai/'
BLOCKED_STATUSES = {401, 403, 429, 451}
REDDIT_HOSTS = {'reddit.com', 'www.reddit.com', 'old.reddit.com', 'new.reddit.com', 'np.reddit.com',
                'm.reddit.com', 'sh.reddit.com', 'redd.it'}
ATOM = '{http://www.w3.org/2005/Atom}'
MEDIA = '{http://search.yahoo.com/mrss/}'
CACHE_SECONDS = 300
CACHE_SIZE = 50
RETRY_SECONDS = 35
_cache = {}


class FetchError(ValueError):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def public_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or re.search(r'[\s\\<>\x00-\x1f]', value):
        raise ValueError('Use a complete public HTTP or HTTPS URL.')
    address = urlsplit(value)
    if (address.scheme not in {'http', 'https'} or not address.hostname or address.username is not None
            or address.password is not None or address.port not in {None, 80, 443}):
        raise ValueError('Use a public HTTP or HTTPS URL without sign-in credentials or a custom port.')
    host = address.hostname.rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('Only public web addresses can be fetched.')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # PublicResolver checks the addresses actually used by the connection.
    else:
        if not ip.is_global or (getattr(ip, 'ipv4_mapped', None) and not ip.ipv4_mapped.is_global):
            raise ValueError('Only public web addresses can be fetched.')
    return urlunsplit(address._replace(fragment=''))


def retry_after(headers):
    for name in ('Retry-After', 'X-Ratelimit-Reset'):
        try:
            return max(0.0, float(headers.get(name)))
        except (TypeError, ValueError):
            continue
    return None


async def fetch_resource(url, *, maximum=PAGE_BYTES, check=None, referer=None):
    """Follow bounded redirects, validating each destination without sharing site cookies."""
    headers = {'User-Agent': 'Hearthling/1.0 (public web reader)', 'Accept': '*/*'}
    if referer:
        referer = public_url(referer)
        if check:
            check(referer)
        headers['Referer'] = referer
    resolver = PublicResolver()
    try:
        async with asyncio.timeout(45), aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=resolver), cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=30), headers=headers) as session:
            for _ in range(6):
                url = public_url(url)
                if check:
                    check(url)
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get('Location')
                        if not location:
                            raise ValueError('The site returned a redirect without a destination.')
                        url = urljoin(url, location)
                        continue
                    if response.status != 200:
                        raise FetchError(f'The site returned HTTP {response.status}; its content could not be read.',
                                         response.status, retry_after(response.headers))
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > maximum:
                            raise ValueError(f'The download exceeds the {maximum // (1024 * 1024)} MB size limit.')
                    return {'url': str(response.url), 'body': bytes(body),
                            'content_type': response.headers.get('Content-Type', '').split(';')[0].lower(),
                            'charset': response.charset or 'utf-8'}
            raise ValueError('The site redirected too many times.')
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
        raise ValueError(f'The public site could not be reached ({type(exc).__name__}).') from None
    finally:
        await resolver.close()


class PageParser(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'template', 'svg', 'nav', 'footer', 'form'}
    BLOCKS = {'p', 'div', 'article', 'main', 'section', 'li', 'ul', 'ol', 'br', 'hr', 'tr',
              'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'blockquote'}

    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.parts, self.title, self.links, self.images = [], [], [], []
        self.skipped = []
        self.in_title = False
        self.anchor = None
        self.description = ''

    def link(self, value):
        try:
            return public_url(urljoin(self.url, value)) if value else None
        except ValueError:
            return None

    def image(self, value, alt=''):
        url = self.link(value)
        if url and len(self.images) < 100 and not any(item['image_url'] == url for item in self.images):
            self.images.append({'image_url': url, 'alt': alt[:300], 'source_url': self.url})

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.SKIP:
            self.skipped.append(tag)
        if self.skipped:
            return
        if tag == 'title':
            self.in_title = True
        elif tag == 'meta':
            name = (attrs.get('property') or attrs.get('name') or '').lower()
            content = attrs.get('content') or ''
            if name in {'og:image', 'og:image:url', 'og:image:secure_url', 'twitter:image', 'twitter:image:src'}:
                self.image(content)
            elif name in {'description', 'og:description'} and not self.description:
                self.description = content[:1000]
        elif tag == 'a':
            self.anchor = {'url': self.link(attrs.get('href')), 'text': ''}
        elif tag in {'img', 'source'}:
            alt = attrs.get('alt') or ''
            for name in ('data-src', 'data-original', 'src'):
                self.image(attrs.get(name), alt)
            for entry in (attrs.get('srcset') or attrs.get('data-srcset') or '').split(','):
                if entry.strip():
                    self.image(entry.strip().split()[0], alt)
        if tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skipped:
            if tag in self.skipped:
                self.skipped = self.skipped[:self.skipped.index(tag)]
            return
        if tag == 'title':
            self.in_title = False
        elif tag == 'a' and self.anchor:
            if self.anchor['url'] and len(self.links) < 100:
                self.anchor['text'] = ' '.join(self.anchor['text'].split())[:200]
                if not any(item['url'] == self.anchor['url'] for item in self.links):
                    self.links.append(self.anchor)
            self.anchor = None
        if tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.skipped:
            return
        if self.in_title:
            self.title.append(data)
        else:
            self.parts.append(data)
        if self.anchor:
            self.anchor['text'] += data


def readable(parts):
    return '\n'.join(line for line in (' '.join(line.split()) for line in ''.join(parts).splitlines()) if line)


def page_content(resource, *, offset=0, max_chars=12000):
    if not 0 <= offset <= PAGE_BYTES or not 1000 <= max_chars <= 20000:
        raise ValueError('Use a non-negative offset and 1000 to 20000 characters per page read.')
    mime, url = resource['content_type'], resource['url']
    result = {'url': url, 'content_type': mime, 'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'title': '', 'text': '', 'links': [], 'images': [], 'untrusted_content': True}
    if mime.startswith('image/'):
        return {**result, 'images': [{'image_url': url, 'source_url': url, 'alt': ''}],
                'note': 'This URL is an image. Use post_image to attach it when requested.'}
    if mime not in {'', 'application/xhtml+xml', 'application/json', 'application/xml'} and not (
            mime.startswith('text/') or mime.endswith('+json')):
        raise ValueError(f'This link serves {mime}, which the page reader cannot extract as text.')
    try:
        text = resource['body'].decode(resource['charset'], errors='replace')
    except LookupError:
        text = resource['body'].decode('utf-8', errors='replace')
    if mime in {'text/html', 'application/xhtml+xml'} or (not mime and re.search(r'<(?:html|!doctype)', text[:1000], re.I)):
        parser = PageParser(url)
        parser.feed(text)
        parser.close()
        text = readable(parser.parts)
        result.update(title=' '.join(''.join(parser.title).split())[:300], description=parser.description,
                      links=parser.links, images=parser.images)
    result.update(text=text[offset:offset + max_chars], offset=offset, total_chars=len(text),
                  next_offset=offset + max_chars if offset + max_chars < len(text) else None)
    if not text.strip():
        result['note'] = 'No readable page text was returned. The site may require JavaScript or a login.'
    elif offset >= len(text):
        result['note'] = 'The offset is past the end of the page. Read again from offset 0.'
    return result


def reddit_feed(url):
    """Map a Reddit page to its public Atom feed, which Reddit still serves without a login."""
    address = urlsplit(url)
    host = (address.hostname or '').lower()
    if host not in REDDIT_HOSTS:
        return None
    path = re.sub(r'(?:/|\.json|\.rss)+$', '', address.path)
    if host == 'redd.it':
        path = f'/comments{path}' if re.fullmatch(r'/[a-z0-9]{1,12}', path, re.I) else ''
    path = re.sub(r'^/(?:r/[^/]+/)?gallery/', '/comments/', path)
    path = re.sub(r'^/u/', '/user/', path)
    if not path and host == 'redd.it' or re.search(r'/s/[^/]+$', path) or path.split('/')[1:2] in (['media'], ['login']):
        return None
    query = address.query
    if '/comments/' in path and 'limit=' not in query:
        query = f'{query}&limit=100' if query else 'limit=100'
    return urlunsplit(('https', 'www.reddit.com', f'{path}/.rss', query, ''))


def entry_text(entry, base):
    parser = PageParser(base)
    parser.feed(entry.findtext(f'{ATOM}content') or '')
    parser.close()
    text = re.sub(r'\s*submitted by\s+/u/\S+(?:\s*\[(?:link|comments)\])*\s*$', '', readable(parser.parts))
    links = [item for item in parser.links if item['text'] != '[comments]' and '/user/' not in item['url']]
    return text, links, parser.images


def reddit_page(resource, source, *, offset=0, max_chars=12000):
    try:
        root = ElementTree.fromstring(resource['body'])
    except ElementTree.ParseError:
        raise ValueError('Reddit returned a feed that could not be read.') from None
    if root.tag != f'{ATOM}feed':
        raise ValueError('Reddit did not return a readable feed for this link.')
    sections, links, images = [], [], []
    for entry in root.findall(f'{ATOM}entry'):
        author = entry.findtext(f'{ATOM}author/{ATOM}name') or '[deleted]'
        when = (entry.findtext(f'{ATOM}published') or entry.findtext(f'{ATOM}updated') or '')[:16].replace('T', ' ')
        link = entry.find(f'{ATOM}link')
        href = link.get('href', '') if link is not None else ''
        text, found_links, found_images = entry_text(entry, href or source)
        category = entry.find(f'{ATOM}category')
        place = f" in {category.get('label')}" if category is not None and category.get('label') else ''
        if (entry.findtext(f'{ATOM}id') or '').startswith('t1_'):
            sections.append(f'Comment by {author} ({when} UTC):\n{text}')
        else:
            sections.append(f"Post: {entry.findtext(f'{ATOM}title') or ''}\nBy {author}{place} ({when} UTC)\n{href}\n{text}".strip())
        thumbnail = entry.find(f'{MEDIA}thumbnail')
        if thumbnail is not None and thumbnail.get('url'):
            found_images.insert(0, {'image_url': thumbnail.get('url'), 'alt': '', 'source_url': href or source})
        for item, bucket, key in [(value, links, 'url') for value in found_links] + [(value, images, 'image_url') for value in found_images]:
            if len(bucket) < 100 and not any(existing[key] == item[key] for existing in bucket):
                bucket.append(item)
    text = '\n\n'.join(sections)
    page = page_content({'url': source, 'body': text.encode(), 'content_type': 'text/plain', 'charset': 'utf-8'},
                        offset=offset, max_chars=max_chars)
    note = ('Read through Reddit\'s public feed. Comments are listed flat without reply nesting, '
            'and long threads include only the comments the feed returns.')
    page.update(title=' '.join((root.findtext(f'{ATOM}title') or '').split())[:300], links=links, images=images,
                via='reddit_feed', note=f"{page['note']} {note}" if page.get('note') else note)
    return page


def reader_page(resource, source, *, offset=0, max_chars=12000):
    text = resource['body'].decode('utf-8', errors='replace')
    if re.search(r'^Warning: Target URL returned error', text, re.M) or 'Markdown Content:' not in text:
        raise ValueError('The reader service could not open this page either.')
    title = re.search(r'^Title: *(.*)$', text, re.M)
    text = text.split('Markdown Content:', 1)[1].strip()
    links, images = [], []
    for match in re.finditer(r'(!?)\[([^\]]*)\]\((https?://[^)\s]+)', text):
        try:
            url = public_url(match.group(3))
        except ValueError:
            continue
        if match.group(1) and len(images) < 100 and not any(item['image_url'] == url for item in images):
            images.append({'image_url': url, 'alt': match.group(2)[:300], 'source_url': source})
        elif not match.group(1) and len(links) < 100 and not any(item['url'] == url for item in links):
            links.append({'url': url, 'text': ' '.join(match.group(2).split())[:200]})
    page = page_content({'url': source, 'body': text.encode(), 'content_type': 'text/plain', 'charset': 'utf-8'},
                        offset=offset, max_chars=max_chars)
    note = 'The site blocked direct reading, so this text came through the r.jina.ai reader service.'
    page.update(title=(title.group(1).strip() if title else '')[:300], links=links, images=images,
                via='r.jina.ai', note=f"{page['note']} {note}" if page.get('note') else note)
    return page


async def patient_fetch(url, *, check=None):
    """Fetch a fallback source once, waiting out a short rate limit and reusing recent copies for paging."""
    now = time.monotonic()
    cached = _cache.get(url)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        resource = await fetch_resource(url, check=check)
    except FetchError as exc:
        if exc.status != 429 or exc.retry_after is None or exc.retry_after > RETRY_SECONDS:
            raise
        await asyncio.sleep(exc.retry_after + 1)
        resource = await fetch_resource(url, check=check)
    for key in [key for key, (stamp, _) in _cache.items() if now - stamp >= CACHE_SECONDS]:
        del _cache[key]
    while len(_cache) >= CACHE_SIZE:
        del _cache[next(iter(_cache))]
    _cache[url] = (now, resource)
    return resource


async def read_page(url, *, check=None, offset=0, max_chars=12000):
    """Read a public link, using Reddit's feed or a reader service when the site turns bots away."""
    url = public_url(url)
    if check:
        check(url)
    if not 0 <= offset <= PAGE_BYTES or not 1000 <= max_chars <= 20000:
        raise ValueError('Use a non-negative offset and 1000 to 20000 characters per page read.')
    feed = reddit_feed(url)
    if feed:
        try:
            return reddit_page(await patient_fetch(feed, check=check), url, offset=offset, max_chars=max_chars)
        except FetchError as exc:
            if exc.status == 429:
                raise ValueError('Reddit is rate-limiting this server right now. Try the link again in a minute.') from None
            raise ValueError(f'Reddit refused to share this link (HTTP {exc.status}). It may be private, removed or age-restricted.') from None
    blocked = None
    try:
        page = page_content(await fetch_resource(url, check=check), offset=offset, max_chars=max_chars)
    except FetchError as exc:
        if exc.status not in BLOCKED_STATUSES:
            raise
        blocked, page = exc, None
    if page is not None and not page.get('note', '').startswith('No readable'):
        return page
    try:
        return reader_page(await patient_fetch(READER_URL + url, check=check), url, offset=offset, max_chars=max_chars)
    except ValueError:
        if blocked:
            raise ValueError(f'{blocked} The site blocks automated readers, and the backup reader service could not open it either.') from None
        return page


def picture_extension(raw):
    """Verify actual image bytes before uploading, preserving animated GIFs and WebP."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                suffix = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp', 'GIF': 'gif'}.get(image.format)
                if not suffix:
                    raise ValueError('Use a PNG, JPEG, WebP or GIF image.')
                image.verify()
                return suffix
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('The URL did not return a readable PNG, JPEG, WebP or GIF image. Use web_fetch on a page link to find its image URLs.') from None
