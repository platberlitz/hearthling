"""Ordered provider profiles with one internal message format and explicit adapters."""
import asyncio
import base64
import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import time
from typing import Any, Literal
from urllib.parse import quote, urlsplit
import uuid

import aiohttp
import httpx
from openai import APIConnectionError, APIError, APIStatusError, AsyncOpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml


def connection_url(value):
    parts = urlsplit(value)
    if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or (parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in {'localhost', '127.0.0.1', '::1'}))):
        raise ValueError('Use an HTTPS base address without credentials, a query or a fragment.')
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError('Invalid connection port.')
    return value.rstrip('/')


def yaml_object(text, *, exclude=False):
    try:
        value = yaml.safe_load(text) if text.strip() else {}
        if not isinstance(value, dict) and not (exclude and isinstance(value, list) and all(isinstance(item, str) for item in value)):
            raise ValueError('Request YAML must be a mapping; exclusions may also be a list of top-level keys.')
        json.dumps(value, allow_nan=False)
        return value
    except (yaml.YAMLError, TypeError, RecursionError, ValueError):
        raise ValueError('Use finite, JSON-compatible YAML mappings without recursive aliases.') from None


def merge(target, changes):
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def exclude(target, keys):
    for key in keys:
        if isinstance(keys, dict) and isinstance(keys[key], dict) and isinstance(target.get(key), dict):
            exclude(target[key], keys[key])
        else:
            target.pop(key, None)


ReasoningEffort = Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']


class ProviderOptions(BaseModel):
    provider_order: list[str] = Field(default_factory=list, max_length=20)
    polisher_enabled: bool = False
    polisher_provider: str = Field(default='', max_length=50)
    polisher_max_tokens: int = Field(default=8192, ge=256, le=16384)
    diagnostics_enabled: bool = False
    raw_generation_logging: bool = False

    @field_validator('provider_order')
    @classmethod
    def identifiers(cls, value):
        if len(set(value)) != len(value) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', item) for item in value):
            raise ValueError('Provider order must contain distinct saved provider IDs, or current.')
        return value


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,50}$')
    name: str = Field(min_length=1, max_length=60)
    family: Literal['openai_chat', 'openai_responses', 'anthropic', 'gemini'] = 'openai_chat'
    base_url: str
    model: str = Field(min_length=1, max_length=150)
    vision: bool = False
    local_keyless: bool = False
    append_version: bool = False
    timeout: int = Field(default=90, ge=5, le=360)
    max_tokens: int = Field(default=4096, ge=256, le=65536)
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_effort: ReasoningEffort | None = None
    thinking_budget: int = Field(default=0, ge=0, le=64000)
    prefill: str = Field(default='', max_length=4000)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    include_body_yaml: str = Field(default='', max_length=20000)
    exclude_body_yaml: str = Field(default='', max_length=20000)

    @field_validator('base_url')
    @classmethod
    def base(cls, value):
        return connection_url(value)

    @field_validator('include_body_yaml', 'exclude_body_yaml')
    @classmethod
    def valid_yaml(cls, value, info):
        yaml_object(value, exclude=info.field_name == 'exclude_body_yaml')
        return value

    @field_validator('extra_body')
    @classmethod
    def finite_json(cls, value):
        if len(json.dumps(value, allow_nan=False)) > 20000:
            raise ValueError('Extra request JSON must be at most 20000 characters.')
        return value

    @model_validator(mode='after')
    def valid_thinking(self):
        if self.family == 'anthropic' and self.thinking_budget and self.thinking_budget >= self.max_tokens:
            raise ValueError('Anthropic thinking tokens must be less than the total output token limit.')
        return self


class ProviderInput(ProviderSpec):
    api_key: str = Field(default='', max_length=4096, exclude=True)
    headers: dict[str, str] | None = Field(default=None, exclude=True, max_length=30)

    @field_validator('api_key')
    @classmethod
    def single_key(cls, value):
        if any(char.isspace() for char in value):
            raise ValueError('A provider key must not contain spaces or line breaks.')
        return value

    @field_validator('headers')
    @classmethod
    def header_values(cls, value):
        if value is not None and any(not re.fullmatch(r'[A-Za-z0-9-]{1,100}', key) or '\r' in item or '\n' in item or len(item) > 4096
                                     for key, item in value.items()):
            raise ValueError('Use valid header names and single-line values.')
        return value


FALLBACK_PREFIX = 'current-fallback-'


def main_connection(spec):
    return spec.id == 'current' or spec.id.startswith(FALLBACK_PREFIX)


def content_text(content):
    return content if isinstance(content, str) else '\n'.join(part.get('text', '') for part in content or [] if part.get('type') == 'text')


def with_prefill(spec, messages, partial=True):
    """End the conversation with the saved start of the reply; Kimi K3 needs partial mode to continue it."""
    if not spec.prefill.strip() or not messages or messages[-1]['role'] != 'user':
        return messages
    message = {'role': 'assistant', 'content': spec.prefill}
    if partial and spec.family == 'openai_chat' and 'kimi-k3' in spec.model.lower():
        message['partial'] = True
    return messages + [message]


def strip_prefill(spec, response):
    """Some providers echo the prefill back; the posted reply never repeats it."""
    message = response.choices[0].message
    if spec.prefill.strip() and message.content and message.content.startswith(spec.prefill):
        message.content = message.content[len(spec.prefill):].lstrip() or None
    return response


def build_request(spec, messages, options):
    """Convert normalised history, including tool results, without changing authority."""
    messages = copy.deepcopy(messages)
    tools = options.get('tools', [])
    choice = options.get('tool_choice', 'auto')
    limit = min(options.get('max_tokens', spec.max_tokens), spec.max_tokens)
    if spec.family == 'anthropic' and spec.thinking_budget:
        limit = min(spec.max_tokens, max(limit, spec.thinking_budget + 1024))
    body = {'model': spec.model}
    if spec.family == 'openai_chat':
        body.update(messages=[{k: v for k, v in message.items() if k != 'provider_blocks'} for message in messages], max_tokens=limit)
        body.update({key: value for key, value in options.items() if key not in {'max_tokens'}})
        if spec.reasoning_effort:
            body['reasoning_effort'] = spec.reasoning_effort
        if spec.thinking_budget:
            body['thinking'] = {'type': 'enabled', 'budget_tokens': spec.thinking_budget}
    elif spec.family == 'openai_responses':
        inputs = []
        for message in messages:
            if message['role'] == 'tool':
                inputs.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message['content']})
                continue
            parts = []
            for part in message.get('content') or [] if isinstance(message.get('content'), list) else [{'type': 'text', 'text': message.get('content') or ''}]:
                if part['type'] == 'text':
                    parts.append({'type': 'output_text' if message['role'] == 'assistant' else 'input_text', 'text': part['text']})
                elif part['type'] == 'image_url':
                    parts.append({'type': 'input_image', 'image_url': part['image_url']['url'], 'detail': part['image_url'].get('detail', 'auto')})
            if parts:
                inputs.append({'role': message['role'], 'content': parts})
            inputs.extend({'type': 'function_call', 'call_id': call['id'], **call['function']} for call in message.get('tool_calls', []))
        body.update(input=inputs, max_output_tokens=limit, store=False)
        if tools:
            translated = {'type': 'function', 'name': choice['function']['name']} if isinstance(choice, dict) else choice
            body.update(tools=[{'type': 'function', **tool['function']} for tool in tools], tool_choice=translated, parallel_tool_calls=False)
        if spec.reasoning_effort:
            body['reasoning'] = {'effort': spec.reasoning_effort}
    elif spec.family == 'anthropic':
        items, system = [], []
        for message in messages:
            if message['role'] == 'system':
                system.append(content_text(message.get('content')))
                continue
            role = 'assistant' if message['role'] == 'assistant' else 'user'
            parts = []
            if message['role'] == 'tool':
                parts.append({'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': message['content']})
            elif message.get('provider_blocks', {}).get('provider') == spec.id:
                parts = message['provider_blocks']['content']
            else:
                for part in message.get('content') or [] if isinstance(message.get('content'), list) else [{'type': 'text', 'text': message.get('content') or ''}]:
                    if part['type'] == 'text' and part['text']:
                        parts.append({'type': 'text', 'text': part['text']})
                    elif part['type'] == 'image_url':
                        parts.append({'type': 'image', 'source': {'type': 'url', 'url': part['image_url']['url']}})
                parts.extend({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])}
                             for call in message.get('tool_calls', []))
            if parts:
                if items and items[-1]['role'] == role:
                    items[-1]['content'].extend(parts)
                else:
                    items.append({'role': role, 'content': parts})
        body.update(messages=items, system='\n\n'.join(system), max_tokens=limit)
        if tools:
            translated = ({'type': 'tool', 'name': choice['function']['name']} if isinstance(choice, dict) else
                          {'type': 'none' if choice == 'none' else 'any' if choice == 'required' else 'auto'})
            body.update(tools=[{'name': tool['function']['name'], 'description': tool['function'].get('description', ''),
                               'input_schema': tool['function']['parameters']} for tool in tools],
                        tool_choice=translated if choice == 'none' else {**translated, 'disable_parallel_tool_use': True})
        if spec.thinking_budget:
            body['thinking'] = {'type': 'enabled', 'budget_tokens': spec.thinking_budget}
        elif spec.reasoning_effort:
            body.update(thinking={'type': 'adaptive'}, output_config={'effort': spec.reasoning_effort})
    else:
        items, system, names = [], [], {}
        for message in messages:
            if message['role'] == 'system':
                system.append({'text': content_text(message.get('content'))})
                continue
            parts = []
            if message['role'] == 'tool':
                try:
                    result = json.loads(message['content'])
                except ValueError:
                    result = {'text': message['content']}
                parts.append({'functionResponse': {'name': names.get(message['tool_call_id'], message['tool_call_id']),
                                                    'response': result if isinstance(result, dict) else {'result': result}}})
            elif message.get('provider_blocks', {}).get('provider') == spec.id:
                parts = message['provider_blocks']['content']
            else:
                for part in message.get('content') or [] if isinstance(message.get('content'), list) else [{'type': 'text', 'text': message.get('content') or ''}]:
                    if part['type'] == 'text' and part['text']:
                        parts.append({'text': part['text']})
                    elif part['type'] == 'image_url':
                        parts.append({'discordImage': part['image_url']['url']})
                parts.extend({'functionCall': {'name': call['function']['name'], 'args': json.loads(call['function']['arguments'])}}
                             for call in message.get('tool_calls', []))
            names.update({call['id']: call['function']['name'] for call in message.get('tool_calls', [])})
            if parts:
                role = 'model' if message['role'] == 'assistant' else 'user'
                if items and items[-1]['role'] == role:
                    items[-1]['parts'].extend(parts)
                else:
                    items.append({'role': role, 'parts': parts})
        body.pop('model')
        body.update(contents=items, systemInstruction={'parts': system}, generationConfig={'maxOutputTokens': limit})
        if tools:
            translated = {'mode': 'NONE' if choice == 'none' else 'ANY' if choice == 'required' or isinstance(choice, dict) else 'AUTO'}
            if isinstance(choice, dict):
                translated['allowedFunctionNames'] = [choice['function']['name']]
            body.update(tools=[{'functionDeclarations': [{'name': tool['function']['name'], 'description': tool['function'].get('description', ''),
                              'parametersJsonSchema': tool['function']['parameters']} for tool in tools]}],
                         toolConfig={'functionCallingConfig': translated})
        if spec.thinking_budget:
            body['generationConfig']['thinkingConfig'] = {'thinkingBudget': spec.thinking_budget}
        elif spec.reasoning_effort:
            body['generationConfig']['thinkingConfig'] = {'thinkingLevel': spec.reasoning_effort.upper()}
    if spec.temperature is not None:
        (body['generationConfig'] if spec.family == 'gemini' else body)['temperature'] = spec.temperature
    merge(body, spec.extra_body)
    merge(body, yaml_object(spec.include_body_yaml))
    exclude(body, yaml_object(spec.exclude_body_yaml, exclude=True))
    return body


def normalise(spec, data):
    if not isinstance(data, dict):
        raise ValueError('The provider response must be a JSON object.')
    if spec.family == 'openai_chat':
        return ChatCompletion.model_validate(data)
    calls, texts, blocks, usage = [], [], None, {}
    if spec.family == 'openai_responses':
        for item in data.get('output', []):
            if item.get('type') == 'function_call':
                calls.append({'id': item.get('call_id') or item['id'], 'type': 'function',
                              'function': {'name': item['name'], 'arguments': item['arguments']}})
            elif item.get('type') == 'message':
                texts.extend(part['text'] for part in item.get('content', []) if part.get('type') == 'output_text')
        usage = data.get('usage', {})
    elif spec.family == 'anthropic':
        blocks = data.get('content', [])
        for item in blocks:
            if item.get('type') == 'text':
                texts.append(item['text'])
            elif item.get('type') == 'tool_use':
                calls.append({'id': item['id'], 'type': 'function', 'function': {'name': item['name'], 'arguments': json.dumps(item['input'])}})
        usage = data.get('usage', {})
    else:
        candidates = data.get('candidates', [])
        blocks = candidates[0].get('content', {}).get('parts', []) if candidates else []
        for item in blocks:
            if 'text' in item and not item.get('thought'):
                texts.append(item['text'])
            if 'functionCall' in item:
                call = item['functionCall']
                calls.append({'id': call.get('id') or 'call_' + uuid.uuid4().hex, 'type': 'function',
                              'function': {'name': call['name'], 'arguments': json.dumps(call.get('args', {}))}})
        usage = data.get('usageMetadata', {})
    if not texts and not calls:
        raise ValueError('The provider returned no dialogue or tool calls.')
    input_tokens = usage.get('input_tokens', usage.get('promptTokenCount', 0))
    output_tokens = usage.get('output_tokens', usage.get('candidatesTokenCount', 0))
    result = ChatCompletion.model_validate({'id': str(data.get('id', uuid.uuid4().hex)), 'object': 'chat.completion',
        'created': int(time.time()), 'model': spec.model, 'choices': [{'index': 0, 'finish_reason': 'tool_calls' if calls else 'stop',
        'message': {'role': 'assistant', 'content': '\n'.join(texts) or None, 'tool_calls': calls or None}}],
        'usage': {'prompt_tokens': input_tokens, 'completion_tokens': output_tokens, 'total_tokens': input_tokens + output_tokens}})
    if blocks is not None:
        result.choices[0].message.provider_blocks = {'provider': spec.id, 'content': blocks}
    return result


class Providers:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db
        self.rejected_vision, self.rejected_prefill, self.rejected_partial = set(), set(), set()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS providers (id TEXT PRIMARY KEY, configuration TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS provider_requests (id INTEGER PRIMARY KEY, at REAL NOT NULL, provider TEXT NOT NULL,
                model TEXT NOT NULL, seconds REAL NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, error TEXT);
        ''')

    def current(self):
        config = self.bot.features.settings
        return ProviderSpec(id='current', name='Main connection', model=config.ai_model, base_url=config.ai_base_url, vision=config.vision_enabled,
                            reasoning_effort=config.ai_reasoning_effort, prefill=config.ai_prefill)

    def current_fallbacks(self):
        """Fallback models share the main connection's address and key, with optional per-model reasoning."""
        main = self.current()
        settings = self.bot.features.settings
        models = [model for model in settings.ai_fallback_models if model != main.model]
        return [main.model_copy(update={'id': f'{FALLBACK_PREFIX}{number}', 'name': f'Fallback model {number}', 'model': model,
                                       'reasoning_effort': settings.ai_fallback_reasoning_efforts[model]
                                       if model in settings.ai_fallback_reasoning_efforts else main.reasoning_effort})
                for number, model in enumerate(models, 1)]

    def get(self, identity):
        if identity == 'current':
            return self.current()
        if identity.startswith(FALLBACK_PREFIX):
            spec = next((item for item in self.current_fallbacks() if item.id == identity), None)
            if not spec:
                raise ValueError('That fallback model is no longer saved.')
            return spec
        row = self.db.execute('SELECT configuration FROM providers WHERE id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('That provider profile does not exist.')
        return ProviderSpec.model_validate_json(row[0])

    def list(self):
        values = [self.current()] + [ProviderSpec.model_validate_json(row[0]) for row in self.db.execute('SELECT configuration FROM providers ORDER BY rowid')]
        return [spec.model_dump() | {'key_saved': bool(self.key(spec)), 'header_names': list(self.headers(spec))} for spec in values]

    def key(self, spec):
        return self.bot.features.credentials.get('ai_api_key' if main_connection(spec) else 'provider_key:' + spec.id, '')

    def headers(self, spec):
        return self.bot.features.credentials.get('provider_headers:' + spec.id, {})

    def save(self, data):
        from features import write_private_json
        value = ProviderInput.model_validate(data)
        if value.id == 'current' or value.id.startswith(FALLBACK_PREFIX):
            raise ValueError('Edit the main connection and its fallback models at the top of Providers, or choose another profile ID.')
        credentials = dict(self.bot.features.credentials)
        if value.api_key:
            credentials['provider_key:' + value.id] = value.api_key
        if value.headers is not None:
            credentials['provider_headers:' + value.id] = value.headers
        write_private_json(self.bot.features.data_dir / 'connections.json', credentials)
        self.bot.features.credentials = credentials
        self.db.execute('INSERT OR REPLACE INTO providers VALUES (?,?)', (value.id, value.model_dump_json()))
        self.db.commit()
        self.rejected_vision.clear()
        self.rejected_prefill.clear()
        self.rejected_partial.clear()

    def delete(self, identity):
        from features import write_private_json
        if identity == 'current' or identity in self.bot.features.settings.provider_order or identity == self.bot.features.settings.polisher_provider:
            raise ValueError('Remove this provider from fallback and polishing choices before deleting it.')
        if any(item['provider'] == identity for item in self.bot.characters.list()):
            raise ValueError('A character still uses this provider.')
        self.db.execute('DELETE FROM providers WHERE id=?', (identity,))
        self.db.commit()
        for prefix in ('provider_key:', 'provider_headers:'):
            self.bot.features.credentials.pop(prefix + identity, None)
        write_private_json(self.bot.features.data_dir / 'connections.json', self.bot.features.credentials)

    def order(self, preferred=None):
        active = self.bot.characters.active()
        preferred = preferred or (active['provider'] if active else '')
        order = self.bot.features.settings.provider_order or ['current']
        expanded = []
        for identity in dict.fromkeys(([preferred] if preferred else []) + order):
            expanded.append(identity)
            if identity == 'current':
                expanded.extend(spec.id for spec in self.current_fallbacks())
        return list(dict.fromkeys(expanded))

    def vision_enabled(self):
        return any(self.get(identity).vision for identity in self.order())

    async def gemini_images(self, body):
        from conversation import image_url
        cache = {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            for message in body.get('contents', []):
                for part in message['parts']:
                    if 'discordImage' not in part:
                        continue
                    url = part.pop('discordImage')
                    if not image_url(url):
                        raise ValueError('Only supplied Discord images may be sent to a vision provider.')
                    if url not in cache:
                        async with session.get(url, allow_redirects=False) as response:
                            raw = bytearray()
                            async for chunk in response.content.iter_chunked(65536):
                                raw.extend(chunk)
                                if len(raw) > 10 * 1024 * 1024:
                                    raise ValueError('The Discord image exceeds 10 MiB.')
                            kind = response.headers.get('Content-Type', '').split(';')[0]
                            if response.status != 200 or kind not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}:
                                raise ValueError('The Discord image could not be read as a supported image.')
                            cache[url] = {'mimeType': kind, 'data': base64.b64encode(raw).decode()}
                    part['inlineData'] = cache[url]

    async def send(self, spec, messages, options):
        body = build_request(spec, messages, options)
        if main_connection(spec):
            return await self.bot.ai.chat.completions.create(**body)
        key, headers = self.key(spec), {'Accept': 'application/json', **self.headers(spec)}
        if not key and not spec.local_keyless:
            raise APIConnectionError(request=httpx.Request('POST', spec.base_url), message=f'{spec.name} has no saved key.')
        base = spec.base_url
        version = '/v1beta' if spec.family == 'gemini' else '/v1'
        if spec.append_version and not base.endswith(version):
            base += version
        paths = {'openai_chat': '/chat/completions', 'openai_responses': '/responses', 'anthropic': '/messages',
                 'gemini': f'/models/{quote(spec.model.removeprefix("models/"), safe="")}:generateContent'}
        url = base + paths[spec.family]
        if key:
            headers.setdefault('x-api-key' if spec.family == 'anthropic' else 'x-goog-api-key' if spec.family == 'gemini' else 'Authorization',
                               key if spec.family in {'anthropic', 'gemini'} else 'Bearer ' + key)
        if spec.family == 'anthropic':
            headers.setdefault('anthropic-version', '2023-06-01')
        request = httpx.Request('POST', url)
        try:
            if spec.family == 'gemini':
                await self.gemini_images(body)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=spec.timeout)) as session:
                async with session.post(url, json=body, headers=headers, allow_redirects=False) as response:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 4 * 1024 * 1024:
                            raise ValueError('Provider response exceeds 4 MiB.')
                    if response.status != 200:
                        detail = self.bot.features.clean_error(raw.decode(errors='replace'))
                        raise APIStatusError(f'{spec.name} returned HTTP {response.status}: {detail}',
                                             response=httpx.Response(response.status, request=request), body=None)
                    return normalise(spec, json.loads(raw))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise APIConnectionError(request=request, message=f'{spec.name} connection failed ({type(exc).__name__}).') from None
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            raise APIError(self.bot.features.clean_error(exc), request=request, body=None) from None

    def record(self, spec, elapsed, response=None, error=None, messages=None):
        usage = getattr(response, 'usage', None)
        self.db.execute('INSERT INTO provider_requests (at,provider,model,seconds,input_tokens,output_tokens,error) VALUES (?,?,?,?,?,?,?)',
                        (time.time(), spec.id, spec.model, elapsed, getattr(usage, 'prompt_tokens', 0) or 0,
                         getattr(usage, 'completion_tokens', 0) or 0, self.bot.features.clean_error(error) if error else None))
        self.db.execute('DELETE FROM provider_requests WHERE id <= (SELECT max(id)-2000 FROM provider_requests)')
        config = self.bot.features.settings
        if config.diagnostics_enabled and messages:
            preview = self.bot.features.redact(json.dumps(messages))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_context',?)", (preview,))
        self.db.commit()
        if config.diagnostics_enabled or config.raw_generation_logging:
            directory = self.bot.features.data_dir / 'logs'
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = RotatingFileHandler(directory / 'requests.log', maxBytes=10 * 1024 * 1024, backupCount=2)
            try:
                detail = {'provider': spec.id, 'model': spec.model, 'seconds': round(elapsed, 3), 'error': str(error) if error else None}
                if config.raw_generation_logging:
                    detail['input'] = messages
                    detail['output'] = response.model_dump() if response and hasattr(response, 'model_dump') else None
                handler.emit(logging.LogRecord('providers', logging.INFO, '', 0, self.bot.features.redact(json.dumps(detail)), (), None))
            finally:
                handler.close()

    async def send_prefilled(self, spec, key, messages, options):
        if not spec.prefill.strip() or key in self.rejected_prefill:
            async with self.bot.ai_slots:
                return await self.send(spec, messages, options)
        try:
            prefilled = with_prefill(spec, messages, partial=key not in self.rejected_partial)
            async with self.bot.ai_slots:
                response = strip_prefill(spec, await self.send(spec, prefilled, options))
            answer = response.choices[0].message
            if prefilled[-1].get('partial') and not answer.content and not answer.tool_calls:
                # Some providers return empty replies in partial mode; plain prefill still works there.
                self.rejected_partial.add(key)
                async with self.bot.ai_slots:
                    response = strip_prefill(spec, await self.send(spec, with_prefill(spec, messages, partial=False), options))
            return response
        except APIError as exc:
            if getattr(exc, 'status_code', None) not in {400, 422} or not re.search(r'prefill|end with a user message', str(exc), re.I):
                raise
            self.rejected_prefill.add(key)
            self.bot.features.record_status('prefill', {'ok': False, 'error': 'This model rejects prefills, so replies are sent without one.', 'model': spec.model})
            async with self.bot.ai_slots:
                return await self.send(spec, messages, options)

    async def reply(self, messages, *, preferred=None, prefill=False, **options):
        from conversation import text_only
        last_error = None
        for identity in self.order(preferred):
            spec = self.get(identity)
            key = (spec.base_url, spec.model)
            if not prefill:
                spec = spec.model_copy(update={'prefill': ''})
            attempt = messages if spec.vision and key not in self.rejected_vision else text_only(messages)
            started = time.monotonic()
            try:
                try:
                    response = await self.send_prefilled(spec, key, attempt, options)
                except APIError as exc:
                    has_images = any(isinstance(item.get('content'), list) and any(part.get('type') == 'image_url' for part in item['content']) for item in attempt)
                    if getattr(exc, 'status_code', None) not in {400, 422} or not has_images or not re.search(r'image|vision|multimodal', str(exc), re.I):
                        raise
                    self.rejected_vision.add(key)
                    self.bot.features.record_status('vision', {'ok': False, 'error': 'This model rejected images and is text-only for this process.', 'model': spec.model})
                    response = await self.send_prefilled(spec, key, text_only(messages), options)
                self.record(spec, time.monotonic() - started, response=response, messages=attempt)
                return response
            except APIError as exc:
                self.record(spec, time.monotonic() - started, error=exc, messages=attempt)
                last_error = exc
        raise last_error

    async def polish(self, text):
        config = self.bot.features.settings
        if not config.polisher_enabled:
            return text
        instruction = config.post_prompts.get('polisher') or 'Polish only the wording of this dialogue. Preserve its speaker, meaning, facts, URLs and refusals. Return only the revised dialogue.'
        try:
            response = await self.reply([{'role': 'system', 'content': instruction}, {'role': 'user', 'content': text}],
                                        preferred=config.polisher_provider or None, max_tokens=config.polisher_max_tokens)
            answer = response.choices[0].message
            revised = answer.content
            if answer.tool_calls or not revised or not self.bot.social.identity_ok(revised):
                return text
            if any(url not in revised for url in re.findall(r'https?://\S+', text)):
                return text
            return revised
        except APIError as exc:
            self.bot.features.record_status('polisher', {'ok': False, 'error': self.bot.features.clean_error(exc)})
            return text
