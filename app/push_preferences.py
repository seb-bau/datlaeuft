from __future__ import annotations

from .models import PushEventType, User


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


def is_push_event_enabled(
    user: User,
    event_type: PushEventType,
) -> bool:
    if event_type == PushEventType.CHANGED_COMMISSION:
        return False

    if event_type not in CONFIGURABLE_PUSH_EVENTS:
        return True

    preferences = user.push_event_preferences or {}
    if event_type.value in preferences:
        return bool(preferences[event_type.value])

    if event_type == PushEventType.NEW_COMMISSION:
        return bool(user.push_new_commission_enabled)

    if event_type in COMMISSION_CHANGE_EVENTS:
        return bool(user.push_changed_commission_enabled)

    if event_type in SAFETY_INSPECTION_EVENTS:
        return bool(user.push_safety_inspection_enabled)

    return True


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
    }