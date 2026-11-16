document.addEventListener("DOMContentLoaded", () => {
    const commissionForm = document.getElementById(
        "commissionForm"
    );

    if (!commissionForm) {
        return;
    }

    const canCreate = (
        commissionForm.dataset.canCreate === "true"
    );
    const objectSearchUrl = (
        commissionForm.dataset.objectSearchUrl
    );
    const assignmentPreviewUrl = (
        commissionForm.dataset.assignmentPreviewUrl
    );
    const defaultCommissionTypeId = String(
        commissionForm.dataset.defaultCommissionTypeId
        || "0"
    );
    const assignmentRuleUserId = String(
        commissionForm.dataset.assignmentRuleUserId
        || ""
    );
    const assignmentRuleExternalId = String(
        commissionForm.dataset.assignmentRuleExternalId
        || ""
    );
    const assignmentRuleType = String(
        commissionForm.dataset.assignmentRuleType
        || ""
    );
    const assignmentRuleLabel = String(
        commissionForm.dataset.assignmentRuleLabel
        || ""
    );
    const assignmentLocked = (
        commissionForm.dataset.assignmentLocked
        === "true"
    );
    const assignmentTypeLocked = (
        commissionForm.dataset.assignmentTypeLocked
        === "true"
    );
    const externalAssignmentLocked = (
        commissionForm.dataset
            .externalAssignmentLocked
        === "true"
    );
    let initialObject = null;

    try {
        initialObject = JSON.parse(
            commissionForm.dataset.initialObject
            || "null"
        );
    } catch {
        initialObject = null;
    }

    const positions = document.getElementById("positions");
    const positionTemplate = document.getElementById(
        "positionTemplate"
    );
    const addPosition = document.getElementById(
        "addPosition"
    );
    const objectSearch = document.getElementById(
        "objectSearch"
    );
    const objectSearchContext = document.getElementById(
    "objectSearchContext"
    );
    const objectResults = document.getElementById(
        "objectResults"
    );
    const objectSummary = document.getElementById(
        "objectSummary"
    );
    const objectType = document.getElementById(
        "objectType"
    );
    const objectId = document.getElementById(
        "objectId"
    );
    const submitCommission = document.getElementById(
        "submitCommission"
    );
    const craftActivity = document.getElementById(
        "craftActivity"
    );
    const craftsmanUser = document.getElementById(
        "craftsmanUser"
    );
    const companyCode = document.getElementById(
        "companyCode"
    );
    const notifyTenants = document.getElementById("notifyTenants");
    let tenantNotificationPreference = notifyTenants.value;
    const assignmentStatus = document.getElementById(
        "assignmentStatus"
    );
    const assignmentTypeInputs = Array.from(
    commissionForm.querySelectorAll(
        "input[name='assignment_type']"
    )
    );
    const internalCraftsmanField =
        document.getElementById(
            "internalCraftsmanField"
        );
    const externalCraftsmanField =
        document.getElementById(
            "externalCraftsmanField"
        );
    const externalCraftsman =
        document.getElementById(
            "externalCraftsman"
        );
    const automaticFieldClass =
        "field-state-automatic";
    const requiredFieldClass =
        "field-state-required";

    function selectedAssignmentType() {
        return (
            assignmentTypeInputs.find(
                (input) => input.checked
            )?.value
            || "internal"
        );
    }

    function isInternalAssignment() {
        return (
            selectedAssignmentType()
            === "internal"
        );
    }

    function isInternalAssignmentLocked() {
        return (
            isInternalAssignment()
            && assignmentLocked
        );
    }

    function applyCraftAssignmentDefault() {
        if (assignmentTypeLocked) {
            return;
        }

        const selectedOption =
            craftActivity.selectedOptions[0];
        const configuredAssignmentType = String(
            selectedOption?.dataset
                .defaultAssignmentType
            || "internal"
        );
        const assignmentType = (
            configuredAssignmentType === "external"
                ? "external"
                : "internal"
        );
        const assignmentInput =
            assignmentTypeInputs.find((input) => {
                return input.value === assignmentType;
            });

        if (assignmentInput) {
            assignmentInput.checked = true;
        }
    }

    function fieldValue(field) {
        return String(field.value || "").trim();
    }

    function hasAutomaticValue(field) {
        const automaticValue = (
            field.dataset.automaticValue
        );

        return (
            automaticValue !== undefined
            && automaticValue !== ""
            && fieldValue(field)
            === String(automaticValue).trim()
        );
    }

    function isRequiredFieldMissing(field) {
        return (
            field.required
            && !field.disabled
            && fieldValue(field) === ""
        );
    }

    function updateFieldState(field) {
        const isMissing =
            isRequiredFieldMissing(field);
        const isAutomatic =
            !isMissing
            && hasAutomaticValue(field);

        field.classList.toggle(
            automaticFieldClass,
            isAutomatic
        );
        field.classList.toggle(
            requiredFieldClass,
            isMissing
        );

        if (isMissing) {
            field.setAttribute(
                "aria-invalid",
                "true"
            );
        } else {
            field.removeAttribute(
                "aria-invalid"
            );
        }
    }

    function updateObjectState() {
        const automaticValue = String(
            objectSummary.dataset
                .automaticValue
            || ""
        );
        const currentValue = String(
            objectId.value || ""
        );
        const isMissing = !currentValue;
        const isAutomatic = (
            !isMissing
            && automaticValue
            && automaticValue === currentValue
        );

        objectSummary.classList.toggle(
            automaticFieldClass,
            Boolean(isAutomatic)
        );
        objectSummary.classList.toggle(
            requiredFieldClass,
            isMissing
        );

        if (isMissing) {
            objectSummary.setAttribute(
                "aria-invalid",
                "true"
            );
        } else {
            objectSummary.removeAttribute(
                "aria-invalid"
            );
        }
    }

    function updateAllFieldStates() {
        commissionForm
            .querySelectorAll(
                "input[required], "
                + "select[required], "
                + "textarea[required]"
            )
            .forEach(updateFieldState);

        updateObjectState();
    }

    function markFieldAsManual(field) {
        if (
            "automaticValue"
            in field.dataset
        ) {
            delete field.dataset
                .automaticValue;
        }

        updateFieldState(field);
    }

    commissionForm.addEventListener(
        "input",
        (event) => {
            if (
                event.target.matches(
                    "input, textarea"
                )
            ) {
                markFieldAsManual(
                    event.target
                );
            }
        }
    );

    commissionForm.addEventListener(
        "change",
        (event) => {
            if (
                event.target.matches(
                    "input, select, textarea"
                )
            ) {
                markFieldAsManual(
                    event.target
                );
            }

            updateAllFieldStates();
        }
    );

    commissionForm.addEventListener(
        "invalid",
        (event) => {
            updateFieldState(event.target);
        },
        true
    );
    let assignmentRequestId = 0;
    let assignmentAbortController = null;
    const assignmentTimeoutMilliseconds = 12000;
    const minimumObjectSearchLength = 2;
    const objectSearchDelayMilliseconds = 300;
    let objectSearchTimer = null;
    let objectSearchRequestId = 0;

    function objectTypeLabel(item) {
        return {
            license_agreement: "Mietvertrag",
            use_unit: "Wohnung / Nutzungseinheit",
            building: "Gebäude",
            economic_unit: "Wirtschaftseinheit"
        }[item.object_type] || "Objekt";
    }

    function ticketAttachmentsReady() {
        const field = document.getElementById?.("ticketAttachmentSelectionReady");
        return !field || field.value === "true";
    }

    function objectHeading(item) {
        if (item.object_type === "license_agreement") {
            return item.tenant_name || (item.is_vacancy ? "Leerstand" : "Mietvertrag");
        }
        const number = {
            use_unit: item.use_unit?.id_num,
            building: item.building?.id_num,
            economic_unit: item.economic_unit?.id_num
        }[item.object_type];
        const address = item.object_type === "economic_unit"
            ? item.economic_unit?.address
            : item.use_unit?.address || item.building?.address;
        const location = item.object_type === "use_unit" ? item.use_unit?.description : null;
        return [number, address || item.economic_unit?.name || item.label || objectTypeLabel(item), location]
            .filter(Boolean).join(" · ");
    }

    function objectIcon(item) {
        const icon = document.createElement("i");
        icon.className = "bi " + ({
            license_agreement: "bi-file-earmark-text", use_unit: "bi-door-open",
            building: "bi-house-door", economic_unit: "bi-buildings"
        }[item.object_type] || "bi-geo-alt") + " me-2 text-secondary";
        icon.setAttribute("aria-hidden", "true");
        icon.title = objectTypeLabel(item);
        return icon;
    }

    function groupedObjectResults(items, searchContext) {
        const rootType = {address: "building", economic_unit: "economic_unit", use_unit: "use_unit"}[searchContext];
        if (!rootType) return items;
        const grouped = [];
        const included = new Set();
        function append(parent, depth) {
            if (included.has(parent)) return;
            grouped.push({...parent, search_depth: depth});
            included.add(parent);
            const childTypes = {
                economic_unit: ["building"], building: ["license_agreement", "use_unit"], use_unit: ["license_agreement"]
            }[parent.object_type] || [];
            for (const child of items) {
                const reference = child[parent.object_type]?.erp_id;
                if (childTypes.includes(child.object_type) && reference && reference === parent[parent.object_type]?.erp_id) {
                    append(child, depth + 1);
                }
            }
        }
        for (const item of items.filter(item => item.object_type === rootType)) append(item, 0);
        return [...grouped, ...items.filter(item => !included.has(item))];
    }

    function futureContractBadge(item) {
        if (!item.is_future || !item.license_agreement?.start_date) {
            return null;
        }
        const startDate = item.license_agreement.start_date.split("-").reverse().join(".");
        const badge = document.createElement("span");
        badge.className = "badge rounded-pill bg-primary-subtle text-primary-emphasis fw-normal ms-2";
        badge.textContent = `Vertrag ab ${startDate}`;
        return badge;
    }

    function retrospectiveCommissionSelected() {
        return document.getElementById?.("retrospectiveCommission")?.value === "true";
    }

    function updateTenantNotificationOptions(item) {
        const eligible = Boolean(item?.license_agreement?.erp_id && !item.is_vacancy);
        notifyTenants.dataset.available = eligible ? "true" : "false";
        const available = eligible && !retrospectiveCommissionSelected();
        notifyTenants.disabled = !available;
        notifyTenants.value = available ? tenantNotificationPreference : "false";
        notifyTenants.title = retrospectiveCommissionSelected() ? "Bei nachträglicher Beauftragung werden keine Mieter informiert." : (available ? "" : "Eine Mieter-Mitteilung benötigt einen belegten Mietvertrag.");
        updateFieldState(notifyTenants);
    }

    notifyTenants.addEventListener("change", () => {
        tenantNotificationPreference = notifyTenants.value;
    });

    function objectText(item) {
        const showUnitLocation = item.object_type === "license_agreement";
        const location = showUnitLocation ? item.use_unit?.description : null;
        const values = [
            item.object_type === "license_agreement" && item.license_agreement?.id_num
                ? `Vertrag ${item.license_agreement.id_num}`
                : null,
            item.is_vacancy && item.object_type !== "license_agreement"
                ? "Leerstand"
                : null,
            location,
            showUnitLocation ? item.use_unit?.address || item.building?.address : null,
            item.object_type === "economic_unit" && item.economic_unit?.address ? item.economic_unit?.name : null,
        ];

        return values
            .filter(Boolean)
            .join(" · ");
    }

        function selectObject(
        item,
        resetAssignments = true
    ) {
        objectType.value = item.object_type;
        objectId.value = item.object_id;
        updateTenantNotificationOptions(item);
                if (resetAssignments) {
            delete objectSummary.dataset
                .automaticValue;
        }

        updateObjectState();

        applyAutomaticValue(
            companyCode,
            String(
                item.company_code?.erp_id
                || ""
            ),
            resetAssignments
        );

        if (
            resetAssignments
            && !hasAssignmentRuleValue()
            && !isInternalAssignmentLocked()
        ) {
            clearAutomaticValue(
                craftsmanUser,
                true
            );
        }

        updateAssignmentControls();
        objectSummary.replaceChildren();
        objectSummary.classList.remove(
            "d-flex",
            "align-items-center",
            "justify-content-center",
            "text-center"
        );

        const heading = document.createElement(
            "div"
        );
        heading.className = "fw-semibold";
        heading.textContent = objectHeading(item);
        heading.prepend(objectIcon(item));
        const statusBadge = futureContractBadge(item);
        if (statusBadge) {
            heading.appendChild(statusBadge);
        }

        const details = document.createElement(
            "div"
        );
        details.className = "text-muted";
        details.textContent = objectText(item);

        objectSummary.appendChild(heading);

        if (details.textContent) {
            objectSummary.appendChild(details);
        }

        objectSummary.dispatchEvent(new CustomEvent("commission-object-selected", {detail: item}));
        objectResults.replaceChildren();
        submitCommission.disabled = (
            !canCreate
            || !objectId.value
            || !ticketAttachmentsReady()
        );

        updateAssignmentPreview({
            forceCraftsman: (
                resetAssignments
                && !hasAssignmentRuleValue()
                && !isInternalAssignmentLocked()
            )
        });
    }

        function showObjectSearchMessage(
        text,
        className
    ) {
        const message = document.createElement(
            "div"
        );
        message.className = (
            className
            + " text-center"
        );
        message.textContent = text;
        objectResults.appendChild(message);
    }

    async function loadObjects(
        query,
        searchContext
    ) {
        const currentRequestId = (
            ++objectSearchRequestId
        );

        objectResults.replaceChildren();
        showObjectSearchMessage(
            "Suche läuft …",
            "list-group-item text-muted"
        );

        try {
            const searchUrl = new URL(
                objectSearchUrl,
                window.location.origin
            );
            searchUrl.searchParams.set("q", query);
            searchUrl.searchParams.set(
                "context",
                searchContext
            );

            const response = await fetch(
                searchUrl.toString()
            );
            const payload = await response.json();

            if (
                currentRequestId
                !== objectSearchRequestId
            ) {
                return;
            }

            objectResults.replaceChildren();

            if (!response.ok) {
                throw new Error(
                    payload.message
                    || "Die Suche ist fehlgeschlagen."
                );
            }

            if (!payload.items.length) {
                showObjectSearchMessage(
                    "Keine passenden Objekte gefunden.",
                    "list-group-item text-muted"
                );
                return;
            }

            groupedObjectResults(payload.items, searchContext).forEach((item) => {
                const button = document.createElement(
                    "button"
                );
                button.type = "button";
                button.className = (
                    "list-group-item "
                    + "list-group-item-action py-2"
                );
                if (item.search_depth > 0) {
                    button.className += " commission-object-child";
                }
                if (item.search_depth > 1) button.className += " commission-object-grandchild";
                button.setAttribute("aria-label", `${objectTypeLabel(item)}: ${objectHeading(item)} · ${objectText(item)}`);

                const title = document.createElement(
                    "div"
                );
                title.className = "fw-semibold";
                title.textContent = objectHeading(item);
                title.prepend(objectIcon(item));
                const statusBadge = futureContractBadge(item);
                if (statusBadge) {
                    title.appendChild(statusBadge);
                }

                const details = document.createElement(
                    "div"
                );
                details.className = "small text-muted";
                details.textContent = objectText(item);

                button.append(
                    title,
                    details
                );
                button.addEventListener(
                    "click",
                    () => selectObject(item)
                );
                objectResults.appendChild(button);
            });
        } catch (error) {
            if (
                currentRequestId
                !== objectSearchRequestId
            ) {
                return;
            }

            objectResults.replaceChildren();
            showObjectSearchMessage(
                error instanceof Error
                    ? error.message
                    : "Die Suche ist fehlgeschlagen.",
                "list-group-item text-danger"
            );
        }
    }

    function filterServicePositions() {
        const selectedCraftId = craftActivity.value;
        const selects = Array.from(
            positions.querySelectorAll(
                '[name="position_service_catalog_id"]'
            )
        );

        addPosition.disabled = !selectedCraftId;

        selects.forEach((select) => {
            Array.from(select.options)
                .forEach((option) => {
                    if (!option.value) {
                        option.hidden = false;
                        option.disabled = false;
                        return;
                    }

                    const matchesCraft = Boolean(
                        selectedCraftId
                        && option.dataset.craftId
                        === selectedCraftId
                    );

                    option.hidden = !matchesCraft;
                    option.disabled = !matchesCraft;
                });

            if (
                select.value
                && select.selectedOptions[0]
                    ?.dataset
                    .craftId
                !== selectedCraftId
            ) {
                select.value = "";
            }

            select.disabled = !selectedCraftId;
            select.options[0].textContent = (
                selectedCraftId
                    ? "Bitte auswählen"
                    : "Bitte zuerst Gewerk auswählen"
            );
        });
    }

    function applyCraftServicePositionDefault() {
        const defaultServiceCatalogId = String(
            craftActivity.selectedOptions[0]
                ?.dataset.defaultServiceCatalogId
            || ""
        );
        const select = positions.querySelector(
            '[name="position_service_catalog_id"]'
        );

        if (
            !defaultServiceCatalogId
            || !select
            || select.disabled
            || select.value
        ) {
            return;
        }

        const defaultOption = Array.from(select.options)
            .find((option) => {
                return (
                    option.value === defaultServiceCatalogId
                    && option.dataset.craftId === craftActivity.value
                    && !option.disabled
                    && !option.hidden
                );
            });

        if (defaultOption) {
            applyAutomaticValue(select, defaultOption.value);
        }
    }

    function setAssignmentStatus(
        message,
        isError = false
    ) {
        assignmentStatus.textContent = message;
        assignmentStatus.className = (
            isError
                ? "form-text text-danger"
                : "form-text"
        );
    }

    function applyAutomaticValue(
        select,
        value,
        force = false
    ) {
        const normalizedValue = String(
            value || ""
        );
        const previousAutomaticValue = (
            select.dataset.automaticValue
            || ""
        );
        const shouldReplace = (
            force
            || !select.value
            || select.value
            === previousAutomaticValue
        );

        select.dataset.automaticValue =
            normalizedValue;

        if (shouldReplace) {
            select.value = normalizedValue;
        }

        updateFieldState(select);
    }

    function clearAutomaticValue(
        select,
        force = false
    ) {
        const automaticValue = (
            select.dataset.automaticValue
            || ""
        );

        if (
            force
            || (
                automaticValue
                && select.value
                === automaticValue
            )
        ) {
            select.value = "";
        }

        delete select.dataset.automaticValue;
        updateFieldState(select);
    }

        function hasAssignmentRuleValue() {
            return Boolean(
                isInternalAssignment()
                && assignmentRuleUserId
                && craftsmanUser.value
                === assignmentRuleUserId
            );
        }

        function hasExternalAssignmentRuleValue() {
        return Boolean(
            selectedAssignmentType() === "external"
            && assignmentRuleExternalId
            && externalCraftsman.value
                === assignmentRuleExternalId
        );
    }


    function assignmentRuleStatusText(
        companyCodeValue = ""
    ) {
        const modeText = (
            assignmentTypeLocked
            || assignmentLocked
            || externalAssignmentLocked
        )
            ? "Verbindliche KI-Regel"
            : "KI-Regel";
        const companyText = companyCodeValue
            ? ` · Buchwerk ${companyCodeValue}`
            : "";

        return (
            `${modeText} „${assignmentRuleLabel}“`
            + companyText
        );
    }

    function externalCraftIds(option) {
        return String(
            option.dataset.craftIds || ""
        )
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean);
    }

    function filterExternalCraftsmen({
        force = false,
        allowedIds = null
    } = {}) {
        const selectedCraftId = String(
            craftActivity.value || ""
        );
        const allowedIdSet = (
            Array.isArray(allowedIds)
                ? new Set(
                    allowedIds.map(
                        (value) => String(value)
                    )
                )
                : null
        );
        const previousAutomaticValue = String(
            externalCraftsman.dataset
                .automaticValue
            || ""
        );
        const candidates = [];

        Array.from(
            externalCraftsman.options
        ).forEach((option) => {
            if (!option.value) {
                option.hidden = false;
                option.disabled = false;
                return;
            }

            const craftIds =
                externalCraftIds(option);
            const matchesCraft = Boolean(
                selectedCraftId
                && (
                    !craftIds.length
                    || craftIds.includes(
                        selectedCraftId
                    )
                )
            );
            const matchesPreview = (
                allowedIdSet === null
                || allowedIdSet.has(
                    option.value
                )
            );
            const isEligible = (
                matchesCraft
                && matchesPreview
            );

            option.hidden = !isEligible;
            option.disabled = !isEligible;

            if (isEligible) {
                candidates.push(option);
            }
        });

        const currentIsCandidate =
            candidates.some((option) => {
                return (
                    option.value
                    === externalCraftsman.value
                );
            });
        const preferredId = String(
            craftActivity.selectedOptions[0]
                ?.dataset
                .preferredExternalCraftsmanId
            || ""
        );
        const automaticOption = (
            candidates.find((option) => {
                return option.value
                    === assignmentRuleExternalId;
            })
            || candidates.find((option) => {
                return option.value
                    === preferredId;
            })
            || (
                !force && currentIsCandidate
                    ? candidates.find((option) => {
                        return option.value
                            === externalCraftsman.value;
                    })
                    : null
            )
            || (
                candidates.length === 1
                    ? candidates[0]
                    : null
            )
        );

        if (
            force
            || !currentIsCandidate
            || (
                previousAutomaticValue
                && externalCraftsman.value
                === previousAutomaticValue
            )
        ) {
            if (automaticOption) {
                applyAutomaticValue(
                    externalCraftsman,
                    automaticOption.value,
                    true
                );
            } else {
                clearAutomaticValue(
                    externalCraftsman,
                    true
                );
            }
        } else {
            updateFieldState(
                externalCraftsman
            );
        }

        externalCraftsman.options[0]
            .textContent = (
                !selectedCraftId
                    ? (
                        "Bitte zuerst Gewerk "
                        + "auswählen"
                    )
                    : !candidates.length
                        ? (
                            "Kein passender externer "
                            + "Handwerker"
                        )
                        : "Bitte auswählen"
            );

        return candidates;
    }

    function selectedExternalCraftsmanName() {
        const selectedOption =
            externalCraftsman
                .selectedOptions[0];

        if (
            !selectedOption
            || !selectedOption.value
        ) {
            return "";
        }

        return (
            selectedOption.dataset.displayName
            || selectedOption.textContent.trim()
        );
    }

    function updateAssignmentControls() {
        if (assignmentTypeLocked) {
            assignmentTypeInputs.forEach((input) => {
                input.checked = (
                    input.value === assignmentRuleType
                );
                input.disabled = (
                    input.value !== assignmentRuleType
                );
            });
        }

        const hasObject = Boolean(
            objectType.value
            && objectId.value
        );
        const hasCraft = Boolean(
            craftActivity.value
        );
        const isInternal =
            isInternalAssignment();

        internalCraftsmanField.classList.toggle(
            "d-none",
            !isInternal
        );
        externalCraftsmanField.classList.toggle(
            "d-none",
            isInternal
        );

        companyCode.disabled = !hasObject;
        craftsmanUser.required = isInternal;
        externalCraftsman.required = !isInternal;
        craftsmanUser.disabled = (
            !isInternal
            || isInternalAssignmentLocked()
            || !(hasObject && hasCraft)
        );
        externalCraftsman.disabled = (
            isInternal
            || !hasCraft
            || externalAssignmentLocked
        );

        updateFieldState(craftsmanUser);
        updateFieldState(externalCraftsman);
        updateFieldState(companyCode);
    }

    async function updateAssignmentPreview({
        forceCraftsman = false
    } = {}) {
        const currentRequestId = (
            ++assignmentRequestId
        );

        if (assignmentAbortController) {
            assignmentAbortController.abort();
        }

        const abortController =
            new AbortController();

        assignmentAbortController =
            abortController;

        const timeoutId = window.setTimeout(
            () => {
                abortController.abort();
            },
            assignmentTimeoutMilliseconds
        );
        const selectedCraftId = (
            craftActivity.value
        );
        const hasObject = Boolean(
            objectType.value
            && objectId.value
        );
        const isInternal =
            isInternalAssignment();

        if (isInternal) {
            if (
                forceCraftsman
                && !isInternalAssignmentLocked()
            ) {
                clearAutomaticValue(
                    craftsmanUser,
                    true
                );
            }
        } else {
            filterExternalCraftsmen({
                force: forceCraftsman
            });
        }

        if (!hasObject) {
            clearAutomaticValue(
                companyCode,
                true
            );

            if (
                isInternal
                && !isInternalAssignmentLocked()
                && !hasAssignmentRuleValue()
            ) {
                clearAutomaticValue(
                    craftsmanUser,
                    true
                );
            }

            updateAssignmentControls();

            if (isInternal) {
                setAssignmentStatus(
                    hasAssignmentRuleValue()
                        ? (
                            assignmentRuleStatusText()
                            + ". Bitte zusätzlich "
                            + "ein Objekt wählen."
                        )
                        : (
                            "Bitte ein Objekt wählen. "
                            + "Das Gewerk kann "
                            + "unabhängig davon "
                            + "ausgewählt werden."
                        )
                );
            } else {
                const externalName =
                    selectedExternalCraftsmanName();

                setAssignmentStatus(
                    externalName
                        ? (
                            "Externer Vorschlag: "
                            + externalName
                            + ". Bitte zusätzlich "
                            + "ein Objekt wählen."
                        )
                        : (
                            "Bitte Objekt und Gewerk "
                            + "auswählen."
                        )
                );
            }

            window.clearTimeout(timeoutId);

            if (
                assignmentAbortController
                === abortController
            ) {
                assignmentAbortController = null;
            }

            return;
        }

        if (
            isInternal
            && !selectedCraftId
            && !isInternalAssignmentLocked()
            && !hasAssignmentRuleValue()
        ) {
            clearAutomaticValue(
                craftsmanUser,
                true
            );
        }

        updateAssignmentControls();

        const parameters =
            new URLSearchParams({
                assignment_type:
                    selectedAssignmentType(),
                object_type: objectType.value,
                object_id: objectId.value
            });

        if (selectedCraftId) {
            parameters.set(
                "craft_activity_id",
                selectedCraftId
            );
        }

        setAssignmentStatus(
            selectedCraftId
                ? (
                    isInternal
                        ? (
                            "Automatische Zuweisung "
                            + "wird ermittelt …"
                        )
                        : (
                            "Externe Zuweisung "
                            + "wird ermittelt …"
                        )
                )
                : (
                    "Buchwerk wird aus dem "
                    + "Objekt ermittelt …"
                )
        );

        try {
            const previewUrl = new URL(
                assignmentPreviewUrl,
                window.location.origin
            );
            previewUrl.search =
                parameters.toString();

            const response = await fetch(
                previewUrl.toString(),
                {
                    method: "GET",
                    headers: {
                        Accept: "application/json"
                    },
                    cache: "no-store",
                    signal: abortController.signal
                }
            );
            const contentType = (
                response.headers.get(
                    "content-type"
                )
                || ""
            );

            if (
                !contentType.includes(
                    "application/json"
                )
            ) {
                throw new Error(
                    "Der Server hat keine gültige "
                    + "Zuweisungsantwort geliefert."
                );
            }

            const payload =
                await response.json();

            if (
                currentRequestId
                !== assignmentRequestId
            ) {
                return;
            }

            if (payload.company_code_erp_id) {
                applyAutomaticValue(
                    companyCode,
                    String(
                        payload
                            .company_code_erp_id
                    )
                );
            }

            if (!response.ok) {
                throw new Error(
                    payload.message
                    || (
                        "Die automatische "
                        + "Zuweisung ist "
                        + "fehlgeschlagen."
                    )
                );
            }

            if (isInternal) {
                if (
                    isInternalAssignmentLocked()
                    || hasAssignmentRuleValue()
                ) {
                    setAssignmentStatus(
                        assignmentRuleStatusText(
                            payload
                                .company_code_code
                        )
                    );
                } else if (selectedCraftId) {
                    const craftsmanUserId =
                        String(
                            payload
                                .craftsman_user_id
                            || ""
                        );

                    if (!craftsmanUserId) {
                        clearAutomaticValue(
                            craftsmanUser
                        );
                        setAssignmentStatus(
                            payload.message
                            || (
                                "Kein Handwerker "
                                + "automatisch ermittelt. "
                                + "Bitte manuell "
                                + "auswählen."
                            )
                        );
                    } else {
                        const hasCraftsmanOption =
                            Array.from(
                                craftsmanUser.options
                            ).some((option) => {
                                return (
                                    option.value
                                    === craftsmanUserId
                                );
                            });

                        if (!hasCraftsmanOption) {
                            throw new Error(
                                "Der automatisch "
                                + "ermittelte Handwerker "
                                + "ist nicht in der "
                                + "Auswahlliste verfügbar."
                            );
                        }

                        applyAutomaticValue(
                            craftsmanUser,
                            craftsmanUserId
                        );

                        setAssignmentStatus(
                            "Automatischer Vorschlag: "
                            + payload.craftsman_name
                            + " · Buchwerk "
                            + payload
                                .company_code_code
                        );
                    }
                } else {
                    clearAutomaticValue(
                        craftsmanUser,
                        true
                    );
                    setAssignmentStatus(
                        "Buchwerk "
                        + payload
                            .company_code_code
                        + " wurde aus dem Objekt "
                        + "ermittelt. Für den "
                        + "Handwerker bitte ein "
                        + "Gewerk wählen."
                    );
                }
            } else if (selectedCraftId) {
                const candidates =
                    filterExternalCraftsmen({
                        allowedIds: payload
                            .external_candidate_ids
                    });
                                const automaticId = String(
                    payload
                        .external_craftsman_id
                    || ""
                );

                if (
                    automaticId
                    && !externalCraftsman.value
                ) {
                    applyAutomaticValue(
                        externalCraftsman,
                        automaticId
                    );
                }

                if (!candidates.length) {
                    throw new Error(
                        "Für dieses Gewerk ist "
                        + "kein externer Handwerker "
                        + "verfügbar."
                    );
                }

                const externalName =
                    selectedExternalCraftsmanName();

                if (
                    hasExternalAssignmentRuleValue()
                ) {
                    setAssignmentStatus(
                        assignmentRuleStatusText(
                            payload.company_code_code
                        )
                    );
                } else if (externalName) {
                    setAssignmentStatus(
                        "Externer Auftragnehmer: "
                        + externalName
                        + " · Buchwerk "
                        + payload.company_code_code
                    );
                } else {
                    setAssignmentStatus(
                        `${candidates.length} externe `
                        + "Handwerker verfügbar. "
                        + "Bitte auswählen. "
                        + "Buchwerk "
                        + payload
                            .company_code_code
                    );
                }
            } else {
                filterExternalCraftsmen({
                    force: true
                });
                setAssignmentStatus(
                    "Buchwerk "
                    + payload.company_code_code
                    + " wurde aus dem Objekt "
                    + "ermittelt. Bitte ein "
                    + "Gewerk wählen."
                );
            }

            updateAssignmentControls();

        } catch (error) {
            if (
                currentRequestId
                !== assignmentRequestId
            ) {
                return;
            }

            if (
                isInternal
                && !isInternalAssignmentLocked()
                && !hasAssignmentRuleValue()
            ) {
                clearAutomaticValue(
                    craftsmanUser
                );
            }

            updateAssignmentControls();

            const message = (
                error instanceof DOMException
                && error.name === "AbortError"
            )
                ? (
                    "Die automatische Zuweisung "
                    + "hat zu lange gedauert. "
                    + "Bitte Objekt oder Gewerk "
                    + "erneut auswählen."
                )
                : (
                    error instanceof Error
                    && error.message
                )
                    ? error.message
                    : (
                        "Die automatische "
                        + "Zuweisung konnte nicht "
                        + "geladen werden."
                    );

            setAssignmentStatus(
                message,
                true
            );
        } finally {
            window.clearTimeout(timeoutId);

            if (
                assignmentAbortController
                === abortController
            ) {
                assignmentAbortController = null;
            }
        }
    }

    function updateRemoveButtons() {
        const rows = positions.querySelectorAll(
            ".position-row"
        );

        rows.forEach((row, rowIndex) => {
            const button = row.querySelector(
                ".remove-position"
            );
            const isFirstPosition = rowIndex === 0;

            button.classList.toggle(
                "d-none",
                isFirstPosition
            );
            button.disabled = isFirstPosition;
        });
    }

    function bindRemoveButton(button) {
        button.addEventListener(
            "click",
            () => {
                button
                    .closest(".position-row")
                    .remove();

                updateRemoveButtons();
                filterServicePositions();
                updateCommissionTypeOptions();
            }
        );
    }

    addPosition.addEventListener(
        "click",
        () => {
            const fragment = (
                positionTemplate.content
                    .cloneNode(true)
            );
            const button = fragment.querySelector(
                ".remove-position"
            );

            bindRemoveButton(button);
            positions.appendChild(fragment);
            updateRemoveButtons();
            filterServicePositions();
            updateCommissionTypeOptions();
            updateAllFieldStates();
        }
    );

    positions
        .querySelectorAll(
            ".remove-position"
        )
        .forEach((button) => {
            bindRemoveButton(button);
        });

    positions.addEventListener(
        "change",
        (event) => {
            if (
                event.target.name
                === "position_service_catalog_id"
            ) {
                updateCommissionTypeOptions();
            }
        }
    );

    craftActivity.addEventListener(
        "change",
        () => {
            applyCraftAssignmentDefault();
            filterServicePositions();
            applyCraftServicePositionDefault();
            updateCommissionTypeOptions();
            filterExternalCraftsmen({
                force: true
            });
            updateAssignmentControls();
            updateAssignmentPreview({
                forceCraftsman: true
            });
        }
    );

    assignmentTypeInputs.forEach(
        (input) => {
            input.addEventListener(
                "change",
                () => {
                    filterExternalCraftsmen();
                    updateAssignmentControls();
                    updateAssignmentPreview();
                }
            );
        }
    );

    function scheduleObjectSearch(
    delay = objectSearchDelayMilliseconds
) {
        window.clearTimeout(
            objectSearchTimer
        );

        objectSearchRequestId += 1;
        objectResults.replaceChildren();

        const query = objectSearch.value.trim();

        if (
            query.length
            < minimumObjectSearchLength
        ) {
            return;
        }

        const searchContext = (
            objectSearchContext.value
        );

        objectSearchTimer = window.setTimeout(
            () => {
                objectSearchTimer = null;
                loadObjects(
                    query,
                    searchContext
                );
            },
            delay
        );
    }

    objectSearch.addEventListener(
        "input",
        () => {
            scheduleObjectSearch();
        }
    );

    const retrospectiveControl = document.getElementById("retrospectiveCommission");
    if (retrospectiveControl) {
        retrospectiveControl.addEventListener("change", () => {
            const retrospective = retrospectiveCommissionSelected();
            notifyTenants.disabled = retrospective || notifyTenants.dataset.available !== "true";
            notifyTenants.value = notifyTenants.disabled ? "false" : tenantNotificationPreference;
            updateFieldState(notifyTenants);
            const completionChoice = document.getElementById("notifyOnCompletion");
            if (completionChoice) {
                completionChoice.disabled = retrospective;
                if (retrospective) completionChoice.checked = false;
            }
        });
    }

    objectSearchContext.addEventListener(
        "change",
        () => {
            scheduleObjectSearch(0);
        }
    );

    objectSearch.addEventListener(
        "keydown",
        (event) => {
            if (
                event.key !== "Enter"
                || event.isComposing
            ) {
                return;
            }

            event.preventDefault();
            window.clearTimeout(
                objectSearchTimer
            );
            objectSearchTimer = null;

            const query = objectSearch.value.trim();

            if (
                query.length
                < minimumObjectSearchLength
            ) {
                return;
            }

            loadObjects(
                query,
                objectSearchContext.value
            );
        }
    );

    const commissionType = document.getElementById(
        "commissionType"
    );

    const availableCommissionTypeIds = new Set(
        Array.from(commissionType.options)
            .map((option) => option.value)
            .filter(Boolean)
    );

    function updateCommissionTypeOptions() {
        const selectedOptions = Array.from(
            positions.querySelectorAll(
                '[name="position_service_catalog_id"]'
            )
        )
            .map((select) => select.selectedOptions[0])
            .filter((option) => {
                return option && option.value;
            });

        let compatibleTypeIds = new Set(
            availableCommissionTypeIds
        );

        selectedOptions.forEach((option) => {
            let mappedTypeIds = [];

            try {
                mappedTypeIds = JSON.parse(
                    option.dataset.commissionTypeIds
                    || "[]"
                ).map(String);
            } catch {
                mappedTypeIds = [];
            }

            mappedTypeIds = mappedTypeIds.filter(
                (typeId) => {
                    return availableCommissionTypeIds.has(
                        typeId
                    );
                }
            );

            if (mappedTypeIds.length) {
                const mappedTypeIdSet = new Set(
                    mappedTypeIds
                );

                compatibleTypeIds = new Set(
                    Array.from(compatibleTypeIds)
                        .filter((typeId) => {
                            return mappedTypeIdSet.has(typeId);
                        })
                );
            }
        });

        Array.from(commissionType.options)
            .forEach((option) => {
                option.disabled = Boolean(
                    option.value
                    && !compatibleTypeIds.has(
                        option.value
                    )
                );
            });

        if (
            commissionType.value
            && compatibleTypeIds.has(
                commissionType.value
            )
        ) {
            return;
        }

        if (compatibleTypeIds.size === 1) {
            commissionType.value = (
                Array.from(compatibleTypeIds)[0]
            );
            return;
        }

        if (
            defaultCommissionTypeId !== "0"
            && compatibleTypeIds.has(
                defaultCommissionTypeId
            )
        ) {
            commissionType.value = (
                defaultCommissionTypeId
            );
            return;
        }

        commissionType.value = "";
    }

        updateRemoveButtons();
    filterServicePositions();
    applyCraftServicePositionDefault();
    updateCommissionTypeOptions();

    submitCommission.disabled = (
        !canCreate
        || !objectId.value
        || !ticketAttachmentsReady()
    );

    commissionForm.addEventListener("ticket-attachments-updated", () => {
        submitCommission.disabled = !canCreate || !objectId.value || !ticketAttachmentsReady();
    });

    filterExternalCraftsmen();
    updateAssignmentControls();

    if (initialObject) {
        objectSearchContext.value = {
            license_agreement: "agreement",
            use_unit: "use_unit",
            building: "address",
            economic_unit: "economic_unit"
        }[initialObject.object_type] || "tenant";
        selectObject(initialObject, false);
    } else {
        updateTenantNotificationOptions(null);
        updateAssignmentPreview();
    }
    updateAllFieldStates();
});
