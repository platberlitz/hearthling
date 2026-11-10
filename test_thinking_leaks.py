"""Thinking accidentally returned as dialogue must never reach callers or tools."""
import testenv
import asyncio
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from openai import APIError
from openai.types.chat import ChatCompletion

from providers import Providers, ProviderSpec


def response(text, *, calls=None, reasoning=None):
    return ChatCompletion.model_validate({
        'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'test',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls' if calls else 'stop',
                     'message': {'role': 'assistant', 'content': text, 'tool_calls': calls,
                                 'reasoning_content': reasoning}}]})


async def main():
    db = sqlite3.connect(':memory:')
    providers = Providers(NS(db=db))
    providers.order = Mock(return_value=['primary', 'backup'])
    providers.get = lambda identity: ProviderSpec(id=identity, name=identity, model=identity,
                                                 base_url='https://example.test/v1', vision=True)
    providers.record = Mock()
    call = {'id': 'picture', 'type': 'function',
            'function': {'name': 'generate_image', 'arguments': '{}'}}
    messages = [{'role': 'user', 'content': 'Draw a cat.'},
                {'role': 'assistant', 'content': None, 'tool_calls': [call]},
                {'role': 'tool', 'tool_call_id': 'picture', 'content': 'Picture already posted.'}]
    good = response('Your picture is ready.')
    for text in ('hthought\nPlan the reply.Here is your picture.',
                 '  hthought\r\nHere is your picture.',
                 'Draft\nHTHOUGHT\nPrivate planning', 'hthought'):
        for calls in (None, [call]):
            providers.record.reset_mock()
            providers.send_prefilled = AsyncMock(side_effect=[response(text, calls=calls), good])
            assert await providers.reply(messages, tools=[{'type': 'function'}]) is good
            attempts = providers.send_prefilled.await_args_list
            assert [item.args[0].id for item in attempts] == ['primary', 'backup']
            assert all(item.args[2] == messages for item in attempts)
            assert all(item.args[3] == {'tools': [{'type': 'function'}]} for item in attempts)
            assert 'leaked thinking marker' in str(providers.record.call_args_list[0].kwargs['error'])
    for valid in (response('I thought you might like this.'),
                  response('The hthought marker is an upstream bug.'),
                  response('Public dialogue.', reasoning='hthought\nPrivate reasoning.'),
                  response(None, calls=[call])):
        providers.send_prefilled = AsyncMock(return_value=valid)
        assert await providers.reply(messages) is valid
        assert providers.send_prefilled.await_count == 1
    providers.send_prefilled = AsyncMock(side_effect=[response('hthought\nPRIVATE') for _ in range(2)])
    try:
        await providers.reply(messages)
    except APIError as exc:
        assert 'leaked thinking marker' in str(exc) and 'PRIVATE' not in str(exc)
    else:
        raise AssertionError('Leaking backups must fail without returning their text.')
    db.close()
    print('Thinking leak checks passed.')


if __name__ == '__main__':
    asyncio.run(main())
