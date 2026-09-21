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
    const assignmentRuleLabel = String(
        commissionForm.dataset.assignmentRuleLabel
        || ""
    );
    const assignmentLocked = (
        commissionForm.dataset.assignmentLocked
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
    const assignmentStatus = document.getElementById(
        "assignmentStatus"
    );
        const automaticFieldClass =
        "field-state-automatic";
    const requiredFieldClass =
        "field-state-required";

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
    const minimumObjectSearchLength = 2;
    const objectSearchDelayMilliseconds = 300;
    let objectSearchTimer = null;
    let objectSearchRequestId = 0;

    function objectText(item) {
        const values = [
            item.tenant_name,
            item.is_vacancy
                ? "Leerstand"
                : null,
            item.license_agreement?.id_num
                ? `Vertrag ${item.license_agreement.id_num}`
                : null,
            item.use_unit?.id_num
                ? `Nutzungseinheit ${item.use_unit.id_num}`
                : null,
            item.building?.address,
            item.building?.id_num
                ? `Gebäude ${item.building.id_num}`
                : null,
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
            && !assignmentLocked
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
        heading.className = "fw-semibold text-truncate";
        heading.textContent = item.label;

        const details = document.createElement(
            "div"
        );
        details.className = "text-muted text-truncate";
        details.textContent = [
            item.license_agreement?.id_num
                ? `Vertrag ${item.license_agreement.id_num}`
                : null,
            item.use_unit?.id_num
                ? `Nutzungseinheit ${item.use_unit.id_num}`
                : null,
            item.building?.address,
            item.building?.id_num
                ? `Gebäude ${item.building.id_num}`
                : null,
        ]
            .filter(Boolean)
            .join(" · ");

        const economicUnit = [
            item.economic_unit?.id_num,
            item.economic_unit?.name,
        ]
            .filter(Boolean)
            .join(" · ");

        const metadata = document.createElement(
            "div"
        );
        metadata.className = "text-muted text-truncate";
        metadata.textContent = [
            economicUnit
                ? `WE ${economicUnit}`
                : null,
            item.company_code?.code
                ? `Buchwerk ${item.company_code.code}`
                : null,
        ]
            .filter(Boolean)
            .join(" · ");

        objectSummary.appendChild(heading);

        if (details.textContent) {
            objectSummary.appendChild(details);
        }

        if (metadata.textContent) {
            objectSummary.appendChild(metadata);
        }

        objectResults.replaceChildren();
        submitCommission.disabled = (
            !canCreate
            || !objectId.value
        );

        updateAssignmentPreview({
            forceCraftsman: (
                resetAssignments
                && !hasAssignmentRuleValue()
                && !assignmentLocked
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

            payload.items.forEach((item) => {
                const button = document.createElement(
                    "button"
                );
                button.type = "button";
                button.className = (
                    "list-group-item "
                    + "list-group-item-action py-2"
                );

                const title = document.createElement(
                    "div"
                );
                title.className = "fw-semibold";
                title.textContent = item.label;

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

    function assignmentRuleStatusText(
        companyCodeValue = ""
    ) {
        const modeText = assignmentLocked
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

    function updateAssignmentControls() {
        const hasObject = Boolean(
            objectType.value
            && objectId.value
        );
        const hasCraft = Boolean(
            craftActivity.value
        );

        companyCode.disabled = !hasObject;
        craftsmanUser.disabled = (
            assignmentLocked
            || !(
                hasObject
                && hasCraft
            )
        );
    }

    async function updateAssignmentPreview({
        forceCraftsman = false
        } = {}) {
        const currentRequestId = (
            ++assignmentRequestId
        );
        const selectedCraftId = (
            craftActivity.value
        );
        const hasObject = Boolean(
            objectType.value
            && objectId.value
        );

        if (forceCraftsman && !assignmentLocked) {
            clearAutomaticValue(
                craftsmanUser,
                true
            );
        }

        if (!hasObject) {
            clearAutomaticValue(
                companyCode,
                true
            );

            if (
                !assignmentLocked
                && !hasAssignmentRuleValue()
            ) {
                clearAutomaticValue(
                    craftsmanUser,
                    true
                );
            }

            updateAssignmentControls();

            setAssignmentStatus(
                hasAssignmentRuleValue()
                    ? (
                        assignmentRuleStatusText()
                        + ". Bitte zusätzlich ein "
                        + "Objekt wählen."
                    )
                    : (
                        "Bitte ein Objekt wählen. "
                        + "Das Gewerk kann unabhängig "
                        + "davon ausgewählt werden."
                    )
            );
            return;
        }

        if (
            !selectedCraftId
            && !assignmentLocked
            && !hasAssignmentRuleValue()
        ) {
            clearAutomaticValue(
                craftsmanUser,
                true
            );
        }

        updateAssignmentControls();

        const parameters = new URLSearchParams({
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
                ? "Automatische Zuweisung wird ermittelt …"
                : "Buchwerk wird aus dem Objekt ermittelt …"
        );

        try {
            const previewUrl = new URL(
                assignmentPreviewUrl,
                window.location.origin
            );
            previewUrl.search = parameters.toString();

            const response = await fetch(
                previewUrl.toString()
            );
            const payload = await response.json();

            if (
                currentRequestId
                !== assignmentRequestId
            ) {
                return;
            }

            if (payload.company_code_erp_id) {
                applyAutomaticValue(
                    companyCode,
                    String(payload.company_code_erp_id)
                );
            }

            if (!response.ok) {
                throw new Error(
                    payload.message
                    || (
                        "Die automatische Zuweisung "
                        + "ist fehlgeschlagen."
                    )
                );
            }

            if (
                assignmentLocked
                || hasAssignmentRuleValue()
            ) {
                setAssignmentStatus(
                    assignmentRuleStatusText(
                        payload.company_code_code
                    )
                );
            } else if (selectedCraftId) {
                applyAutomaticValue(
                    craftsmanUser,
                    String(
                        payload.craftsman_user_id
                        || ""
                    )
                );
                setAssignmentStatus(
                    "Automatischer Vorschlag: "
                    + payload.craftsman_name
                    + " · Buchwerk "
                    + payload.company_code_code
                );
            } else {
                clearAutomaticValue(
                    craftsmanUser,
                    true
                );
                setAssignmentStatus(
                    "Buchwerk "
                    + payload.company_code_code
                    + " wurde aus dem Objekt ermittelt. "
                    + "Für den Handwerker bitte "
                    + "ein Gewerk wählen."
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
                !assignmentLocked
                && !hasAssignmentRuleValue()
            ) {
                clearAutomaticValue(craftsmanUser);
            }

            updateAssignmentControls();

            const message = (
                error instanceof Error
                && error.name === "Error"
                && error.message
            )
                ? error.message
                : (
                    "Die automatische Zuweisung "
                    + "konnte nicht geladen werden."
                );

            setAssignmentStatus(
                message,
                true
            );
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
            filterServicePositions();
            updateCommissionTypeOptions();
            updateAssignmentPreview({
                forceCraftsman: true
            });
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
    updateCommissionTypeOptions();

    submitCommission.disabled = (
        !canCreate
        || !objectId.value
    );

    if (initialObject) {
        selectObject(initialObject, false);
    } else {
        updateAssignmentPreview();
    }
    updateAllFieldStates();
});