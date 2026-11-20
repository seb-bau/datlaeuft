from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from flask import current_app

from .extensions import db
from .models import (
    CraftActivity,
    User,
    UserJurisdictionPersonMapping,
    WowiEconomicUnit,
    WowiEconomicUnitJurisdiction,
)
from .settings import SettingKeys, settings
from .wowiport import WowiportService
from .blacklist import include_blacklisted


EMAIL_PATTERN = re.compile(
    r"^[^@\s]+@[^@\s]+$"
)
DOMAIN_SEPARATOR_PATTERN = re.compile(
    r"[,;\s]+"
)


@dataclass
class UserSyncResult:
    seen: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_domain: int = 0
    without_explicit_craft_activity: int = 0
    skipped_protected: int = 0
    skipped_excluded: int = 0
    skipped_conflict: int = 0
    configuration_missing: int = 0


@include_blacklisted()
def sync_users_from_wowiport(
    service: WowiportService | None = None,
    craftsman_rows: list[Any] | None = None,
) -> UserSyncResult:
    service = service or WowiportService()
    result = UserSyncResult()
    allowed_domains = (
        _configured_email_domains()
    )

    if not allowed_domains:
        result.configuration_missing = 1
        return result

    craft_activities_by_erp_id = {
        item.erp_id: item
        for item in CraftActivity.query.filter(
            CraftActivity.erp_id.isnot(None),
        ).all()
    }

    responsible_person_candidates: (
        dict[str, dict[int, str]] | None
    ) = None

    rows = (
        craftsman_rows
        if craftsman_rows is not None
        else service.get_craftsmen_for_user_sync()
    )

    for row in rows:
        result.seen += 1

        craftsman_erp_id = _as_int(
            _value(
                row,
                "Id",
                "id",
                "id_",
            )
        )

        if craftsman_erp_id is None:
            result.skipped_conflict += 1
            continue

        email = extract_craftsman_email(row, allowed_domains)

        if email is None:
            result.skipped_domain += 1
            continue

        craft_activity_ids = (
            extract_craft_activity_ids(row)
        )

        if not craft_activity_ids:
            result.without_explicit_craft_activity += 1

        craft_activities = [
            craft_activities_by_erp_id[erp_id]
            for erp_id in sorted(
                craft_activity_ids
            )
            if (
                    erp_id
                    in craft_activities_by_erp_id
            )
        ]

        craftsman_matches = (
            User.query
            .filter_by(
                craftsman_erp_id=(
                    craftsman_erp_id
                )
            )
            .order_by(User.id.asc())
            .all()
        )

        if len(craftsman_matches) > 1:
            result.skipped_conflict += 1
            continue

        user = (
            craftsman_matches[0]
            if craftsman_matches
            else None
        )
        email_match = (
            User.query
            .filter_by(email=email)
            .one_or_none()
        )

        if user is None:
            user = email_match
        elif (
            email_match is not None
            and email_match.id != user.id
        ):
            result.skipped_conflict += 1
            continue

        if (
                user is not None
                and not user.wowiport_managed
                and not user.entra_oid
        ):
            result.skipped_protected += 1
            continue

        if (
            user is not None
            and user.exclude_from_wowiport_sync
        ):
            result.skipped_excluded += 1
            continue

        created = user is None

        if created:
            user = User(
                email=email,
                display_name=email,
                wowiport_managed=True,
                exclude_from_wowiport_sync=False,
                is_active_flag=True,
            )
            db.session.add(user)

        identity_managed_by_entra = bool(
            user.entra_oid
        )

        previous_values = (
            user.email,
            user.display_name,
            user.first_name,
            user.last_name,
            user.craftsman_erp_id,
            user.person_erp_id,
            user.creditor_erp_id,
            user.creditor_number,
            user.craftsman_portal_id,
            user.is_active_flag,
            frozenset(
                item.id
                for item
                in user.craft_activities
            ),
        )

        person = _value(
            row,
            "Person",
            "person",
        )

        if not identity_managed_by_entra:
            user.email = email
            user.display_name = extract_craftsman_display_name(
                row,
                email,
            )
            user.wowiport_managed = True
            user.is_active_flag = True

        natural_person = _value(person, "NaturalPerson", "natural_person")
        if natural_person is not None:
            for attribute, keys in (
                ("first_name", ("FirstName", "first_name")),
                ("last_name", ("LastName", "last_name")),
            ):
                value = _text_or_none(_value(natural_person, *keys))
                if value is not None and (not identity_managed_by_entra or not getattr(user, attribute)):
                    setattr(user, attribute, value)

        user.craftsman_erp_id = (
            craftsman_erp_id
        )
        user.person_erp_id = _as_int(
            _value(
                row,
                "PersonId",
                "person_id",
            )
            or _value(
                person,
                "Id",
                "id",
                "id_",
            )
        )
        user.creditor_erp_id = _as_int(
            _value(
                row,
                "CreditorId",
                "creditor_id",
            )
        )
        user.creditor_number = (
            _text_or_none(
                _value(
                    row,
                    "CreditorNumber",
                    "creditor_number",
                )
            )
        )
        user.craftsman_portal_id = (
            _text_or_none(
                _value(
                    row,
                    "CraftsmanPortalId",
                    "craftsman_portal_id",
                )
            )
        )
        user.craft_activities = (
            craft_activities
        )

        jurisdiction_mapping_changed = False

        if identity_managed_by_entra:
            if responsible_person_candidates is None:
                responsible_person_candidates = (
                    _responsible_person_candidates_by_email(
                        service,
                        allowed_domains,
                    )
                )

            jurisdiction_mapping_changed = (
                _sync_jurisdiction_person_mapping_by_email(
                    user,
                    responsible_person_candidates,
                )
            )

        current_values = (
            user.email,
            user.display_name,
            user.first_name,
            user.last_name,
            user.craftsman_erp_id,
            user.person_erp_id,
            user.creditor_erp_id,
            user.creditor_number,
            user.craftsman_portal_id,
            user.is_active_flag,
            frozenset(
                item.id
                for item
                in user.craft_activities
            ),
        )

        if created:
            result.created += 1
        elif (
                previous_values != current_values
                or jurisdiction_mapping_changed
        ):
            result.updated += 1
        else:
            result.unchanged += 1

    return result


def _configured_email_domains() -> set[str]:
    raw_value = settings.get(
        SettingKeys
        .WOWI_USER_EMAIL_DOMAIN_WHITELIST
    )
    values = (
        DOMAIN_SEPARATOR_PATTERN
        .split(str(raw_value or ""))
    )

    return {
        value
        .strip()
        .lower()
        .lstrip("@")
        .rstrip(".")
        for value in values
        if value
        .strip()
        .lstrip("@")
        .rstrip(".")
    }


def extract_craftsman_erp_id(
    row: Any,
) -> int | None:
    return _as_int(
        _value(
            row,
            "Id",
            "id",
            "id_",
        )
    )


def extract_craftsman_email(
    row: Any,
    allowed_domains: set[str] | None = None,
) -> str | None:
    person = _value(
        row,
        "Person",
        "person",
    )
    communications = []

    first_email = _value(
        person,
        "FirstEmailCommunication",
        "first_email_communication",
    )

    if first_email is not None:
        communications.append(first_email)

    communications.extend(
        _as_list(
            _value(
                person,
                "Communications",
                "communications",
            )
        )
    )

    main_communication = _value(
        row,
        "MainCommunication",
        "main_communication",
    )

    if main_communication is not None:
        communications.append(
            main_communication
        )

    for accessibility in (
        _craftsman_accessibilities(row)
    ):
        communication = _value(
            accessibility,
            "Communication",
            "communication",
        )

        if communication is not None:
            communications.append(
                communication
            )

    for communication in communications:
        email = _text_or_none(
            _value(
                communication,
                "Content",
                "content",
                "Value",
                "value",
            )
        )

        if email is None:
            continue

        normalized_email = (
            email.strip().lower()
        )

        if (
            EMAIL_PATTERN.fullmatch(
                normalized_email
            )
            is None
        ):
            continue

        domain = (
            normalized_email
            .rsplit("@", 1)[1]
            .rstrip(".")
        )

        if (
            allowed_domains is None
            or domain in allowed_domains
        ):
            return normalized_email

    return None


def extract_craft_activity_ids(
    row: Any,
) -> set[int]:
    result = set()

    for accessibility in (
        _craftsman_accessibilities(row)
    ):
        craft_activity = _value(
            accessibility,
            "CraftActivity",
            "craft_activity",
        )

        erp_id = _as_int(
            _value(
                craft_activity,
                "Id",
                "id",
                "id_",
            )
            or _value(
                accessibility,
                "CraftActivityId",
                "craft_activity_id",
            )
        )

        if erp_id is not None:
            result.add(erp_id)

    return result


def _craftsman_accessibilities(
    row: Any,
) -> list[Any]:
    return _as_list(
        _value(
            row,
            "CraftsmanAccessibilityList",
            "craftsman_accessibility_list",
            "CraftsmanAccessibility",
            "craftsman_accessibility",
        )
    )


def extract_craftsman_display_name(
    row: Any,
    fallback: str,
) -> str:
    person = _value(
        row,
        "Person",
        "person",
    )
    natural_person = _value(
        person,
        "NaturalPerson",
        "natural_person",
    )
    legal_person = _value(
        person,
        "LegalPerson",
        "legal_person",
    )

    natural_parts = [
        _text_or_none(
            _value(
                natural_person,
                "FirstName",
                "first_name",
            )
        ),
        _text_or_none(
            _value(
                natural_person,
                "LastName",
                "last_name",
            )
        ),
    ]
    natural_name = " ".join(
        part
        for part in natural_parts
        if part
    )

    legal_parts = [
        _text_or_none(
            _value(
                legal_person,
                "LongName1",
                "long_name1",
            )
        ),
        _text_or_none(
            _value(
                legal_person,
                "LongName2",
                "long_name2",
            )
        ),
    ]
    legal_name = " ".join(
        part
        for part in legal_parts
        if part
    )

    candidates = [
        _value(
            person,
            "Name",
            "name",
        ),
        natural_name,
        legal_name,
        _value(
            person,
            "Shortname",
            "shortname",
        ),
        fallback,
    ]

    for candidate in candidates:
        text = _text_or_none(candidate)

        if text:
            return text

    return fallback


def _value(
    source: Any,
    *names: str,
) -> Any:
    if source is None:
        return None

    if isinstance(source, dict):
        normalized = {
            str(key)
            .replace("_", "")
            .lower(): value
            for key, value
            in source.items()
        }

        for name in names:
            key = (
                name
                .replace("_", "")
                .lower()
            )

            if key in normalized:
                return normalized[key]

        return None

    for name in names:
        if hasattr(source, name):
            return getattr(source, name)

    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, tuple):
        return list(value)

    return [value]


def _as_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None

    return parsed if parsed > 0 else None


def _text_or_none(
    value: Any,
) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _responsible_person_candidates_by_email(
    service: WowiportService,
    allowed_domains: set[str],
) -> dict[str, dict[int, str]]:
    official_candidates: dict[
        int,
        dict[int, str | None],
    ] = {}

    def add_candidate(
        official_erp_id: int | None,
        person_erp_id: int | None,
        person_name: str | None,
    ) -> None:
        if (
            official_erp_id is None
            or person_erp_id is None
        ):
            return

        people = official_candidates.setdefault(
            official_erp_id,
            {},
        )
        normalized_name = _text_or_none(
            person_name
        )

        if (
            person_erp_id not in people
            or normalized_name is not None
        ):
            people[person_erp_id] = (
                normalized_name
            )

    jurisdictions = (
        WowiEconomicUnitJurisdiction.query
        .filter(
            WowiEconomicUnitJurisdiction
            .active.is_(True),
            WowiEconomicUnitJurisdiction
            .responsible_official_erp_id
            .isnot(None),
            WowiEconomicUnitJurisdiction
            .responsible_person_erp_id
            .isnot(None),
        )
        .all()
    )

    for jurisdiction in jurisdictions:
        add_candidate(
            jurisdiction
            .responsible_official_erp_id,
            jurisdiction
            .responsible_person_erp_id,
            jurisdiction.responsible_name,
        )

    economic_units = (
        WowiEconomicUnit.query
        .filter(
            WowiEconomicUnit.active.is_(True),
            WowiEconomicUnit
            .universal_responsibility
            .is_(True),
            WowiEconomicUnit
            .universal_responsible_official_erp_id
            .isnot(None),
            WowiEconomicUnit
            .universal_responsible_person_erp_id
            .isnot(None),
        )
        .all()
    )

    for economic_unit in economic_units:
        add_candidate(
            economic_unit
            .universal_responsible_official_erp_id,
            economic_unit
            .universal_responsible_person_erp_id,
            None,
        )

    candidates_by_email: dict[
        str,
        dict[int, str],
    ] = {}

    for (
        official_erp_id,
        people,
    ) in official_candidates.items():
        try:
            row = (
                service.get_responsible_official(
                    official_erp_id
                )
            )
        except Exception as exc:
            current_app.logger.warning(
                "responsible_official_lookup_failed "
                "official_erp_id=%s error=%s",
                official_erp_id,
                str(exc),
            )
            continue

        email = extract_craftsman_email(row, allowed_domains)

        if email is None:
            continue

        for (
            person_erp_id,
            fallback_name,
        ) in people.items():
            person_name = (
                extract_craftsman_display_name(
                    row,
                    fallback_name or email,
                )
            )
            candidates_by_email.setdefault(
                email,
                {},
            )[person_erp_id] = person_name

    return candidates_by_email


def _sync_jurisdiction_person_mapping_by_email(
    user: User,
    candidates_by_email: dict[
        str,
        dict[int, str],
    ],
) -> bool:
    normalized_email = str(
        user.email or ""
    ).strip().lower()

    if (
        not normalized_email
        or user.craftsman_erp_id is None
    ):
        return False

    candidates = candidates_by_email.get(
        normalized_email,
        {},
    )

    if not candidates:
        return False

    if len(candidates) != 1:
        current_app.logger.warning(
            "user_jurisdiction_mapping_ambiguous "
            "user_id=%s candidate_count=%s",
            user.id,
            len(candidates),
        )
        return False

    person_erp_id, person_name = next(
        iter(candidates.items())
    )

    mapping = (
        UserJurisdictionPersonMapping.query
        .filter_by(
            jurisdiction_person_erp_id=(
                person_erp_id
            )
        )
        .one_or_none()
    )

    if mapping is None:
        db.session.add(
            UserJurisdictionPersonMapping(
                user=user,
                jurisdiction_person_erp_id=(
                    person_erp_id
                ),
                person_name_snapshot=(
                    person_name
                ),
                active=True,
            )
        )
        return True

    if mapping.user_id != user.id:
        current_app.logger.warning(
            "user_jurisdiction_mapping_conflict "
            "person_erp_id=%s "
            "existing_user_id=%s "
            "target_user_id=%s",
            person_erp_id,
            mapping.user_id,
            user.id,
        )
        return False

    if not mapping.active:
        return False

    if (
        mapping.person_name_snapshot
        == person_name
    ):
        return False

    mapping.person_name_snapshot = person_name
    return True
