from __future__ import annotations

from sqlalchemy import func

from .extensions import db
from .models import (
    InventoryItem,
    InventoryLocation,
    InventoryMovement,
    InventoryMovementType,
    InventoryStock,
    TimeTaskExpectedMaterial,
)


def get_default_inventory_location() -> InventoryLocation:
    location = (
        InventoryLocation.query
        .filter(
            InventoryLocation.active.is_(True),
            InventoryLocation.is_default.is_(True),
        )
        .order_by(InventoryLocation.id.asc())
        .first()
    )

    if location is None:
        location = (
            InventoryLocation.query
            .filter(InventoryLocation.active.is_(True))
            .order_by(InventoryLocation.name.asc())
            .first()
        )

    if location is None:
        raise ValueError("Es ist kein aktiver Lagerort vorhanden.")

    return location


def get_inventory_location(location_id: int) -> InventoryLocation:
    location = db.session.get(InventoryLocation, location_id)

    if location is None or not location.active:
        raise ValueError("Der ausgewählte Lagerort ist nicht verfügbar.")

    return location


def get_inventory_stock(
    item: InventoryItem,
    location: InventoryLocation,
) -> InventoryStock:
    stock = (
        InventoryStock.query
        .filter_by(
            item_id=item.id,
            location_id=location.id,
        )
        .with_for_update()
        .one_or_none()
    )

    if stock is None:
        stock = InventoryStock(
            item=item,
            location=location,
            quantity=0,
        )
        db.session.add(stock)

    return stock


def refresh_inventory_total(item: InventoryItem) -> None:
    db.session.flush()

    total = (
        db.session.query(
            func.coalesce(
                func.sum(InventoryStock.quantity),
                0,
            )
        )
        .filter(InventoryStock.item_id == item.id)
        .scalar()
    )

    item.stock_quantity = float(total or 0)


def change_inventory_stock(
    stock: InventoryStock,
    quantity: float,
) -> None:
    new_quantity = stock.quantity + quantity

    if new_quantity < -0.0001:
        raise ValueError(
            f"Am Lagerort „{stock.location.name}“ ist nicht genügend Bestand vorhanden."
        )

    stock.quantity = max(new_quantity, 0)


def prepare_inventory_movement(
    movement: InventoryMovement,
) -> InventoryMovement:
    item = movement.item

    if item is None:
        raise ValueError(
            "Für die Buchung fehlt der Artikel."
        )

    item_type = item.item_type
    item_group = (
        item_type.group
        if item_type
        else None
    )

    if movement.item_type_id_snapshot is None:
        movement.item_type_id_snapshot = (
            item_type.id
            if item_type
            else None
        )

    if movement.item_group_id_snapshot is None:
        movement.item_group_id_snapshot = (
            item_group.id
            if item_group
            else None
        )

    serial_number = str(
        movement.serial_number or ""
    ).strip()
    movement.serial_number = (
        serial_number
        or None
    )

    if (
        movement.movement_type
        == InventoryMovementType.WITHDRAWAL.value
        and item.serial_number_required
    ):
        if abs(float(movement.quantity) - 1.0) > 0.0001:
            raise ValueError(
                "Seriennummernpflichtige Artikel "
                "müssen einzeln mit Menge 1 gebucht werden."
            )

        if movement.serial_number is None:
            raise ValueError(
                "Für diesen Artikel muss eine "
                "Seriennummer angegeben werden."
            )

    return movement


def movement_item_type_id(
    movement: InventoryMovement,
) -> int | None:
    if movement.item_type_id_snapshot is not None:
        return movement.item_type_id_snapshot

    if movement.item is None:
        return None

    return movement.item.item_type_id


def movement_matches_expected_material(
    movement: InventoryMovement,
    expected: TimeTaskExpectedMaterial,
) -> bool:
    if expected.item_id is not None:
        return movement.item_id == expected.item_id

    return (
        expected.item_type_id is not None
        and movement_item_type_id(movement)
        == expected.item_type_id
    )


def book_inventory_movement(
    movement: InventoryMovement,
) -> InventoryMovement:
    movement_type = movement.movement_type
    quantity = float(movement.quantity)

    if movement_type == InventoryMovementType.CORRECTION.value:
        if abs(quantity) < 0.0001:
            raise ValueError("Die Korrekturmenge darf nicht 0 sein.")
    elif quantity <= 0:
        raise ValueError("Die Buchungsmenge muss größer als 0 sein.")

    prepare_inventory_movement(
        movement
    )

    db.session.add(movement)

    if movement_type == InventoryMovementType.WITHDRAWAL.value:
        location = (
            movement.source_location
            or get_default_inventory_location()
        )
        movement.source_location = location

        stock = get_inventory_stock(
            movement.item,
            location,
        )
        change_inventory_stock(stock, -quantity)

    elif movement_type == InventoryMovementType.STORED.value:
        location = (
            movement.target_location
            or get_default_inventory_location()
        )
        movement.target_location = location

        stock = get_inventory_stock(
            movement.item,
            location,
        )
        change_inventory_stock(stock, quantity)

    elif movement_type == InventoryMovementType.CORRECTION.value:
        location = (
            movement.target_location
            or get_default_inventory_location()
        )
        movement.target_location = location

        stock = get_inventory_stock(
            movement.item,
            location,
        )
        change_inventory_stock(stock, quantity)

    elif movement_type == InventoryMovementType.CANCELLATION.value:
        location = (
            movement.target_location
            or get_default_inventory_location()
        )
        movement.target_location = location

        stock = get_inventory_stock(
            movement.item,
            location,
        )
        change_inventory_stock(stock, quantity)

    elif movement_type == InventoryMovementType.TRANSFER.value:
        if movement.source_location is None:
            raise ValueError("Der Quell-Lagerort fehlt.")

        if movement.target_location is None:
            raise ValueError("Der Ziel-Lagerort fehlt.")

        if movement.source_location.id == movement.target_location.id:
            raise ValueError(
                "Quell- und Ziel-Lagerort müssen unterschiedlich sein."
            )

        source_stock = get_inventory_stock(
            movement.item,
            movement.source_location,
        )
        target_stock = get_inventory_stock(
            movement.item,
            movement.target_location,
        )

        change_inventory_stock(source_stock, -quantity)
        change_inventory_stock(target_stock, quantity)

    elif movement_type == InventoryMovementType.ORDERED.value:
        movement.target_location = (
            movement.target_location
            or get_default_inventory_location()
        )

    else:
        raise ValueError("Die Buchungsart wird nicht unterstützt.")

    refresh_inventory_total(movement.item)

    return movement
