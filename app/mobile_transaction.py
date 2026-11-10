from contextvars import ContextVar
from hashlib import sha256
import json

from .extensions import db


current_mobile_transaction = ContextVar("current_mobile_transaction", default=None)


def mobile_transaction_active():
    return current_mobile_transaction.get() is not None


def commit_mobile_changes():
    if mobile_transaction_active():
        db.session.flush()
    else:
        db.session.commit()


def defer_mobile_delivery(kind, payload):
    transaction = current_mobile_transaction.get()
    if transaction is None or transaction.get("read_only"):
        return False
    from .models import MobileSyncDelivery
    digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    key = f"{transaction['user_id']}:{transaction['operation_id']}:{kind}:{digest}"
    if db.session.get(MobileSyncDelivery, key) is None:
        db.session.add(MobileSyncDelivery(id=key, user_id=transaction["user_id"], kind=kind, payload=payload))
    return True


def track_mobile_files(*paths):
    transaction = current_mobile_transaction.get()
    if transaction is not None:
        transaction.setdefault("files", []).extend(paths)


def record_mobile_photo_references(photos):
    transaction = current_mobile_transaction.get()
    if transaction is None:
        return
    for local_id, photo in zip(transaction.get("local_photo_ids", []), photos):
        transaction.setdefault("references", {})[str(local_id)] = photo.id
