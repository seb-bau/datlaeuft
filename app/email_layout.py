from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

from jinja2 import StrictUndefined, TemplateError, meta, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment
from markupsafe import Markup


DEFAULT_EMAIL_LAYOUT = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ subject }}</title>
</head>
<body style="font-family:Arial,sans-serif;line-height:1.5">
{{ content }}
</body>
</html>"""

EMAIL_LAYOUT_FIELDS = {
    "subject": "Betreff der Nachricht; wird für HTML maskiert",
    "content": "Vollständiger Mailtext mit Zeilenumbrüchen und anklickbaren Links; muss genau einmal unverändert im sichtbaren Inhalt stehen",
}


HTML_TEMPLATE_ENV = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=True)
HTML_TEMPLATE_ENV.globals.clear()
HTML_TEMPLATE_ENV.filters.clear()


class EmailLayoutContentParser(HTMLParser):
    hidden_tags = {
        "head", "title", "script", "style", "textarea", "template", "noscript",
        "iframe", "xmp", "plaintext", "select", "option", "svg", "math",
    }

    def __init__(self, marker):
        super().__init__()
        self.marker = marker
        self.hidden = []
        self.content_count = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden.remove(tag)

    def handle_data(self, data):
        if not self.hidden:
            self.content_count += data.count(self.marker)


def compile_email_layout(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 50000 or "\x00" in text:
        raise ValueError("Bitte ein HTML-Layout mit höchstens 50.000 Zeichen ohne Nullzeichen angeben.")
    try:
        tree = HTML_TEMPLATE_ENV.parse(text)
        allowed = (nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name)
        if any(not isinstance(node, allowed) for node in (tree, *tree.find_all(nodes.Node))):
            raise ValueError("Im HTML-Layout sind nur HTML und die Platzhalter subject und content erlaubt.")
        unknown = meta.find_undeclared_variables(tree) - set(EMAIL_LAYOUT_FIELDS)
        if unknown:
            raise ValueError("Unbekannte Platzhalter: " + ", ".join(sorted(unknown)))
        if sum(node.name == "content" for node in tree.find_all(nodes.Name)) != 1:
            raise ValueError("Der Platzhalter {{ content }} muss genau einmal im HTML-Layout stehen.")
        template = HTML_TEMPLATE_ENV.from_string(text)
        marker = "__EMAIL_LAYOUT_CONTENT__"
        parser = EmailLayoutContentParser(marker)
        parser.feed(template.render(subject="Beispiel", content=marker))
        parser.close()
        if parser.content_count != 1:
            raise ValueError("Der Platzhalter {{ content }} muss im sichtbaren HTML-Inhalt stehen, nicht in Attributen oder im Kopfbereich.")
        return template
    except TemplateError as exc:
        raise ValueError(f"Das HTML-Layout ist ungültig: {exc}") from exc


def validate_email_layout(text):
    compile_email_layout(text)
    return text


def render_email_html(body, subject, layout):
    parts = []
    offset = 0
    for match in re.finditer(r"https?://[^\s<>]+", body):
        parts.append(escape(body[offset:match.start()]))
        url = escape(match.group(), quote=True)
        parts.append(f'<a href="{url}">{url}</a>')
        offset = match.end()
    parts.append(escape(body[offset:]))
    content = "".join(parts).replace("\n", "<br>\n")
    template = compile_email_layout(layout)
    html = template.render(subject=subject, content=Markup(content))
    if len(html) > 2000000:
        raise ValueError("Die erzeugte HTML-Mail ist zu lang.")
    return html
