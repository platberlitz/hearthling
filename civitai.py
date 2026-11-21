"""Read Civitai's public model and image listings, signed in with the owner's API key when one is saved."""
import asyncio
from datetime import datetime, timezone
import html
import json
import re
import time
from urllib.parse import urlencode

import aiohttp


API = 'https://civitai.com/api/v1'
SITE = 'https://civitai.com'
MODEL_TYPES = ('Checkpoint', 'LORA', 'LoCon', 'DoRA', 'TextualInversion', 'Hypernetwork', 'AestheticGradient',
               'Controlnet', 'Upscaler', 'MotionModule', 'VAE', 'Poses', 'Wildcards', 'Workflows', 'Detection', 'Other')
MODEL_SORTS = {'newest': 'Newest', 'most_downloaded': 'Most Downloaded', 'highest_rated': 'Highest Rated'}
IMAGE_SORTS = {'most_reactions': 'Most Reactions', 'most_comments': 'Most Comments', 'newest': 'Newest'}
PERIODS = {'day': 'Day', 'week': 'Week', 'month': 'Month', 'year': 'Year', 'all': 'AllTime'}
MODEL_LINK = re.compile(r'https?://(?:www\.)?civitai\.(?:com|green)/models/(\d+)')
CACHE_SECONDS = 300
MAX_BODY = 4 * 1024 * 1024


class CivitaiError(ValueError):
    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def plain(value, limit):
    text = html.unescape(re.sub(r'<[^>]+>', ' ', str(value or '')))
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def adult_allowed(request):
    """Adult listings only reach age-restricted channels in the server."""
    channel = getattr(request, 'channel', None)
    check = getattr(channel, 'is_nsfw', None)
    return getattr(request, 'guild', None) is not None and callable(check) and bool(check())


def model_id(value):
    if isinstance(value, int):
        return value
    found = MODEL_LINK.search(value or '')
    if not found:
        raise CivitaiError('Give a Civitai model ID or a civitai.com/models/ link.')
    return int(found.group(1))


def sized(url, kind):
    """Civitai originals can pass Discord's upload limit; its image server returns a smaller copy on request."""
    return url.replace('/original=true/', '/transcode=true,width=450/' if kind == 'video' else '/width=1024/', 1)


def image_entry(item):
    meta = item.get('meta') if isinstance(item.get('meta'), dict) else {}
    stats = item.get('stats') if isinstance(item.get('stats'), dict) else {}
    kind = item.get('type') or 'image'
    return {'image_url': sized(item['url'], kind), 'original_url': item['url'], 'source_url': f'{SITE}/images/{item["id"]}',
            'creator': str(item.get('username') or '')[:100],
            'base_model': item.get('baseModel') or '', 'kind': kind, 'adult': item.get('nsfwLevel') not in {None, 'None'},
            'reactions': sum(value for name, value in stats.items() if name.endswith('Count') and name != 'commentCount' and isinstance(value, int)),
            'created_at': item.get('createdAt') or '', 'prompt': plain(meta.get('prompt'), 300)}


def model_summary(item):
    versions = item.get('modelVersions') if isinstance(item.get('modelVersions'), list) else []
    latest = versions[0] if versions and isinstance(versions[0], dict) else {}
    stats = item.get('stats') if isinstance(item.get('stats'), dict) else {}
    creator = item.get('creator') if isinstance(item.get('creator'), dict) else {}
    return {'id': item['id'], 'name': plain(item.get('name'), 150), 'type': item.get('type') or '',
            'base_model': latest.get('baseModel') or '', 'latest_version': plain(latest.get('name'), 100),
            'published_at': latest.get('publishedAt') or '', 'creator': str(creator.get('username') or '')[:100],
            'downloads': stats.get('downloadCount', 0), 'likes': stats.get('thumbsUpCount', 0),
            'adult': bool(item.get('nsfw')), 'url': f'{SITE}/models/{item["id"]}'}


class Civitai:
    def __init__(self, credentials):
        self.credentials = credentials
        self.cache = {}

    def key(self):
        return self.credentials().get('civitai_api_key', '')

    async def get(self, path, params=None, *, cache=True):
        query = urlencode([(name, value) for name, value in (params or {}).items() if value not in (None, '')], doseq=True)
        url = f'{API}/{path}' + (f'?{query}' if query else '')
        key = self.key()
        cached = self.cache.get((url, bool(key)))
        if cache and cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        headers = {'Accept': 'application/json', 'User-Agent': 'Hearthling/1.0'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(url, headers=headers, allow_redirects=False) as response:
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > MAX_BODY:
                            raise CivitaiError('Civitai returned more data than this lookup supports.')
                    if response.status == 401:
                        raise CivitaiError('Civitai rejected the saved API key. The owner can replace it in dashboard Connections.'
                                           if key else 'Civitai needs a signed-in API key for this. The owner can add one in dashboard Connections.')
                    if response.status == 404:
                        raise CivitaiError('Civitai has no public record at that address; it may be deleted, private or the ID may be wrong.')
                    if response.status != 200:
                        raise CivitaiError(f'Civitai returned HTTP {response.status} ({response.reason}).',
                                           retry_after=60 if response.status == 429 or response.status >= 500 else None)
                    try:
                        data = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise CivitaiError('Civitai returned a page instead of its listing data.') from None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise CivitaiError(f'Civitai could not be reached ({type(exc).__name__}).', retry_after=60) from None
        if len(self.cache) >= 64:
            self.cache.pop(next(iter(self.cache)))
        self.cache[(url, bool(key))] = (time.time(), data)
        return data

    async def account(self):
        if not self.key():
            raise CivitaiError('No Civitai API key is saved. Listings still work signed out.')
        data = await self.get('me', cache=False)
        return {'signed_in': True, 'username': str(data.get('username') or '')[:100], 'tier': data.get('tier') or ''}

    async def models(self, *, query='', limit=10, sort=None, period=None, model_type=None, base_model=None, adult=False):
        if sort and sort not in MODEL_SORTS or period and period not in PERIODS or model_type and model_type not in MODEL_TYPES:
            raise CivitaiError('Use a supported Civitai sort, period and model type.')
        params = {'limit': min(max(limit, 1), 20) * (1 if adult else 2), 'query': query.strip()[:200],
                  'sort': MODEL_SORTS.get(sort or ('highest_rated' if query.strip() else 'most_downloaded')),
                  'period': PERIODS.get(period or ('all' if query.strip() else 'week')), 'types': model_type,
                  'baseModels': base_model, 'nsfw': None if adult else 'false'}
        data = await self.get('models', params)
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CivitaiError('Civitai changed its model listing format. No results could be verified.')
        models = [model_summary(item) for item in items if isinstance(item, dict) and isinstance(item.get('id'), int)]
        hidden = 0 if adult else sum(model['adult'] for model in models)
        models = [model for model in models if adult or not model['adult']][:limit]
        return {'source': f'{SITE}/models?' + urlencode({key: value for key, value in (('query', query.strip()), ('baseModels', base_model)) if value}),
                'filters': {'query': query.strip(), 'sort': params['sort'], 'period': params['period'], 'type': model_type or 'any',
                            'base_model': base_model or 'any', 'adult_content': 'included' if adult else 'hidden'},
                'signed_in': bool(self.key()), 'fetched_at': now(), 'models': models, 'adult_hidden': hidden}

    async def model(self, identity, *, adult=False):
        data = await self.get(f'models/{model_id(identity)}')
        if not isinstance(data, dict) or not isinstance(data.get('id'), int):
            raise CivitaiError('Civitai returned an unreadable model record.')
        summary = model_summary(data)
        if summary['adult'] and not adult:
            raise CivitaiError('Civitai marks this model as adult content. It can be shown in an age-restricted channel.')
        versions = []
        for version in (data.get('modelVersions') or [])[:5]:
            if not isinstance(version, dict):
                continue
            stats = version.get('stats') if isinstance(version.get('stats'), dict) else {}
            versions.append({'id': version.get('id'), 'name': plain(version.get('name'), 100), 'base_model': version.get('baseModel') or '',
                             'published_at': version.get('publishedAt') or '', 'downloads': stats.get('downloadCount', 0),
                             'trigger_words': [plain(word, 80) for word in (version.get('trainedWords') or [])[:10] if isinstance(word, str)],
                             'notes': plain(version.get('description'), 300)})
        latest = next((version['id'] for version in versions if isinstance(version['id'], int)), None)
        # Civitai's modelId image filter stalls and returns nothing; the version filter is fast.
        images = (await self.images(version=latest, limit=6, sort='most_reactions', period='all', adult=adult))['images'] if latest else []
        return {**summary, 'tags': [plain(tag, 50) for tag in (data.get('tags') or [])[:15] if isinstance(tag, str)],
                'description': plain(data.get('description'), 1200), 'commercial_use': data.get('allowCommercialUse'),
                'versions': versions, 'sample_images': images, 'signed_in': bool(self.key()), 'fetched_at': now()}

    async def images(self, *, version=None, limit=10, sort=None, period=None, adult=False):
        if sort and sort not in IMAGE_SORTS or period and period not in PERIODS:
            raise CivitaiError('Use a supported Civitai image sort and period.')
        params = {'limit': min(max(limit, 1), 20) * (1 if adult else 2), 'modelVersionId': version,
                  'sort': IMAGE_SORTS[sort or 'most_reactions'], 'period': PERIODS[period or 'day'], 'nsfw': None if adult else 'None'}
        data = await self.get('images', params)
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CivitaiError('Civitai changed its image listing format. No pictures could be verified.')
        images = [image_entry(item) for item in items if isinstance(item, dict) and isinstance(item.get('id'), int)
                  and isinstance(item.get('url'), str) and item['url'].startswith('https://image.civitai.com/')]
        images = [image for image in images if adult or not image['adult']][:limit]
        return {'source': f'{SITE}/images', 'filters': {'model_version_id': version, 'sort': params['sort'], 'period': params['period'],
                'adult_content': 'included' if adult else 'hidden'}, 'signed_in': bool(self.key()), 'fetched_at': now(), 'images': images}

    async def base_models(self):
        data = await self.get('enums')
        names = data.get('ActiveBaseModel') if isinstance(data, dict) else None
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise CivitaiError('Civitai changed its base model list format.')
        return names


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
