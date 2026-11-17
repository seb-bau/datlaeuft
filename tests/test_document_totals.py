import ast
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest


class DocumentTotalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        models = ast.parse((cls.root / "app/models.py").read_text())
        enums = [node for node in models.body if isinstance(node, ast.ClassDef) and node.name in {
            "OfferStatus", "OfferPositionSource", "InvoicePositionSource",
        }]
        namespace = {"Enum": Enum}
        exec(compile(ast.Module(body=enums, type_ignores=[]), "models.py", "exec"), namespace)
        namespace.update(
            Any=object, Decimal=Decimal, InvalidOperation=InvalidOperation, ROUND_HALF_UP=ROUND_HALF_UP,
            MONEY_QUANTIZER=Decimal("0.01"), Offer=object, Invoice=object,
            OfferPosition=SimpleNamespace, InvoicePosition=SimpleNamespace,
            TimeTaskTemplate=object, db=SimpleNamespace(session=SimpleNamespace(flush=lambda: None)),
        )
        names = {
            "offer_service.py": {"replace_offer_positions", "recalculate_offer", "money_value", "decimal_value", "optional_int"},
            "invoice_service.py": {"replace_invoice_positions", "append_invoice_position", "recalculate_invoice", "as_decimal", "money"},
            "admin.py": {"optional_form_decimal"},
        }
        for filename, selected in names.items():
            source = ast.parse((cls.root / "app" / filename).read_text())
            functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in selected]
            exec(compile(ast.Module(body=functions, type_ignores=[]), filename, "exec"), namespace)
        cls.backend = namespace

    def test_live_totals_match_both_backend_calculations(self):
        cases = [
            [("2", "10", "19")],
            [("2,5", "4,995", "19,00")],
            [("1", "0.03", "19"), ("1", "0.03", "19")],
            [("1", "1.005", "19")],
            [("1", "0.005", "100")],
            [("3", "0.29", "19")],
            [("3.3333", "0.3333", "19")],
            [("1", "100", "0"), ("1", "100", "7"), ("1", "100", "19")],
            [("1", "0", "19")],
            [("1 000", "0,10", "19")],
            [("1e2", "1e-1", "19")],
        ]
        payload = [[dict(quantity=quantity, unitPriceNet=price, vatRate=rate) for quantity, price, rate in rows] for rows in cases]
        script = "const fs=require('node:fs'); const {calculateTotals}=require('./app/static/js/document_totals.js'); const rows=JSON.parse(fs.readFileSync(0,'utf8')); process.stdout.write(JSON.stringify(rows.map(calculateTotals),(_,value)=>typeof value==='bigint'?value.toString():value));"
        result = subprocess.run(["node", "-e", script], input=json.dumps(payload), cwd=self.root, text=True, capture_output=True, check=True)
        browser_totals = json.loads(result.stdout)
        for index, values in enumerate(cases):
            rows = [{"source_type": "free", "description": "Testposition", "unit": "Stk",
                     "quantity": self.backend["optional_form_decimal"](quantity, "Menge"),
                     "unit_price_net": self.backend["optional_form_decimal"](price, "Einzelpreis"),
                     "vat_rate": self.backend["optional_form_decimal"](rate, "Umsatzsteuer")} for quantity, price, rate in values]
            for kind in ("offer", "invoice"):
                with self.subTest(kind=kind, values=values):
                    document = SimpleNamespace(id=None, status="draft", positions=[])
                    self.backend[f"replace_{kind}_positions"](document, rows)
                    expected = {field: str(int(getattr(document, f"total_{field}") * 100)) for field in ("net", "vat", "gross")}
                    self.assertEqual(browser_totals[index], expected)

    def test_both_editors_connect_totals_to_their_positions_and_only_load_the_script_when_editable(self):
        from jinja2 import Environment
        for kind in ("offer", "invoice"):
            source = (self.root / f"app/templates/admin/{kind}_draft.html").read_text()
            Environment().parse(source)
            self.assertIn(f'data-positions-target="{kind}Positions"', source)
            for field in ("net", "vat", "gross"):
                self.assertEqual(source.count(f'data-document-total="{field}"'), 1)
            self.assertIn('aria-live="polite"', source)
            script = '<script src="{{ url_for(\'static\', filename=\'js/document_totals.js\') }}" defer></script>'
            self.assertEqual(source.count(script), 1)
            start = source.rfind("{% if editable %}", 0, source.index(script))
            end = source.index("{% endif %}", start)
            self.assertLess(source.index(script), end)
