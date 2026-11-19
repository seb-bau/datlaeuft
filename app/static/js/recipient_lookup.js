document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-recipient-search]").forEach(search => {
    const isOffer = search.dataset.documentType === "offer";
    const form = search.closest("form");
    const results = document.getElementById(search.id + "Results");
    const selected = document.getElementById(
      isOffer ? "selectedObject" : "selectedAgreement"
    );
    const agreementInput = document.getElementById("recipientLicenseAgreementErpId");
    const personInput = document.getElementById("recipientPersonErpId");
    const message = document.getElementById("recipientMessage");
    const candidateContainer = document.getElementById("recipientCandidateContainer");
    const candidateSelect = document.getElementById("recipientCandidateSelect");
    const pendingAgreement = document.getElementById("pendingInvoiceAgreement");
    const pendingPerson = document.getElementById("pendingInvoiceAgreementPerson");
    const saveNotice = document.getElementById("invoiceAgreementSaveNotice");
    const assignmentSummary = document.getElementById("invoiceAgreementAssignment");
    const recipientFields = document.getElementById("invoiceRecipientFields");
    const paymentSection = document.getElementById("invoicePaymentInformation");
    const finalize = document.getElementById("invoiceFinalize");
    const savedAgreementId = isOffer
      ? search.dataset.agreementId || ""
      : agreementInput.value;
    const savedAgreementLabel = selected.textContent;
    let agreementId = savedAgreementId;
    let searchTimer;
    let searchController;
    let recipientController;
    let recipientLoading = false;
    let recipientCandidates = [];
    let assignment = null;

    function markChanged() {
      form.dispatchEvent(new Event("change", {bubbles: true}));
    }

    function showMessage(text, error = false) {
      message.textContent = text;
      message.classList.toggle("text-danger", error);
      message.classList.toggle("text-muted", !error);
    }

    function showSearchMessage(text, error = false) {
      const entry = document.createElement("div");
      entry.className = "list-group-item " + (error ? "text-danger" : "text-muted");
      entry.textContent = text;
      results.replaceChildren(entry);
    }

    function resetCandidates() {
      candidateContainer.classList.add("d-none");
      candidateSelect.required = false;
      candidateSelect.replaceChildren();
      recipientCandidates = [];
    }

    function setPendingAgreement(value) {
      const pending = Boolean(value);
      pendingAgreement.value = value;
      pendingPerson.value = "";
      assignment = null;
      assignmentSummary.hidden = true;
      recipientFields.hidden = pending;
      recipientFields.disabled = pending;
      paymentSection.hidden = pending;
      finalize.disabled = pending;
    }

    function showAssignment(candidate) {
      if (!assignment) return;
      const recipient = assignment.recipient_name || candidate?.name;
      assignmentSummary.textContent = `Aussteller ${assignment.issuer_name}` +
        (recipient ? ` · Rechnungsempfänger ${recipient}` : "");
      assignmentSummary.hidden = false;
    }

    async function readJSON(url, signal, errorMessage) {
      const response = await fetch(url, {
        signal,
        headers: {Accept: "application/json"}
      }).catch(error => {
        if (error.name === "AbortError") throw error;
        throw new Error(errorMessage);
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || response.redirected || !Array.isArray(data.items)) {
        throw new Error(data.message || errorMessage);
      }
      return data;
    }

    function applyRecipient(candidate) {
      if (pendingAgreement) {
        pendingPerson.value = String(candidate.person_id || "");
        showAssignment(candidate);
        markChanged();
        return;
      }
      personInput.value = candidate.person_id || "";
      const fields = {
        recipientType: candidate.recipient_type || "private",
        recipientName: candidate.name || "",
        recipientNameAddition: candidate.name_addition || "",
        recipientStreet: candidate.street || "",
        recipientPostalCode: candidate.postal_code || "",
        recipientTown: candidate.town || "",
        recipientCountryCode: candidate.country_code || "DE",
        recipientEmail: candidate.email || "",
        recipientVatId: candidate.vat_id || ""
      };
      Object.entries(fields).forEach(([id, value]) => {
        document.getElementById(id).value = value;
      });
      markChanged();
    }

    async function loadRecipient() {
      recipientController?.abort();
      const activeController = new AbortController();
      recipientController = activeController;
      recipientLoading = true;
      const selectedAgreementId = agreementId;
      resetCandidates();
      showMessage("Anschrift wird geladen …");

      try {
        const url = new URL(search.dataset.recipientUrl, window.location.origin);
        url.searchParams.set("license_agreement_id", selectedAgreementId);
        const {items, assignment: selection} = await readJSON(
          url,
          activeController.signal,
          "Die Anschrift konnte nicht aus Wowiport geladen werden."
        );
        if (activeController.signal.aborted || agreementId !== selectedAgreementId) {
          return;
        }
        if (pendingAgreement && !selection) {
          throw new Error("Der Rechnungsentwurf wurde inzwischen geändert. Bitte laden Sie die Rechnung neu.");
        }
        assignment = selection;
        showMessage("");
        recipientCandidates = items;
        if (items.length === 1) {
          applyRecipient(items[0]);
          return;
        }
        if (!items.length) {
          showMessage("Für den Vertrag wurde keine Anschrift gefunden.", true);
          return;
        }
        candidateSelect.add(new Option("Bitte auswählen", ""));
        items.forEach((candidate, index) => {
          candidateSelect.add(new Option(
            [candidate.name, candidate.role].filter(Boolean).join(" · "),
            String(index)
          ));
        });
        candidateSelect.required = Boolean(pendingAgreement);
        candidateContainer.classList.remove("d-none");
        showAssignment();
      } catch (error) {
        if (!activeController.signal.aborted && agreementId === selectedAgreementId) {
          showMessage(error.message, true);
        }
      } finally {
        if (recipientController === activeController) recipientLoading = false;
      }
    }

    function selectResult(result, label) {
      clearTimeout(searchTimer);
      searchController?.abort();
      recipientController?.abort();
      recipientLoading = false;
      const agreement = result.license_agreement || {};
      agreementId = String(agreement.erp_id || "");
      if (!pendingAgreement) personInput.value = "";
      if (isOffer) {
        document.getElementById("objectType").value = result.object_type || "";
        document.getElementById("objectId").value = result.object_id || "";
        selected.textContent = label;
      } else {
        if (pendingAgreement) setPendingAgreement(agreementId);
        else agreementInput.value = agreementId;
        saveNotice.hidden = !agreementId || agreementId === savedAgreementId;
        const tenantName = result.tenant_name || result.label || "Mieter";
        selected.textContent = agreement.id_num
          ? "Vertrag " + agreement.id_num + " · " + tenantName
          : tenantName;
      }
      results.replaceChildren();
      resetCandidates();
      showMessage("");
      markChanged();
      if (!isOffer && !pendingAgreement) form.dispatchEvent(new CustomEvent("invoice-agreement-selected", {detail: {agreementId}, bubbles: true}));
      if (agreementId) {
        if (!pendingAgreement) applyRecipient({});
        loadRecipient();
      }
    }

    async function searchRecipients() {
      searchController?.abort();
      const query = search.value.trim();
      results.replaceChildren();
      if (query.length < 2) return;
      const activeController = new AbortController();
      searchController = activeController;
      showSearchMessage("Suche läuft …");

      try {
        const url = new URL(search.dataset.searchUrl, window.location.origin);
        url.searchParams.set("q", query);
        const {items} = await readJSON(
          url,
          activeController.signal,
          "Die Suche ist fehlgeschlagen. Bitte erneut versuchen."
        );
        if (activeController.signal.aborted || search.value.trim() !== query) return;
        results.replaceChildren();
        items.forEach(result => {
          const agreement = result.license_agreement || {};
          const economicUnit = result.economic_unit || {};
          const building = result.building || {};
          const unit = result.use_unit || {};
          const address = unit.address || building.address || economicUnit.address || "";
          const location = [unit.floor, unit.position, unit.description].filter(Boolean).filter((value, index, values) => values.indexOf(value) === index).join(" · ");
          const label = [
            result.tenant_name || result.label || "Mieter",
            agreement.id_num ? "Vertrag " + agreement.id_num : "",
            pendingAgreement ? result.company_code?.code : "",
            economicUnit.id_num ? "WE " + economicUnit.id_num : "",
            address,
            location
          ].filter(Boolean).join(" · ");
          const button = document.createElement("button");
          button.type = "button";
          button.className = "list-group-item list-group-item-action";
          button.textContent = label;
          button.addEventListener("click", () => selectResult(result, label));
          results.appendChild(button);
        });
        if (!items.length) {
          showSearchMessage(isOffer
            ? "Keine passenden Objekte gefunden."
            : "Keine passenden Verträge gefunden.");
        }
      } catch (error) {
        if (!activeController.signal.aborted && search.value.trim() === query) {
          showSearchMessage(error.message, true);
        }
      }
    }

    search.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchController?.abort();
      results.replaceChildren();
      if (pendingAgreement?.value && !search.value.trim()) {
        recipientController?.abort();
        recipientLoading = false;
        agreementId = savedAgreementId;
        setPendingAgreement("");
        saveNotice.hidden = true;
        selected.textContent = savedAgreementLabel;
        resetCandidates();
        showMessage("");
        markChanged();
      }
      if (search.value.trim().length >= 2) {
        searchTimer = setTimeout(searchRecipients, 300);
      }
    });
    search.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        clearTimeout(searchTimer);
        searchRecipients();
      }
    });
    candidateSelect.addEventListener("change", () => {
      if (candidateSelect.value === "") {
        if (pendingAgreement) applyRecipient({});
        return;
      }
      const candidate = recipientCandidates[Number(candidateSelect.value)];
      if (candidate) applyRecipient(candidate);
    });
    form.addEventListener("submit", event => {
      if (recipientLoading) {
        event.preventDefault();
        showMessage("Die Anschrift wird noch geladen. Bitte kurz warten.");
      } else if (pendingAgreement?.value && !pendingPerson.value) {
        event.preventDefault();
        showMessage("Bitte wählen Sie einen Vertragspartner aus.", true);
      }
    });
    document.getElementById("clearObjectButton")?.addEventListener("click", () => {
      clearTimeout(searchTimer);
      searchController?.abort();
      recipientController?.abort();
      recipientLoading = false;
      agreementId = "";
      document.getElementById("objectType").value = "";
      document.getElementById("objectId").value = "";
      personInput.value = "";
      search.value = "";
      selected.textContent = "Kein Objekt ausgewählt";
      results.replaceChildren();
      resetCandidates();
      showMessage("");
      markChanged();
    });
  });
});
