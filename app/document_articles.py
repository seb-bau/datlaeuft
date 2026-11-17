from __future__ import annotations

import json
from decimal import Decimal

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import literal, or_, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .audit import audit
from .auth import accounting_required
from .local_catalog import MAX_IMPORT_BYTES, article_decimal, catalog_values, parse_catalog_import, plan_catalog_import
from .extensions import db
from .models import (
    InventoryItem,
    InventoryItemGroup,
    InventoryItemType,
    Invoice,
    LocalCatalogItem,
    Offer,
)


document_articles_bp = Blueprint("document_articles", __name__)


def article_text(name: str, label: str, length: int, required=False) -> str:
    value = request.form.get(name, "").strip()

    if required and not value:
        raise ValueError(f"{label} fehlt.")

    if len(value) > length:
        raise ValueError(
            f"{label} darf höchstens {length} Zeichen enthalten."
        )

    return value


def serialize_article(item) -> dict:
    local = isinstance(item, LocalCatalogItem)
    price = item.price_net if local else item.billing_base_price_net
    rate = item.vat_rate if local else item.billing_vat_rate
    rate = Decimal("19") if rate is None else Decimal(str(rate))

    return {
        "id": item.id,
        "source_type": "local_catalog" if local else "material",
        "item_number": item.item_number,
        "name": item.name,
        "business_area": item.business_area if local else "",
        "product_group": item.product_group if local else (
            item.item_type.group.name if item.item_type else ""
        ),
        "unit": item.unit,
        "price_net": str(price if price is not None else Decimal("0")),
        "active": item.active,
        "tax_label": item.tax_label if local else (
            "Kein Steuersatz" if rate == 0 else "Umsatzsteuer"
        ),
        "vat_rate": str(rate),
    }


@document_articles_bp.route(
    "/admin/document-articles/<document_type>/<int:document_id>",
    methods=["GET", "POST"],
)
@login_required
def items(document_type: str, document_id: int):
    documents = {
        "offer": (Offer, current_user.can_manage_offers),
        "invoice": (Invoice, current_user.can_manage_invoices),
    }

    if document_type not in documents:
        abort(404)

    model, permitted = documents[document_type]

    if not (current_user.is_admin or permitted):
        abort(403)

    document = db.get_or_404(model, document_id)

    if document.status != "draft":
        return jsonify(
            error="Der Beleg ist kein bearbeitbarer Entwurf."
        ), 409

    source = request.args.get("source", "all" if request.method == "GET" else "local_catalog")

    if source not in {"all", "local_catalog", "material"} or source == "all" and request.method != "GET":
        return jsonify(error="Die Artikelquelle ist ungültig."), 400

    article_model = (
        LocalCatalogItem if source == "local_catalog" else InventoryItem
    )

    if request.method == "GET":
        if source == "all":
            search = request.args.get("q", "").strip()[:255]
            statements = []
            for model, kind in ((LocalCatalogItem, "local_catalog"), (InventoryItem, "material")):
                statement = select(model.id.label("item_id"), model.name.label("name"), literal(kind).label("source_type")).where(model.active.is_(True))
                if search:
                    statement = statement.where(or_(model.item_number.ilike(f"%{search}%"), model.name.ilike(f"%{search}%")))
                statements.append(statement)
            combined = union_all(*statements).subquery()
            page = max(1, request.args.get("page", 1, type=int))
            references = db.session.execute(select(combined).order_by(combined.c.name, combined.c.source_type, combined.c.item_id).offset((page - 1) * 50).limit(51)).all()
            catalog_ids = [row.item_id for row in references if row.source_type == "local_catalog"]
            inventory_ids = [row.item_id for row in references if row.source_type == "material"]
            catalog_items = {item.id: item for item in LocalCatalogItem.query.filter(LocalCatalogItem.id.in_(catalog_ids)).all()}
            inventory_items = {item.id: item for item in InventoryItem.query.options(joinedload(InventoryItem.item_type).joinedload(InventoryItemType.group)).filter(InventoryItem.id.in_(inventory_ids)).all()}
            articles = [(catalog_items if row.source_type == "local_catalog" else inventory_items)[row.item_id] for row in references]
            item_types = InventoryItemType.query.join(InventoryItemType.group).filter(InventoryItemType.active.is_(True), InventoryItemGroup.active.is_(True)).order_by(InventoryItemType.name).all()
            return jsonify(items=[serialize_article(item) for item in articles[:50]], has_more=len(articles) > 50,
                           item_types=[{"id": item.id, "name": item.name, "unit": item.unit} for item in item_types])
        query = article_model.query.filter(
            article_model.active.is_(True)
        )
        search = request.args.get("q", "").strip()[:255]

        if search:
            query = query.filter(
                or_(
                    article_model.item_number.ilike(f"%{search}%"),
                    article_model.name.ilike(f"%{search}%"),
                )
            )

        if source == "material":
            query = query.options(
                joinedload(InventoryItem.item_type).joinedload(
                    InventoryItemType.group
                )
            )

        page = max(1, request.args.get("page", 1, type=int))
        articles = (
            query.order_by(article_model.name, article_model.id)
            .offset((page - 1) * 50)
            .limit(51)
            .all()
        )

        item_types = (
            InventoryItemType.query
            .join(InventoryItemType.group)
            .filter(
                InventoryItemType.active.is_(True),
                InventoryItemGroup.active.is_(True),
            )
            .order_by(InventoryItemType.name)
            .all()
        )

        return jsonify(
            items=[
                serialize_article(item)
                for item in articles[:50]
            ],
            has_more=len(articles) > 50,
            item_types=[
                {
                    "id": item.id,
                    "name": item.name,
                    "unit": item.unit,
                }
                for item in item_types
            ],
        )

    try:
        item = article_model(
            item_number=article_text(
                "item_number", "Artikelnummer", 100, True
            ),
            name=article_text(
                "name", "Bezeichnung", 255, True
            ),
            unit=article_text(
                "unit", "Einheit", 40, True
            ),
            active="active" in request.form,
        )

        price = article_decimal(
            request.form.get("price_net"),
            "Nettopreis",
            "9999999999.9999",
        )
        rate = article_decimal(
            request.form.get("vat_rate"),
            "MwSt.",
            "100",
            2,
        )

        if source == "local_catalog":
            item.business_area = article_text(
                "business_area", "Geschäftsbereich", 100
            )
            item.product_group = article_text(
                "product_group", "Warengruppe", 100
            )
            item.tax_label = article_text(
                "tax_label", "Steuersatz", 80, True
            )
            item.price_net = price
            item.vat_rate = rate

        else:
            item.billing_base_price_net = article_decimal(
                price,
                "Nettopreis",
                "99999999.9999",
            )
            item.billing_vat_rate = rate
            raw_type = request.form.get("item_type_id", "").strip()

            if raw_type:
                try:
                    type_id = int(raw_type)
                except ValueError as exc:
                    raise ValueError(
                        "Die Artikelart ist ungültig."
                    ) from exc

                item_type = db.session.get(
                    InventoryItemType,
                    type_id,
                )

                if (
                    not item_type
                    or not item_type.active
                    or not item_type.group.active
                ):
                    raise ValueError(
                        "Die Artikelart ist nicht verfügbar."
                    )

                item.item_type = item_type
                item.unit = item_type.unit

        db.session.add(item)
        db.session.flush()

        audit(
            "article.create",
            item.__tablename__,
            item.id,
        )

        result = serialize_article(item)
        db.session.commit()

        return jsonify(item=result), 201

    except ValueError as exc:
        db.session.rollback()
        return jsonify(error=str(exc)), 400

    except IntegrityError:
        db.session.rollback()
        return jsonify(
            error="Diese Artikelvariante ist bereits vorhanden." if source == "local_catalog" else "Die Artikelnummer ist bereits vergeben."
        ), 409


@document_articles_bp.get("/admin/local-catalog")
@accounting_required
def catalog():
    search = request.args.get("q", "").strip()[:255]
    business_area = request.args.get("business_area", "").strip()[:100]
    state = request.args.get("state", "active")
    if state not in {"active", "inactive", "all"}:
        state = "active"
    query = LocalCatalogItem.query
    if search:
        query = query.filter(or_(LocalCatalogItem.item_number.ilike(f"%{search}%"), LocalCatalogItem.name.ilike(f"%{search}%")))
    if business_area:
        query = query.filter(LocalCatalogItem.business_area == business_area)
    if state != "all":
        query = query.filter(LocalCatalogItem.active.is_(state == "active"))
    page = max(1, request.args.get("page", 1, type=int))
    pagination = query.order_by(LocalCatalogItem.name, LocalCatalogItem.item_number, LocalCatalogItem.id).paginate(page=page, per_page=50, error_out=False)
    areas = [value for value, in db.session.query(LocalCatalogItem.business_area).distinct().order_by(LocalCatalogItem.business_area) if value]
    return render_template("admin/local_catalog.html", pagination=pagination, areas=areas, search=search, business_area=business_area, selected_state=state)


@document_articles_bp.route("/admin/local-catalog/new", methods=["GET", "POST"])
@document_articles_bp.route("/admin/local-catalog/<int:item_id>/edit", methods=["GET", "POST"])
@accounting_required
def catalog_edit(item_id=None):
    item = db.get_or_404(LocalCatalogItem, item_id) if item_id is not None else None
    values = serialize_article(item) if item is not None else {
        "unit": "Stk", "price_net": "0,00", "vat_rate": "19,00", "tax_label": "Normalsteuersatz", "active": True,
    }
    if request.method == "POST":
        values = dict(request.form)
        values["active"] = "active" in request.form
        try:
            changes = catalog_values(values)
            created = item is None
            if created:
                item = LocalCatalogItem()
                db.session.add(item)
            for field, value in changes.items():
                setattr(item, field, value)
            db.session.flush()
            audit("local_catalog.create" if created else "local_catalog.update", "local_catalog_item", item.id)
            db.session.commit()
            flash("Katalogartikel gespeichert.", "success")
            return redirect(url_for("document_articles.catalog"))
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "Diese Artikelvariante ist bereits vorhanden.", "danger")
    return render_template("admin/local_catalog_form.html", item=item if item_id is not None else None, values=values)


@document_articles_bp.post("/admin/local-catalog/<int:item_id>/active")
@accounting_required
def catalog_active(item_id: int):
    item = db.get_or_404(LocalCatalogItem, item_id)
    if request.form.get("active") not in {"true", "false"}:
        abort(400)
    item.active = request.form["active"] == "true"
    audit("local_catalog.active", "local_catalog_item", item.id, {"active": item.active})
    db.session.commit()
    flash("Katalogartikel aktiviert." if item.active else "Katalogartikel deaktiviert.", "success")
    return redirect(url_for("document_articles.catalog", state="all"))


def catalog_import_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="local-catalog-import")


@document_articles_bp.route("/admin/local-catalog/import", methods=["GET", "POST"])
@accounting_required
def catalog_import():
    preview = None
    plan = None
    token = None
    default_unit = request.form.get("default_unit", "Stk")
    if request.method == "POST":
        try:
            action = request.form.get("action", "preview")
            if action == "apply":
                payload = catalog_import_serializer().loads(request.form.get("token", ""), max_age=1800)
                if payload.get("user_id") != current_user.get_id():
                    abort(403)
                plan = plan_catalog_import(payload["rows"])
                for row in plan["rows"]:
                    if row["action"] == "unchanged":
                        continue
                    item = row["item"]
                    if item is None:
                        item = LocalCatalogItem()
                        db.session.add(item)
                    for field, value in row["values"].items():
                        setattr(item, field, value)
                audit("local_catalog.import", "local_catalog_item", "import", {key: plan[key] for key in ("created", "updated", "unchanged")})
                db.session.commit()
                flash(f"Import abgeschlossen: {plan['created']} neue Artikel, {plan['updated']} aktualisiert, {plan['unchanged']} unverändert.", "success")
                return redirect(url_for("document_articles.catalog", state="all"))
            if action != "preview":
                abort(400)
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                raise ValueError("Bitte eine Importdatei auswählen.")
            preview = parse_catalog_import(upload.stream.read(MAX_IMPORT_BYTES + 1), default_unit)
            plan = plan_catalog_import(preview["rows"])
            if not preview["errors"]:
                token = catalog_import_serializer().dumps({"user_id": current_user.get_id(), "rows": preview["rows"]})
        except (BadSignature, SignatureExpired):
            db.session.rollback()
            flash("Die Importvorschau ist ungültig oder abgelaufen. Bitte die Datei erneut prüfen.", "danger")
            plan = None
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "Der Katalog wurde zwischenzeitlich geändert. Bitte die Importvorschau erneut erstellen.", "danger")
            token = None
    return render_template("admin/local_catalog_import.html", preview=preview, plan=plan, token=token, default_unit=default_unit)


def attach_article_fields(rows: list[dict], positions) -> None:
    previous = {
        (
            position.source_type,
            position.inventory_item_id,
            position.local_catalog_item_id,
        )
        for position in positions
    }

    inventory_ids = request.form.getlist(
        "position_inventory_item_id"
    )
    local_ids = request.form.getlist(
        "position_local_catalog_item_id"
    )
    source_values = request.form.getlist(
        "position_source_data"
    )
    article_numbers = request.form.getlist("position_article_number")

    def position_id(values, index):
        raw = values[index].strip() if index < len(values) else ""

        if not raw:
            return None

        try:
            result = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"Artikelverweis der Position {index + 1} ist ungültig."
            ) from exc

        if result <= 0:
            raise ValueError(
                f"Artikelverweis der Position {index + 1} ist ungültig."
            )

        return result

    for index, row in enumerate(rows):
        row["inventory_item_id"] = position_id(
            inventory_ids, index
        )
        row["local_catalog_item_id"] = position_id(
            local_ids, index
        )

        if index < len(source_values):
            try:
                source_data = json.loads(
                    source_values[index] or "{}"
                )
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Artikeldaten der Position {index + 1} sind ungültig."
                ) from exc

            if not isinstance(source_data, dict):
                raise ValueError(
                    f"Artikeldaten der Position {index + 1} sind ungültig."
                )

            row["source_data"] = source_data

        source_data = dict(row.get("source_data") or {})
        if index < len(article_numbers):
            source_data["article_number"] = article_numbers[index].strip()
        if len(str(source_data.get("article_number") or "")) > 120:
            raise ValueError(f"Die Artikelnummer der Position {index + 1} darf höchstens 120 Zeichen enthalten.")
        row["source_data"] = source_data

        source = row["source_type"]

        if source not in {"material", "local_catalog"}:
            row["inventory_item_id"] = None
            row["local_catalog_item_id"] = None
            continue

        local = source == "local_catalog"

        row[
            "inventory_item_id" if local else "local_catalog_item_id"
        ] = None

        row["task_template_id"] = None

        article_id = row[
            "local_catalog_item_id" if local else "inventory_item_id"
        ]

        item = (
            db.session.get(
                LocalCatalogItem if local else InventoryItem,
                article_id,
            )
            if article_id
            else None
        )

        key = (
            source,
            row["inventory_item_id"],
            row["local_catalog_item_id"],
        )

        if item is None or (
            not item.active and key not in previous
        ):
            raise ValueError(
                f"Artikel der Position {index + 1} ist nicht verfügbar."
            )

        source_data = dict(row.get("source_data") or {})
        source_data.setdefault("article_number", item.item_number)

        if local:
            for field in (
                "business_area",
                "product_group",
                "tax_label",
            ):
                source_data.setdefault(field, getattr(item, field))

        row["source_data"] = source_data

