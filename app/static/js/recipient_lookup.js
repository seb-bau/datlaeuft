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
    let agreementId = isOffer
      ? search.dataset.agreementId || ""
      : agreementInput.value;
    let searchTimer;
    let searchController;
    let recipientController;
    let recipientLoading = false;
    let recipientCandidates = [];

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
      return data.items;
    }

    function applyRecipient(candidate) {
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
      candidateContainer.classList.add("d-none");
      candidateSelect.replaceChildren();
      recipientCandidates = [];
      showMessage("Anschrift wird geladen …");

      try {
        const url = new URL(search.dataset.recipientUrl, window.location.origin);
        url.searchParams.set("license_agreement_id", selectedAgreementId);
        const items = await readJSON(
          url,
          activeController.signal,
          "Die Anschrift konnte nicht aus Wowiport geladen werden."
        );
        if (activeController.signal.aborted || agreementId !== selectedAgreementId) {
          return;
        }
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
        candidateContainer.classList.remove("d-none");
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
      personInput.value = "";
      if (isOffer) {
        document.getElementById("objectType").value = result.object_type || "";
        document.getElementById("objectId").value = result.object_id || "";
        selected.textContent = label;
      } else {
        agreementInput.value = agreementId;
        const tenantName = result.tenant_name || result.label || "Mieter";
        selected.textContent = agreement.id_num
          ? "Vertrag " + agreement.id_num + " · " + tenantName
          : tenantName;
      }
      results.replaceChildren();
      candidateContainer.classList.add("d-none");
      candidateSelect.replaceChildren();
      recipientCandidates = [];
      showMessage("");
      markChanged();
      if (agreementId) {
        applyRecipient({});
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
        const items = await readJSON(
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
          const label = [
            result.tenant_name || result.label || "Mieter",
            agreement.id_num ? "Vertrag " + agreement.id_num : "",
            economicUnit.id_num ? "WE " + economicUnit.id_num : "",
            isOffer ? building.address || "" : ""
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
      if (candidateSelect.value === "") return;
      const candidate = recipientCandidates[Number(candidateSelect.value)];
      if (candidate) applyRecipient(candidate);
    });
    form.addEventListener("submit", event => {
      if (!recipientLoading) return;
      event.preventDefault();
      showMessage("Die Anschrift wird noch geladen. Bitte kurz warten.");
      markChanged();
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
      candidateContainer.classList.add("d-none");
      candidateSelect.replaceChildren();
      recipientCandidates = [];
      showMessage("");
      markChanged();
    });
  });
});