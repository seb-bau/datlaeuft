from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from .audit import audit
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


def article_decimal(value, label: str, maximum: str, places=4) -> Decimal:
    try:
        result = Decimal(
            str("" if value is None else value).strip().replace(",", ".")
        )
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} ist ungültig.") from exc

    if not result.is_finite() or not Decimal("0") <= result <= Decimal(maximum):
        raise ValueError(f"{label} muss zwischen 0 und {maximum} liegen.")

    rounded = result.quantize(Decimal("1").scaleb(-places))

    if result != rounded:
        raise ValueError(
            f"{label} darf höchstens {places} Nachkommastellen enthalten."
        )

    return rounded


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

    source = request.args.get("source", "local_catalog")

    if source not in {"local_catalog", "material"}:
        return jsonify(error="Die Artikelquelle ist ungültig."), 400

    article_model = (
        LocalCatalogItem if source == "local_catalog" else InventoryItem
    )

    if request.method == "GET":
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
            error="Die Artikelnummer ist bereits vergeben."
        ), 409


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
