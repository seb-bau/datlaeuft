from jinja2 import TemplateError, meta, nodes
from jinja2.defaults import DEFAULT_FILTERS

from .invoice_cover_letter_layout import LAYOUT_ENV


LAYOUT_LIMIT = 50000
POSITION_FIELDS = (
    "number", "article_number", "description", "details", "quantity_formatted", "unit",
    "unit_price_formatted", "vat_rate_formatted", "total_net_formatted",
)
VAT_FIELDS = ("rate_formatted", "net_formatted", "vat_formatted")
_ADDRESS_FIELDS = {"recipient_address", "seller_address"}
_ROW_FIELDS = {"positions": POSITION_FIELDS, "vat_rows": VAT_FIELDS}
_LIST_FIELDS = _ADDRESS_FIELDS | _ROW_FIELDS.keys()
_ALLOWED_NODES = (
    nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name, nodes.Const,
    nodes.If, nodes.And, nodes.Or, nodes.Not, nodes.Compare, nodes.Operand, nodes.CondExpr,
    nodes.For, nodes.Getattr, nodes.Filter,
)
_LAYOUT_ENV = LAYOUT_ENV.overlay()
_LAYOUT_ENV.filters = {"join": DEFAULT_FILTERS["join"]}


def compile_document_layout(value, *, label, fields, additional_members=()):
    if not isinstance(value, str) or not value.strip() or len(value) > LAYOUT_LIMIT or "\x00" in value:
        raise ValueError(f"Bitte ein {label} mit höchstens 50.000 Zeichen ohne Nullzeichen angeben.")
    list_fields = _LIST_FIELDS.intersection(fields)
    address_fields = _ADDRESS_FIELDS.intersection(fields)
    member_fields = {*POSITION_FIELDS, *VAT_FIELDS, *additional_members}
    try:
        tree = _LAYOUT_ENV.parse(value)
        if any(not isinstance(node, _ALLOWED_NODES) for node in (tree, *tree.find_all(nodes.Node))):
            raise ValueError(f"Im {label} sind HTML, CSS, die aufgeführten Platzhalter, Bedingungen und Schleifen über die Dokumentlisten erlaubt.")
        for loop in tree.find_all(nodes.For):
            if (
                not isinstance(loop.target, nodes.Name) or not isinstance(loop.iter, nodes.Name)
                or loop.iter.name not in list_fields or loop.recursive or loop.test is not None
                or any(loop.find_all(nodes.For))
            ):
                raise ValueError(f"Schleifen im {label} dürfen nur über Adresszeilen, Positionen oder Steuersummen laufen.")
        for expression in tree.find_all(nodes.Filter):
            if (
                expression.name != "join" or not isinstance(expression.node, nodes.Name)
                or expression.node.name not in address_fields
                or len(expression.args) != 1 or not isinstance(expression.args[0], nodes.Const)
                or not isinstance(expression.args[0].value, str)
                or expression.kwargs or expression.dyn_args is not None or expression.dyn_kwargs is not None
            ):
                raise ValueError("Der Filter join ist für die Adresszeilen mit einem festen Trennzeichen verfügbar.")
        unknown = meta.find_undeclared_variables(tree) - fields.keys()
        unknown.update(field.attr for field in tree.find_all(nodes.Getattr) if field.attr not in member_fields)
        if unknown:
            raise ValueError(f"Unbekannte Platzhalter im {label}: " + ", ".join(sorted(unknown)))
        return _LAYOUT_ENV.from_string(value)
    except (TemplateError, RecursionError) as exc:
        raise ValueError(f"Das {label} ist ungültig: {exc}") from exc


def document_layout_values(context, fields):
    values = {name: str(context.get(name) or "") for name in fields if name not in _LIST_FIELDS}
    for name in _ADDRESS_FIELDS.intersection(fields):
        values[name] = tuple(str(line) for line in context.get(name, ()))
    for name, row_fields in _ROW_FIELDS.items():
        values[name] = tuple(
            {field: str(row.get(field) or "") for field in row_fields} for row in context.get(name, ())
        )
    values["preview"] = bool(context.get("preview"))
    values["show_article_numbers"] = any(position["article_number"] for position in values["positions"])
    return values


def render_document_layout(template, values, *, label):
    remaining = 2000000
    chunks = []
    try:
        for chunk in template.generate(values):
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError(f"Das erzeugte HTML für das {label} ist zu lang. Bitte das Layout kürzen.")
            chunks.append(chunk)
    except (TemplateError, TypeError, RecursionError) as exc:
        raise ValueError(f"Das {label} ist ungültig: {exc}") from exc
    return "".join(chunks)


def document_layout_url_fetcher(url):
    from weasyprint import default_url_fetcher

    if not url.casefold().startswith("data:"):
        raise ValueError("Grafiken und Schriften im Layout müssen als Daten-URL eingebunden sein.")
    return default_url_fetcher(url)
