"""SillyTavern PNG and JSON imports."""
import testenv
import base64
import io
import json
import unittest

from PIL import Image, PngImagePlugin
from characters import import_card, parse_character


class CardImportTests(unittest.TestCase):
    def test_formats_and_prompt_boundaries(self):
        data = {'name': 'Rowan', 'description': '{{char}} tends a garden with {{user}}.',
                'personality': 'Patient.', 'mes_example': '{{CHAR}}: Hello.',
                'first_mes': 'GREETING_ONLY', 'system_prompt': 'INSTRUCTIONS_ONLY',
                'character_book': {'entries': [{'content': 'LORE_ONLY'}]}}
        for spec in (None, 'chara_card_v2', 'chara_card_v3'):
            card = {'spec': spec, 'data': data} if spec else data
            contents = [json.dumps(card).encode()]
            for key in ('chara', 'ccv3'):
                output = io.BytesIO()
                metadata = PngImagePlugin.PngInfo()
                metadata.add_text(key, base64.b64encode(contents[0]).decode())
                Image.new('RGB', (1, 1)).save(output, format='PNG', pnginfo=metadata)
                contents.append(output.getvalue())
            for content in contents:
                result = import_card(content)
                parsed = parse_character(result['markdown'])
                self.assertIn('Rowan tends a garden with you.', parsed['persona'])
                self.assertIn('Patient.', parsed['persona'])
                self.assertEqual(parsed['examples'], 'Rowan: Hello.')
                for text in ('GREETING_ONLY', 'INSTRUCTIONS_ONLY', 'LORE_ONLY'):
                    self.assertIn(text, result['markdown'])
                    self.assertNotIn(text, str(parsed))

    def test_embedded_headings_and_fences(self):
        card = {'name': 'Rowan', 'description': '## User Context\nStill persona.\n```\nUnclosed', 'mes_example': 'Hello.'}
        parsed = parse_character(import_card(json.dumps(card).encode())['markdown'])
        self.assertIn('Still persona.', parsed['persona'])
        self.assertEqual(parsed['examples'], 'Hello.')
        self.assertEqual(parsed['ignored_sections'], ['card details'])

    def test_invalid_cards(self):
        for content in (b'', b'not json', b'[]', b'{}', b'\x89PNG\r\n\x1a\n', b'x' * (15 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                import_card(content)
        output = io.BytesIO()
        Image.new('RGB', (1, 1)).save(output, format='PNG')
        with self.assertRaisesRegex(ValueError, 'no SillyTavern'):
            import_card(output.getvalue())


if __name__ == '__main__':
    unittest.main()
