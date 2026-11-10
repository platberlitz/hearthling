"""Image corrections retain the real source through Discord reply chains."""
import asyncio
import base64
import json
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import testenv
import discord

from hearthling import Hearthling, open_database
from conversation import image_sources, message_content
from features import FeatureAction
from test_features import PNG, http_session, request


async def main():
    with tempfile.TemporaryDirectory() as directory:
        db = open_database(':memory:')
        async with Hearthling(None, db, data_dir=directory, credentials={
                'image_api_key': 'offline-test', 'image_base_url': 'https://example.invalid/v1',
                'image_family': 'responses'}) as bot:
            current = request()
            current.content = 'I said fully clothed. Keep the original pose.'
            current.channel.permissions_for = MagicMock(return_value=discord.Permissions(
                view_channel=True, read_message_history=True))

            def message(identity, author, content='', image=None):
                result = MagicMock(spec=discord.Message)
                result.id, result.author, result.content = identity, author, content
                result.channel = current.channel
                result.reference = None
                result.attachments = ([NS(content_type='image/png', size=len(PNG), filename='source.png', url=image)]
                                      if image else [])
                result.embeds, result.stickers = [], []
                return result

            def reply(child, parent):
                child.reference = discord.MessageReference(message_id=parent.id, channel_id=parent.channel.id)
                child.reference.resolved = parent

            url = 'https://cdn.discordapp.com/attachments/111/222/source.png'
            source = message(current.id - 3, NS(id=current.author.id + 1, bot=False, display_name='Member'), image=url)
            original = message(current.id - 2, current.author, 'Draw my character in this bedroom, fully clothed.')
            answer = message(current.id - 1, NS(id=bot.bot_id, bot=True, display_name='Assistant'),
                             'PROVIDER_REJECTION_FROM_PREVIOUS_ATTEMPT')
            reply(original, source)
            reply(answer, original)
            reply(current, answer)
            assert await image_sources(bot, current) == [current, answer, original, source]
            payload = json.loads(await message_content(bot, current))
            assert payload['images'][0]['url'] == url
            assert original.content in json.dumps(payload['image_reference_context'])
            assert answer.content not in json.dumps(payload['image_reference_context'])

            # The bytes must reach the actual edit request, not just the chat model.
            args = FeatureAction(action='generate_image', prompt='A fully clothed adult in the original bedroom pose.')
            response = {'output': [{'type': 'image_generation_call', 'result': base64.b64encode(PNG).decode()}]}
            session = http_session(response)
            with patch('features.download_image', new=AsyncMock(return_value=PNG)) as download, \
                    patch('features.aiohttp.ClientSession', return_value=session):
                result = await bot.features.generate_image(args, current)
            assert result['bytes'] == len(PNG)
            download.assert_awaited_once_with(url, maximum=10 * 1024 * 1024)
            sent = session.__aenter__.return_value.post.call_args.kwargs['json']
            assert sent['tools'][0]['action'] == 'edit'
            assert any(part.get('image_url', '').startswith('data:image/png;base64,')
                       for part in sent['input'][0]['content'])
            prompt = sent['input'][0]['content'][0]['text']
            assert current.content in prompt and original.content in prompt
            assert answer.content not in prompt

            # Refresh an uncached Discord message through the same permission checks.
            answer.reference.resolved = None
            current.channel.fetch_message = AsyncMock(return_value=original)
            assert (await image_sources(bot, current))[-1] is source
            current.channel.fetch_message.assert_awaited_once_with(original.id)
            answer.reference.resolved = original
            assert all(call.args[0] is current.author for call in current.channel.permissions_for.call_args_list)

            # A fresh explicit image wins; another member's conversation is not followed.
            current.attachments = [NS(content_type='image/png', size=10, filename='new.png', url=url + '?new')]
            assert await image_sources(bot, current) == [current, answer]
            current.attachments = []
            answer.author = source.author
            assert await image_sources(bot, current) == [current, answer]
            answer.author = NS(id=bot.bot_id, bot=True)

            # Clearing history and losing channel access must stop implicit recovery.
            with patch.object(bot.characters, 'history_after', return_value=original.id):
                assert await image_sources(bot, current) == [current, answer]
            current.channel.permissions_for.return_value.read_message_history = False
            assert await image_sources(bot, current) == [current]
            current.channel.permissions_for.return_value.read_message_history = True
            original.reference.channel_id += 1
            assert await image_sources(bot, current) == [current, answer, original]
            original.reference.channel_id = current.channel.id
            reply(original, answer)
            assert await image_sources(bot, current) == [current, answer, original]
        db.close()
    print('PASS: image follow-up source bytes, edit instructions, uncached replies, new-source priority, permissions, history clearing and cycles.')


if __name__ == '__main__':
    asyncio.run(main())
