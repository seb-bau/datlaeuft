import importlib.util
import unittest
from html.parser import HTMLParser
from pathlib import Path


source_path = Path(__file__).resolve().parents[1] / "app" / "email_layout.py"
module_spec = importlib.util.spec_from_file_location("email_layout", source_path)
email_layout = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(email_layout)


class LayoutParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs))


class EmailLayoutTests(unittest.TestCase):
    def test_default_has_no_logo_or_company_footer(self):
        html = email_layout.render_email_html("Guten Tag,\n\nNachricht", "Betreff", email_layout.DEFAULT_EMAIL_LAYOUT)
        parser = LayoutParser()
        parser.feed(html)
        self.assertNotIn("img", parser.tags)
        self.assertNotIn("footer", parser.tags)
        self.assertNotIn("https://", html)
        self.assertIn("<title>Betreff</title>", html)
        self.assertIn("Guten Tag,<br>\n<br>\nNachricht", html)

    def test_custom_layout_can_add_logo_colors_and_footer(self):
        layout = '<html><body style="color:#123456"><img src="https://example.invalid/logo.png"><main>{{ content }}</main><footer>Unternehmen</footer></body></html>'
        html = email_layout.render_email_html("Nachricht", "Betreff", layout)
        self.assertIn('<main>Nachricht</main>', html)
        self.assertIn('style="color:#123456"', html)
        self.assertIn('<footer>Unternehmen</footer>', html)
        self.assertIn('src="https://example.invalid/logo.png"', html)

    def test_subject_and_untrusted_body_are_escaped_once(self):
        body = '<script>alert("x")</script>\nhttps://example.invalid/?a=1&b="quoted"'
        layout = '<title>{{ subject }}</title><div>{{ content }}</div>'
        html = email_layout.render_email_html(body, '</title><script>subject</script>', layout)
        parser = LayoutParser()
        parser.feed(html)
        self.assertNotIn('script', parser.tags)
        self.assertIn('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;', html)
        self.assertNotIn('&amp;lt;script', html)
        self.assertIn('&lt;/title&gt;&lt;script&gt;subject&lt;/script&gt;', html)
        self.assertEqual(parser.links, [{'href': 'https://example.invalid/?a=1&b="quoted"'}])

    def test_message_is_not_evaluated_as_a_template(self):
        html = email_layout.render_email_html('{{ subject }} {% include "secret" %}', 'Betreff', '{{ content }}')
        self.assertIn('{{ subject }} {% include &quot;secret&quot; %}', html)

    def test_content_is_required_exactly_once(self):
        for layout in ('<body>Leer</body>', '{{ subject }}', '{{ content }}{{ content }}'):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                email_layout.validate_email_layout(layout)

    def test_content_must_be_in_visible_markup(self):
        layouts = [
            '<div data-content="{{ content }}"></div>',
            '<a href={{ content }}>Link</a>',
            '<!-- {{ content }} -->',
            '<head>{{ content }}</head>',
            '<title>{{ content }}</title>',
            '<style>{{ content }}</style>',
            '<script>{{ content }}</script>',
            '<textarea>{{ content }}</textarea>',
            '<template>{{ content }}</template>',
            '<svg>{{ content }}</svg>',
        ]
        for layout in layouts:
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                email_layout.validate_email_layout(layout)

    def test_unknown_fields_and_executable_template_constructs_are_rejected(self):
        layouts = [
            '{{ content }}{{ company_name }}',
            '{{ content|safe }}',
            '{{ content.upper() }}',
            '{{ content.__class__ }}',
            '{% if subject %}{{ content }}{% endif %}',
            '{% for item in content %}{{ item }}{% endfor %}',
            '{% include "layout.html" %}{{ content }}',
            '{% set subject = "Test" %}{{ content }}',
            '{{ content + subject }}',
            '{{ content ',
        ]
        for layout in layouts:
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                email_layout.validate_email_layout(layout)

    def test_empty_invalid_and_oversized_layouts_are_rejected(self):
        for layout in (None, {}, '', '  ', '\x00{{ content }}', '{{ content }}' + 'x' * 50000):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                email_layout.validate_email_layout(layout)

    def test_css_and_normal_whitespace_in_placeholders_are_supported(self):
        layout = '<style>@media(max-width:600px){body{padding:8px}}</style><body>{{  content  }}</body>'
        self.assertEqual(email_layout.validate_email_layout(layout), layout)
        self.assertIn('<body>Nachricht</body>', email_layout.render_email_html('Nachricht', 'Betreff', layout))

    def test_html_output_limit_is_enforced(self):
        with self.assertRaises(ValueError):
            email_layout.render_email_html('&' * 500000, 'Betreff', '{{ content }}')


if __name__ == '__main__':
    unittest.main()
