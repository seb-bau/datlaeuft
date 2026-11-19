document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("commissionForm");
    const modal = document.getElementById("otherAffectedModal");
    if (!form || !modal) return;

    const reporterField = document.getElementById("reporterField");
    const reporter = document.getElementById("reporter");
    const affectedField = document.getElementById("otherAffectedField");
    const saved = document.getElementById("otherAffectedValue");
    const button = document.getElementById("otherAffectedButton");
    const icon = document.getElementById("otherAffectedIcon");
    const editor = document.getElementById("affectedEditor");
    const search = document.getElementById("affectedSearch");
    const context = document.getElementById("affectedSearchContext");
    const results = document.getElementById("affectedResults");
    const status = document.getElementById("affectedContactStatus");
    const apply = document.getElementById("applyAffected");
    const sourceContext = document.getElementById("objectSearchContext");
    context.replaceChildren(...Array.from(sourceContext.options, option => option.cloneNode(true)));
    let active = false;
    let session = 0;
    let revision = 0;
    let timer = null;
    const pending = new Set();
    const added = new Set();

    function updateFields() {
        const type = document.getElementById("objectType").value;
        const showReporter = ["building", "economic_unit"].includes(type);
        const showAffected = ["license_agreement", "use_unit"].includes(type);
        reporterField.classList.toggle("d-none", !showReporter);
        reporter.disabled = !showReporter;
        affectedField.classList.toggle("d-none", !showAffected);
        saved.disabled = !showAffected;
        const filled = Boolean(saved.value.trim());
        button.classList.toggle("btn-success", filled);
        button.classList.toggle("btn-outline-primary", !filled);
        icon.className = `bi ${filled ? "bi-check-circle-fill" : "bi-people"} me-1`;
        button.title = filled ? "Informationen hinterlegt" : "Keine Informationen hinterlegt";
        button.setAttribute("aria-label", `Weitere Betroffene: ${button.title}`);
    }

    function message(text, error = false) {
        status.textContent = text;
        status.classList.toggle("text-danger", error);
    }

    async function appendAgreement(item, resultButton) {
        if (item.object_type !== "license_agreement" || pending.has(item.object_id) || added.has(item.object_id)) return;
        const currentSession = session;
        pending.add(item.object_id);
        resultButton.disabled = true;
        apply.disabled = true;
        message("Mieterkontakte werden geladen …");
        try {
            const url = form.dataset.affectedContactsUrl.replace(/\/0\/affected-contacts$/, `/${item.object_id}/affected-contacts`);
            const response = await fetch(url);
            const payload = await response.json();
            if (!active || currentSession !== session) return;
            if (!response.ok || typeof payload.text !== "string" || !payload.text.trim()) {
                throw new Error(payload.message || "Die Mieterkontakte konnten nicht geladen werden.");
            }
            const existing = editor.value.trimEnd();
            editor.value = existing ? `${existing}\n-----\n${payload.text}` : payload.text;
            added.add(item.object_id);
            message("Mietvertrag angehängt. Sie können den Text bearbeiten.");
        } catch (error) {
            if (active && currentSession === session) {
                resultButton.disabled = false;
                message(error instanceof Error ? error.message : "Die Mieterkontakte konnten nicht geladen werden.", true);
            }
        } finally {
            if (currentSession === session) {
                pending.delete(item.object_id);
                apply.disabled = pending.size > 0;
            }
        }
    }

    async function loadResults(query, searchContext, currentRevision) {
        const loading = document.createElement("div");
        loading.className = "list-group-item text-body-secondary";
        loading.textContent = "Suche läuft …";
        results.replaceChildren(loading);
        try {
            const url = new URL(form.dataset.objectSearchUrl, window.location.origin);
            url.searchParams.set("q", query);
            url.searchParams.set("context", searchContext);
            const response = await fetch(url.toString());
            const payload = await response.json();
            if (!active || currentRevision !== revision) return;
            if (!response.ok) throw new Error(payload.message || "Die Suche ist fehlgeschlagen.");
            results.replaceChildren();
            if (!payload.items.length) {
                loading.textContent = "Keine passenden Objekte gefunden.";
                results.appendChild(loading);
            }
            for (const item of payload.items) {
                const resultButton = document.createElement("button");
                resultButton.type = "button";
                resultButton.className = "list-group-item list-group-item-action py-2";
                resultButton.disabled = item.object_type !== "license_agreement" || pending.has(item.object_id) || added.has(item.object_id);
                const title = document.createElement("div");
                title.className = "fw-semibold";
                title.textContent = item.label;
                const details = document.createElement("div");
                details.className = "small text-body-secondary";
                details.textContent = [
                    item.license_agreement?.id_num ? `Vertrag ${item.license_agreement.id_num}` : null,
                    item.use_unit?.address || item.building?.address || item.economic_unit?.name,
                    item.use_unit?.description,
                    item.is_future && item.license_agreement?.start_date ? `Vertrag ab ${item.license_agreement.start_date.split("-").reverse().join(".")}` : null,
                ].filter(Boolean).join(" · ");
                resultButton.append(title, details);
                resultButton.addEventListener("click", () => appendAgreement(item, resultButton));
                results.appendChild(resultButton);
            }
        } catch (error) {
            if (!active || currentRevision !== revision) return;
            loading.className = "list-group-item text-danger";
            loading.textContent = error instanceof Error ? error.message : "Die Suche ist fehlgeschlagen.";
            results.replaceChildren(loading);
        }
    }

    function scheduleSearch() {
        clearTimeout(timer);
        const currentRevision = ++revision;
        results.replaceChildren();
        const query = search.value.trim();
        if (active && query.length >= 2) {
            timer = setTimeout(() => loadResults(query, context.value, currentRevision), 300);
        }
    }

    modal.addEventListener("show.bs.modal", () => {
        active = true;
        session += 1;
        editor.value = saved.value;
        search.value = "";
        context.value = sourceContext.value;
        results.replaceChildren();
        pending.clear();
        added.clear();
        apply.disabled = false;
        message("");
    });
    modal.addEventListener("shown.bs.modal", () => search.focus());
    modal.addEventListener("hide.bs.modal", () => {
        active = false;
        session += 1;
        revision += 1;
        clearTimeout(timer);
    });
    apply.addEventListener("click", () => {
        if (pending.size) return;
        saved.value = editor.value.trim();
        updateFields();
        bootstrap.Modal.getInstance(modal).hide();
    });
    search.addEventListener("input", scheduleSearch);
    context.addEventListener("change", scheduleSearch);
    document.getElementById("objectSummary").addEventListener("commission-object-selected", updateFields);
    updateFields();
});
