import importlib.util
from pathlib import Path
import unittest


path = Path(__file__).resolve().parents[1] / "app" / "invoice_cover_letter_layout.py"
spec = importlib.util.spec_from_file_location("invoice_cover_letter_layout", path)
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)


class InvoiceCoverLetterLayoutTests(unittest.TestCase):
    def test_default_omits_the_sender_line_and_keeps_optional_contact_fields(self):
        html = layout.render_cover_letter_html({
            "title": "Mieterbelastung", "text": "Individueller Text", "recipient_address": "Empfänger\nMusterweg 1",
            "sender_line": "ABSENDER AUS DEM GESCHÄFTSPAPIER", "caseworker_name": "Sachbearbeitung", "preview": True,
        }, layout.DEFAULT_COVER_LETTER_LAYOUT)
        self.assertNotIn("ABSENDER AUS DEM GESCHÄFTSPAPIER", html)
        self.assertIn("Empfänger\nMusterweg 1", html)
        self.assertIn("Sachbearbeitung", html)
        self.assertNotIn("<strong>Telefon:</strong>", html)
        self.assertIn("ENTWURF", html)

    def test_custom_html_css_can_reorder_remove_and_restore_elements(self):
        template = '<style>@page {margin: 50mm 20mm} .address {position:absolute;top:10mm}</style><h1>{{ title }}</h1><div>{{ text }}</div><aside class="address">{{ recipient_address }}</aside><footer>{{ sender_line }}</footer>'
        html = layout.render_cover_letter_html({
            "title": "Überschrift", "text": "Text", "recipient_address": "Adresse", "sender_line": "Absender",
            "caseworker_name": "ENTFERNTER KONTAKTBLOCK",
        }, template)
        self.assertLess(html.index("<h1>"), html.index('<aside'))
        self.assertIn("position:absolute;top:10mm", html)
        self.assertIn("<footer>Absender</footer>", html)
        self.assertNotIn("ENTFERNTER KONTAKTBLOCK", html)

    def test_fields_are_escaped_once_and_are_not_evaluated_again(self):
        html = layout.render_cover_letter_html({"title": '<script>Test</script>', "text": '{{ config }} & "Text"'}, '{{ title }}<main>{{ text }}</main>')
        self.assertIn("&lt;script&gt;Test&lt;/script&gt;", html)
        self.assertNotIn("&amp;lt;", html)
        self.assertIn("{{ config }} &amp; &#34;Text&#34;", html)

    def test_simple_conditions_and_all_document_fields_are_available(self):
        template = '{% if caseworker_phone and not preview %}Telefon: {{ caseworker_phone }}{% else %}Kein Telefon{% endif %}'
        self.assertEqual(layout.render_cover_letter_html({"caseworker_phone": "123", "preview": False}, template), "Telefon: 123")
        self.assertEqual(layout.render_cover_letter_html({"caseworker_phone": "123", "preview": True}, template), "Kein Telefon")
        for name in layout.COVER_LETTER_LAYOUT_FIELDS:
            self.assertTrue(layout.render_cover_letter_html({name: True if name == "preview" else "Beispiel"}, "{{ " + name + " }}"))

    def test_executable_constructs_and_unknown_fields_are_rejected(self):
        for template in (
            '{{ config }}', '{{ text.__class__ }}', '{{ text.upper() }}', '{{ text|safe }}',
            '{% include "admin/settings.html" %}', '{% extends "base.html" %}',
            '{% for item in text %}{{ item }}{% endfor %}', '{% set title = "Test" %}',
            '{{ 10 ** 100000 }}', '{{ text[0] }}',
        ):
            with self.subTest(template=template), self.assertRaises(ValueError):
                layout.validate_cover_letter_layout(template)

    def test_invalid_and_oversized_layouts_are_rejected(self):
        for template in (None, {}, "", " ", "\x00", "{{ text", "x" * 50001):
            with self.subTest(template=template), self.assertRaises(ValueError):
                layout.validate_cover_letter_layout(template)

    def test_full_control_also_allows_static_layouts(self):
        self.assertEqual(layout.render_cover_letter_html({}, "<p>Eigener Aufbau</p>"), "<p>Eigener Aufbau</p>")

    def test_expanded_document_length_is_bounded(self):
        with self.assertRaises(ValueError):
            layout.render_cover_letter_html({"text": "x" * 2000}, "{{ text }}" * 1100)
