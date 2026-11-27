(() => {
    const fields = document.getElementById("tenantChargeFields");
    if (!fields) return;

    const orderType = document.getElementById("commissionType");
    const objectType = document.getElementById("objectType");
    const objectId = document.getElementById("objectId");
    const current = document.getElementById("tenantChargeCurrent");
    const recipient = document.getElementById("tenantChargeRecipient");
    const notice = document.getElementById("tenantChargeNotice");
    const retry = document.getElementById("tenantChargeRetry");
    let objectKey = "";
    let requestKey = "";
    let selected = fields.dataset.selection;
    let activeRequest = null;

    function updateSelection() {
        if (current.checked) recipient.value = "";
        recipient.disabled = current.checked && !current.disabled;
    }

    async function refresh() {
        const enabled = orderType.selectedOptions[0]?.dataset.tenantCharge === "true";
        const key = `${objectType.value}:${objectId.value}`;
        if (objectKey && key !== objectKey) {
            current.checked = false;
            selected = "";
        }
        objectKey = key;
        fields.hidden = !enabled;
        fields.disabled = !enabled;

        const nextRequestKey = enabled ? `${orderType.value}:${key}` : "";
        if (nextRequestKey === requestKey) return;
        requestKey = nextRequestKey;
        activeRequest?.abort();
        current.disabled = true;
        recipient.replaceChildren(new Option("Bitte auswählen", ""));
        retry.hidden = true;
        updateSelection();
        if (!enabled) return;
        if (!objectId.value || !["license_agreement", "use_unit"].includes(objectType.value)) {
            notice.textContent = "Bitte zuerst eine Wohnung oder einen Mietvertrag auswählen.";
            return;
        }

        const controller = new AbortController();
        activeRequest = controller;
        const params = new URLSearchParams({
            object_type: objectType.value,
            object_id: objectId.value,
            commission_type_id: orderType.value,
        });
        notice.textContent = "Mietverträge werden geladen …";
        try {
            const response = await fetch(`${fields.dataset.url}?${params}`, {signal: controller.signal});
            const payload = await response.json();
            if (controller.signal.aborted) return;
            if (!response.ok) throw new Error(payload.message || "Die Mietverträge konnten nicht geladen werden.");
            current.disabled = !payload.current_available;
            if (current.disabled) current.checked = false;
            for (const item of payload.items) {
                const label = item.suggested ? `${item.label} · Vorschlag Vormieter` : item.label;
                recipient.add(new Option(label, item.value));
            }
            recipient.value = payload.items.some(item => item.value === selected) ? selected : "";
            updateSelection();
            notice.textContent = payload.items.length ? "" : "Für diese Wohnung sind keine Vertragsnehmer verfügbar.";
        } catch (error) {
            if (controller.signal.aborted) return;
            notice.textContent = error.message;
            retry.hidden = false;
        }
    }

    orderType.addEventListener("change", refresh);
    document.getElementById("objectSummary").addEventListener("commission-object-selected", refresh);
    current.addEventListener("change", () => {
        updateSelection();
        selected = recipient.value;
    });
    recipient.addEventListener("change", () => { selected = recipient.value; });
    retry.addEventListener("click", () => {
        requestKey = "";
        refresh();
    });
    refresh();
})();
