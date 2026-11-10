"""Unusable model responses must retain research context and advance fallbacks."""
import testenv
import asyncio
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import httpx
from openai import APIError, APIStatusError
from providers import Providers, ProviderSpec


async def main():
    db = sqlite3.connect(':memory:')
    providers = Providers(NS(db=db))
    providers.order = Mock(return_value=['primary', 'backup'])
    providers.get = lambda identity: ProviderSpec(id=identity, name=identity, model=identity,
                                                 base_url='https://example.test/v1', vision=True)
    providers.record = Mock()
    messages = [{'role': 'user', 'content': 'Find community links.'},
                {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'search', 'type': 'function', 'function': {'name': 'search', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'search', 'content': 'Verified research results.'}]

    def response(text=None, calls=None, reason='stop'):
        return NS(choices=[NS(finish_reason=reason, message=NS(content=text, tool_calls=calls))])

    good = response('Here are the links and findings.')
    for bad in (response(reason='malformed_function_call'), response('  '), NS(choices=[]),
                response('Partial text', reason='malformed_function_call')):
        providers.record.reset_mock()
        providers.send_prefilled = AsyncMock(side_effect=[bad, good])
        assert await providers.reply(messages, tools=[{'type': 'function'}]) is good
        calls = providers.send_prefilled.await_args_list
        assert [call.args[0].id for call in calls] == ['primary', 'backup']
        assert all(call.args[2] == messages for call in calls)
        assert providers.record.call_args_list[0].kwargs['error']
    for valid in (good, response(calls=[NS(id='next')])):
        providers.send_prefilled = AsyncMock(return_value=valid)
        assert await providers.reply(messages) is valid
        assert providers.send_prefilled.await_count == 1
    providers.send_prefilled = AsyncMock(return_value=response())
    try:
        await providers.reply(messages)
    except APIError as exc:
        assert 'unusable reply' in str(exc)
    else:
        raise AssertionError('An exhausted fallback chain must fail.')
    providers.bot.features = NS(record_status=Mock(), clean_error=str)
    images = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Describe the attached picture.'},
        {'type': 'image_url', 'image_url': {'url': 'https://cdn.discordapp.com/attachments/1/2/photo.png'}}]}]
    for status, error in ((400, 'Failed to download image'), (422, 'Invalid image format'),
                          (400, '图片输入格式/解析错误'), (400, 'Model does not support vision')):
        failure = APIStatusError(error, response=httpx.Response(status, request=httpx.Request('POST', 'https://example.test')), body=None)
        providers.send_prefilled = AsyncMock(side_effect=[failure, good, good])
        providers.record.reset_mock()
        assert await providers.reply(images) is good
        calls = providers.send_prefilled.await_args_list
        assert [call.args[0].id for call in calls] == ['primary', 'backup']
        assert all(call.args[2] == images for call in calls)
        assert providers.record.call_args_list[0].kwargs['error'] is failure
        assert await providers.reply(images) is good
        assert providers.send_prefilled.await_args.args[0].id == 'primary'
        assert providers.send_prefilled.await_args.args[2] == images
    original_get = providers.get
    providers.get = lambda identity: original_get(identity).model_copy(update={'vision': identity == 'backup'})
    providers.send_prefilled = AsyncMock(return_value=good)
    assert await providers.reply(images) is good
    assert providers.send_prefilled.await_count == 1 and providers.send_prefilled.await_args.args[0].id == 'backup'
    providers.get = lambda identity: original_get(identity).model_copy(update={'vision': False})
    providers.send_prefilled.reset_mock()
    try:
        await providers.reply(images)
    except APIError:
        pass
    else:
        raise AssertionError('An image request requires an image-enabled provider.')
    providers.send_prefilled.assert_not_awaited()
    db.close()
    print('Provider fallback checks passed.')


if __name__ == '__main__':
    asyncio.run(main())
