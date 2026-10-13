from __future__ import annotations

from sqlalchemy.orm.attributes import set_committed_value

from .audit import audit
from .extensions import db
from .models import (
    SafetyInspectionGroup,
    SafetyInspectionJob,
    SafetyInspectionTemplate,
    SafetyInspectionTemplateItem,
    SafetyInspectionTemplateVersion,
)


JOB_DELETE_REASON = (
    "Mit diesem Prüfdetailplan wurden bereits Prüfaufträge angelegt. "
    "Eine Löschung ist nicht möglich."
)
GROUP_DELETE_REASON = (
    "Dieser Prüfdetailplan ist noch einer Prüfgruppe zugeordnet. "
    "Bitte der Prüfgruppe zuerst einen anderen Prüfdetailplan zuordnen."
)


def get_safety_inspection_template_delete_reasons(templates):
    template_ids = [template.id for template in templates]
    used_template_ids = set()
    assigned_template_ids = set()

    for offset in range(0, len(template_ids), 500):
        selected_ids = template_ids[offset:offset + 500]
        used_rows = (
            SafetyInspectionTemplateVersion.query
            .with_entities(SafetyInspectionTemplateVersion.template_id)
            .join(
                SafetyInspectionJob,
                SafetyInspectionJob.template_version_id
                == SafetyInspectionTemplateVersion.id,
            )
            .filter(
                SafetyInspectionTemplateVersion.template_id.in_(selected_ids)
            )
            .distinct()
            .all()
        )
        assigned_rows = (
            SafetyInspectionGroup.query
            .with_entities(SafetyInspectionGroup.template_id)
            .filter(SafetyInspectionGroup.template_id.in_(selected_ids))
            .distinct()
            .all()
        )
        used_template_ids.update(row.template_id for row in used_rows)
        assigned_template_ids.update(row.template_id for row in assigned_rows)

    return {
        template.id: (
            JOB_DELETE_REASON
            if template.used_for_jobs or template.id in used_template_ids
            else GROUP_DELETE_REASON
            if template.id in assigned_template_ids
            else None
        )
        for template in templates
    }


def delete_safety_inspection_template(template_id, user):
    template = db.session.get(
        SafetyInspectionTemplate,
        template_id,
        with_for_update=True,
        populate_existing=True,
    )
    if template is None:
        raise LookupError(template_id)

    if template.used_for_jobs:
        raise ValueError(JOB_DELETE_REASON)

    versions = (
        SafetyInspectionTemplateVersion.query
        .filter_by(template_id=template.id)
        .order_by(SafetyInspectionTemplateVersion.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    existing_job = (
        SafetyInspectionJob.query
        .with_entities(SafetyInspectionJob.id)
        .join(
            SafetyInspectionTemplateVersion,
            SafetyInspectionJob.template_version_id
            == SafetyInspectionTemplateVersion.id,
        )
        .filter(SafetyInspectionTemplateVersion.template_id == template.id)
        .with_for_update(of=SafetyInspectionJob)
        .first()
    )
    if existing_job is not None:
        raise ValueError(JOB_DELETE_REASON)

    existing_group = (
        SafetyInspectionGroup.query
        .with_entities(SafetyInspectionGroup.id)
        .filter_by(template_id=template.id)
        .with_for_update()
        .first()
    )
    if existing_group is not None:
        raise ValueError(GROUP_DELETE_REASON)

    items = (
        SafetyInspectionTemplateItem.query
        .join(
            SafetyInspectionTemplateVersion,
            SafetyInspectionTemplateItem.template_version_id
            == SafetyInspectionTemplateVersion.id,
        )
        .filter(SafetyInspectionTemplateVersion.template_id == template.id)
        .order_by(SafetyInspectionTemplateItem.id)
        .with_for_update(of=SafetyInspectionTemplateItem)
        .populate_existing()
        .all()
    )
    items_by_version = {version.id: [] for version in versions}

    for item in items:
        items_by_version[item.template_version_id].append(item)

    for version in versions:
        set_committed_value(version, "items", items_by_version[version.id])
        set_committed_value(version, "jobs", [])

    set_committed_value(template, "versions", versions)
    set_committed_value(template, "groups", [])

    audit(
        "safety_inspection.template_deleted",
        "safety_inspection_template",
        template.id,
        {
            "name": template.name,
            "version_ids": [version.id for version in versions],
        },
        actor_user_id=user.id,
    )
    db.session.delete(template)
    db.session.flush()