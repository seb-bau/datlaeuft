from __future__ import annotations

from .models import PushEventType, User
from .settings import SettingKeys, settings


COMMISSION_CHANGE_EVENTS = (
    PushEventType.APPOINTMENT_ACCEPTED,
    PushEventType.APPOINTMENT_REJECTED,
    PushEventType.COMMISSION_PRIORITY_RAISED,
)
SAFETY_INSPECTION_EVENTS = (
    PushEventType.NEW_SAFETY_INSPECTION,
    PushEventType.CHANGED_SAFETY_INSPECTION,
    PushEventType.URGENT_SAFETY_DEFECT,
)
CONFIGURABLE_PUSH_EVENTS = (
    PushEventType.NEW_COMMISSION,
    *COMMISSION_CHANGE_EVENTS,
    PushEventType.NEW_PROCESS_INFO,
    *SAFETY_INSPECTION_EVENTS,
)


PUSH_EVENT_LABELS = {
    "new_commission": "Neuer Auftrag",
    "appointment_accepted": "Terminzusage",
    "appointment_rejected": "Terminabsage",
    "commission_priority_raised": "Auftrag mit hoher Priorität",
    "new_process_info": "Neue Vorgangsinformation",
    "new_safety_inspection": "Neue Verkehrssicherheitsprüfung",
    "changed_safety_inspection": "Geänderte Verkehrssicherheitsprüfung",
    "urgent_safety_defect": "Dringender Sicherheitsmangel",
}


def push_default(event_type: PushEventType) -> bool:
    definition = getattr(SettingKeys, "PUSH_DEFAULT_" + event_type.name, None)
    return bool(settings.get(definition)) if definition is not None else True


def push_preference_overrides(user: User) -> dict[str, bool]:
    preferences = dict(user.push_event_preferences or {})
    for event_types, attribute in (
        ((PushEventType.NEW_COMMISSION,), "push_new_commission_enabled"),
        (COMMISSION_CHANGE_EVENTS, "push_changed_commission_enabled"),
        (SAFETY_INSPECTION_EVENTS, "push_safety_inspection_enabled"),
    ):
        if getattr(user, attribute) is False:
            for event_type in event_types:
                preferences.setdefault(event_type.value, False)
    return preferences


def admin_push_preference_rows(user: User) -> list[dict]:
    overrides = push_preference_overrides(user)
    return [{
        "event": event_type.value, "label": PUSH_EVENT_LABELS[event_type.value],
        "mode": ("on" if overrides[event_type.value] else "off") if event_type.value in overrides else "default",
        "default": push_default(event_type), "enabled": is_push_event_enabled(user, event_type),
    } for event_type in CONFIGURABLE_PUSH_EVENTS]


def save_admin_push_preferences(user: User, form) -> None:
    if form.get("push_preferences_present") != "1":
        return
    preferences = push_preference_overrides(user)
    for event_type in CONFIGURABLE_PUSH_EVENTS:
        mode = form.get("push_event_" + event_type.value, "default")
        if mode not in {"default", "on", "off"}:
            raise ValueError("Ungültige Push-Einstellung.")
        if mode == "default":
            preferences.pop(event_type.value, None)
        else:
            preferences[event_type.value] = mode == "on"
    user.push_event_preferences = preferences
    user.push_new_commission_enabled = True
    user.push_changed_commission_enabled = True
    user.push_safety_inspection_enabled = True


def is_push_event_enabled(
    user: User,
    event_type: PushEventType,
) -> bool:
    if event_type == PushEventType.CHANGED_COMMISSION:
        return False

    if event_type not in CONFIGURABLE_PUSH_EVENTS:
        return True

    preferences = push_preference_overrides(user)
    if event_type.value in preferences:
        return bool(preferences[event_type.value])

    return push_default(event_type)


def serialize_push_preferences(user: User) -> dict:
    return {
        "new_commission_enabled": is_push_event_enabled(
            user, PushEventType.NEW_COMMISSION,
        ),
        "changed_commission_enabled": any(
            is_push_event_enabled(user, event_type)
            for event_type in COMMISSION_CHANGE_EVENTS
        ),
        "events": {
            event_type.value: is_push_event_enabled(user, event_type)
            for event_type in CONFIGURABLE_PUSH_EVENTS
        },
        "defaults": {event_type.value: push_default(event_type) for event_type in CONFIGURABLE_PUSH_EVENTS},
        "overrides": push_preference_overrides(user),
    }