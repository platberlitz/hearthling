"""Image-provider settings; paid attempts remain owned by Features' replay ledger."""
import base64
import ipaddress
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, field_validator

from providers import connection_url


BUILTIN_IMAGE_MODELS = {'default': 'gpt-image-1'}
IMAGE_STYLE = 'anime style, cel-shaded, flat colors, clean wordless artwork'
IMAGE_MIME = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp'}
GEMINI_ASPECT = {'1024x1024': '1:1', '1024x1536': '2:3', '1536x1024': '3:2'}


def styled(prompt, art_style=None, house_style=IMAGE_STYLE):
    """Append the house art style; art_style replaces it only when a speaker requests a different style."""
    style = art_style or house_style
    return f'{prompt}, {style}' if style else prompt


def prompt_messages(prompt, limit=1900):
    """Discord messages quoting the exact prompt sent to the image provider."""
    # A zero-width space keeps a literal ``` inside the prompt from closing the code block.
    text = prompt.replace('```', '`\u200b``')
    chunks = [text[start:start + limit] for start in range(0, len(text), limit)]
    return [('Prompt sent to the image model:\n' if index == 0 else '') + f'```\n{chunk}\n```'
            for index, chunk in enumerate(chunks)]


class ImageSpec(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,50}$')
    name: str = Field(min_length=1, max_length=60)
    family: Literal['responses', 'openai_images', 'gemini'] = 'openai_images'
    base_url: str
    model: str = Field(default='gpt-image-1', min_length=1, max_length=150)
    coordinator_model: str = Field(default='gpt-5-mini', min_length=1, max_length=150)
    size: str = Field(default='1024x1024', pattern=r'^(auto|[0-9]{3,4}x[0-9]{3,4})$')
    quality: str = Field(default='high', max_length=30)
    style: str = Field(default='', max_length=100)
    output_format: Literal['png', 'jpeg', 'webp'] = 'png'
    background: Literal['', 'auto', 'transparent', 'opaque'] = ''
    moderation: Literal['', 'auto', 'low'] = ''
    response_format: Literal['', 'b64_json', 'url'] = ''
    timeout: int = Field(default=360, ge=10, le=600)
    local_keyless: bool = False
    supports_edits: bool = False
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @field_validator('base_url')
    @classmethod
    def base(cls, value):
        return connection_url(value)

    @field_validator('extra_body')
    @classmethod
    def finite_body(cls, value):
        if len(json.dumps(value, allow_nan=False)) > 20000:
            raise ValueError('Image request overrides must be at most 20000 characters.')
        return value


class ImageOptions(BaseModel):
    image_providers: list[ImageSpec] = Field(default_factory=list, max_length=20)
    image_provider: str = Field(default='default', max_length=50)
    image_fallback_providers: list[str] = Field(default_factory=list, max_length=20)
    dm_followup_image_provider: str = Field(default='', max_length=50)

    @field_validator('image_fallback_providers')
    @classmethod
    def unique_fallbacks(cls, value):
        if len(value) != len(set(value)) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', item) for item in value):
            raise ValueError('List each fallback image provider once, using its saved ID.')
        return value

    @field_validator('image_providers')
    @classmethod
    def unique_ids(cls, value):
        ids = [item.id for item in value]
        if set(ids) & BUILTIN_IMAGE_MODELS.keys() or len(ids) != len(set(ids)):
            raise ValueError('Give each additional image provider a unique ID other than default.')
        return value


def selected(features, preferred=None):
    identity = preferred or features.settings.image_provider
    if identity in BUILTIN_IMAGE_MODELS:
        config = features.settings
        base_url = config.image_base_url or config.ai_base_url
        spec = ImageSpec(id=identity, name='Default image provider', family=config.image_family, base_url=base_url,
                         model=config.image_model or BUILTIN_IMAGE_MODELS[identity], coordinator_model=config.image_coordinator_model,
                         size=config.image_size, quality=config.image_quality, supports_edits=True)
        key = features.credentials.get('image_api_key') or features.credentials.get('ai_api_key', '')
    else:
        spec = next((item for item in features.settings.image_providers if item.id == identity), None)
        if spec is None:
            raise ValueError('Choose a saved image provider.')
        key = features.credentials.get('image_key:' + identity, '')
    if not key and not spec.local_keyless:
        raise ValueError(f'{spec.name} has no saved image key.')
    return spec, key


def image_request(spec, prompt, size=None, references=None, art_style=None, house_style=IMAGE_STYLE):
    prompt = styled(prompt, art_style, house_style)
    options = {'model': spec.model, 'quality': spec.quality, 'size': size or spec.size}
    for name in ('style', 'background', 'moderation'):
        if value := getattr(spec, name):
            options[name] = value
    if spec.output_format != 'png':
        options['output_format'] = spec.output_format
    references = list(references or [])
    if spec.family == 'responses':
        content = [{'type': 'input_text', 'text': prompt}]
        for raw in references:
            mime = IMAGE_MIME[extension(raw)]
            content.append({'type': 'input_image', 'image_url': f'data:{mime};base64,{base64.b64encode(raw).decode()}',
                            'detail': 'high'})
        return {'model': spec.coordinator_model, 'input': [{'role': 'user', 'content': content}],
                'tools': [{'type': 'image_generation', **options, 'action': 'edit' if references else 'generate'}],
                'tool_choice': {'type': 'image_generation'}, **spec.extra_body}
    if spec.family == 'gemini':
        parts = [{'text': prompt}] + [{'inline_data': {'mime_type': IMAGE_MIME[extension(raw)], 'data': base64.b64encode(raw).decode()}}
                                      for raw in references]
        image_config = {'aspectRatio': GEMINI_ASPECT.get(size or spec.size, '1:1')}
        if spec.quality in {'512', '1K', '2K', '4K'}:
            image_config['imageSize'] = spec.quality
        return {'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': {'responseModalities': ['IMAGE'], 'imageConfig': image_config}, **spec.extra_body}
    if references:
        if not spec.supports_edits:
            raise ValueError(f'{spec.name} cannot use reference images.')
        form = aiohttp.FormData()
        for name, value in {'prompt': prompt, **options, **spec.extra_body}.items():
            form.add_field(name, value if isinstance(value, str) else json.dumps(value))
        for index, raw in enumerate(references):
            suffix = extension(raw)
            form.add_field('image[]', raw, filename=f'reference-{index}.{suffix}', content_type=IMAGE_MIME[suffix])
        return form
    if spec.response_format:
        options['response_format'] = spec.response_format
    return {'prompt': prompt, **options, **spec.extra_body}


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate the addresses actually used, rather than a separate DNS preflight."""
    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        addresses = await self.resolver.resolve(host, port, family)
        if not addresses or any(not ipaddress.ip_address(item['host']).is_global for item in addresses):
            raise OSError('An image download resolved to a non-public address.')
        return addresses

    async def close(self):
        await self.resolver.close()


async def download_image(url, *, maximum=25 * 1024 * 1024):
    from features import FeatureError, bounded_body
    try:
        address = urlsplit(url)
        if (address.scheme != 'https' or not address.hostname or address.username or address.password
                or address.port not in {None, 443}):
            raise ValueError('The provider returned an invalid image URL.')
        try:
            if not ipaddress.ip_address(address.hostname).is_global:
                raise ValueError('The provider returned a non-public image URL.')
        except ValueError:
            if re.fullmatch(r'[0-9a-fA-F:.]+', address.hostname):
                raise
        resolver = PublicResolver()
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=resolver), timeout=aiohttp.ClientTimeout(total=45)) as session:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError(f'Image download returned HTTP {response.status}.')
                    return await bounded_body(response, maximum)
        finally:
            await resolver.close()
    except (ValueError, OSError, aiohttp.ClientError) as exc:
        raise FeatureError(f'The image could not be downloaded ({type(exc).__name__}); the request was not repeated.', uncertain=True) from None


def request_target(spec, key, *, edit=False):
    if spec.family == 'gemini':
        return f'{spec.base_url}/v1beta/models/{spec.model}:generateContent', {'x-goog-api-key': key} if key else {}
    path = '/responses' if spec.family == 'responses' else '/images/edits' if edit else '/images/generations'
    return spec.base_url + path, {'Authorization': 'Bearer ' + key} if key else {}


def gemini_picture(data):
    """Final image part of a generateContent reply; thought-preview images are skipped."""
    candidates = data.get('candidates') if isinstance(data, dict) else None
    pictures, notes = [], []
    for candidate in candidates if isinstance(candidates, list) else []:
        content = candidate.get('content') if isinstance(candidate, dict) else None
        parts = content.get('parts') if isinstance(content, dict) else None
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict) or part.get('thought'):
                continue
            inline = part.get('inlineData') or part.get('inline_data')
            if isinstance(inline, dict) and inline.get('data'):
                pictures.append(inline['data'])
            elif isinstance(part.get('text'), str):
                notes.append(part['text'])
        if isinstance(candidate, dict) and candidate.get('finishReason') not in {None, 'STOP'}:
            notes.append(f"Finished with {candidate['finishReason']}.")
    feedback = data.get('promptFeedback') if isinstance(data, dict) else None
    if isinstance(feedback, dict) and feedback.get('blockReason'):
        notes.append(f"Blocked: {feedback['blockReason']}.")
    return (pictures[-1] if pictures else None), ' '.join(notes).strip()


def extension(raw):
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if raw.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        return 'webp'
    raise ValueError('The provider returned an unsupported image file.')
