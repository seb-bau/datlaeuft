document.addEventListener("DOMContentLoaded", () => {
  const section = document.getElementById("invoicePaymentInformation");
  const edit = document.getElementById("invoicePaymentEdit");
  if (!section || !edit) return;
  const form = document.getElementById("invoice-draft-form");
  const input = document.getElementById("invoicePaymentData");
  const agreement = document.getElementById("recipientLicenseAgreementErpId");
  const modal = document.getElementById("invoicePaymentModal");
  const refresh = document.getElementById("invoicePaymentRefresh");
  const message = document.getElementById("invoicePaymentMessage");
  const method = document.getElementById("paymentMethod");
  const choice = document.getElementById("paymentOption");
  const errorMessage = document.getElementById("paymentEditError");
  const fields = {iban: "paymentIban", bic: "paymentBic", mandate_reference: "paymentMandateReference", creditor_id: "paymentCreditorId", account_holder: "paymentAccountHolder"};
  let value = JSON.parse(input.value);
  let options = [];
  let controller;
  let loading = false;

  function commit(next) {
    value = next;
    input.value = JSON.stringify(value);
    const sepa = value.method === "sepa_debit";
    document.getElementById("invoicePaymentMethod").textContent = sepa ? "SEPA-Mandat" : value.method === "bank_transfer" ? "Überweisung" : "Noch nicht festgelegt";
    document.getElementById("invoicePaymentAccountLabel").textContent = sepa ? "Einzugskonto" : "Zahlungskonto";
    document.getElementById("invoicePaymentIban").textContent = value.iban || "Nicht hinterlegt";
    document.getElementById("invoicePaymentBic").textContent = value.bic || "Nicht hinterlegt";
    document.getElementById("invoicePaymentMandate").textContent = value.mandate_reference || "Nicht hinterlegt";
    document.getElementById("invoicePaymentCreditor").textContent = value.creditor_id || "Nicht hinterlegt";
    section.querySelectorAll("[data-sepa-summary]").forEach(element => { element.hidden = !sepa; });
    refresh.disabled = !agreement.value || loading;
    form.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function updateMethod() {
    const sepa = method.value === "sepa_debit";
    modal.querySelectorAll("[data-sepa-field]").forEach(element => { element.hidden = !sepa; });
    document.getElementById("paymentAccountHelp").textContent = sepa
      ? "IBAN und BIC des Kontos, von dem eingezogen wird. Mandatsreferenz und Gläubiger-ID werden beim Festschreiben benötigt."
      : "IBAN und BIC des Kontos, auf das der Rechnungsempfänger überweisen soll.";
  }

  function fill(next) {
    method.value = next.method || "bank_transfer";
    Object.entries(fields).forEach(([name, id]) => { document.getElementById(id).value = next[name] || (name === "creditor_id" ? section.dataset.creditorId || "" : ""); });
    updateMethod();
  }

  function validIban(text) {
    if (!/^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$/.test(text)) return false;
    const digits = (text.slice(4) + text.slice(0, 4)).replace(/[A-Z]/g, character => String(character.charCodeAt(0) - 55));
    return [...digits].reduce((remainder, digit) => (remainder * 10 + Number(digit)) % 97, 0) === 1;
  }

  async function load() {
    controller?.abort();
    const selectedAgreement = agreement.value;
    options = [];
    if (!selectedAgreement) return;
    const active = new AbortController();
    controller = active;
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; active.abort(); }, 20000);
    loading = true;
    edit.disabled = true;
    refresh.disabled = true;
    message.className = "small mt-2 text-muted";
    message.textContent = "Zahlungsinformationen werden frisch aus Wowiport geladen …";
    commit({method: "", iban: "", bic: "", source: "unresolved", license_agreement_erp_id: Number(selectedAgreement)});
    try {
      const url = new URL(section.dataset.endpoint, window.location.origin);
      url.searchParams.set("license_agreement_id", selectedAgreement);
      const response = await fetch(url, {signal: active.signal, cache: "no-store", headers: {Accept: "application/json"}});
      const data = await response.json().catch(() => ({}));
      if (active.signal.aborted || agreement.value !== selectedAgreement) return;
      if (!response.ok || response.redirected || !data.selected || !Array.isArray(data.options)) throw new Error(data.message || "Die Zahlungsinformationen konnten nicht geladen werden.");
      options = data.options;
      commit(data.selected);
      message.textContent = data.requires_selection ? "Mehrere gültige Zahlungsmodalitäten gefunden. Bitte über „ändern“ auswählen."
        : data.selected.method === "sepa_debit" && (!data.selected.mandate_reference || !data.selected.creditor_id) ? "Bitte Mandatsreferenz und Gläubiger-ID über „ändern“ vervollständigen."
        : data.selected.source === "company" ? (data.selected.iban ? "Überweisung an das ausstellende Buchwerk." : "Für das ausstellende Buchwerk fehlt die IBAN. Bitte über „ändern“ ergänzen.")
        : !data.selected.iban ? "In Wowiport ist keine passende Bankverbindung hinterlegt. Bitte über „ändern“ ergänzen." : "Aus Wowiport übernommen.";
    } catch (error) {
      if (agreement.value !== selectedAgreement || active.signal.aborted && !timedOut) return;
      message.className = "small mt-2 text-danger";
      message.textContent = timedOut ? "Wowiport antwortet nicht rechtzeitig. Bitte erneut laden oder über „ändern“ eingeben." : error.message;
    } finally {
      clearTimeout(timeout);
      if (controller === active) {
        loading = false;
        edit.disabled = false;
        refresh.disabled = !agreement.value;
      }
    }
  }

  form.addEventListener("invoice-agreement-selected", load);
  refresh.addEventListener("click", load);
  modal.addEventListener("show.bs.modal", () => {
    errorMessage.hidden = true;
    fill(value);
    choice.replaceChildren(new Option("Manuell anpassen", ""));
    options.forEach((item, index) => choice.add(new Option(`${item.method === "sepa_debit" ? "SEPA-Mandat" : "Überweisung"} · ${item.iban || "IBAN fehlt"}${item.mandate_reference ? " · " + item.mandate_reference : ""}`, String(index))));
  });
  choice.addEventListener("change", () => { if (choice.value !== "" && options[Number(choice.value)]) fill(options[Number(choice.value)]); });
  method.addEventListener("change", () => {
    const selected = method.value;
    fill(options.find(item => item.method === selected) || {method: selected});
  });
  document.getElementById("paymentApply").addEventListener("click", () => {
    const next = {method: method.value, source: "manual", license_agreement_erp_id: agreement.value ? Number(agreement.value) : null};
    Object.entries(fields).forEach(([name, id]) => { next[name] = document.getElementById(id).value.trim(); });
    next.iban = next.iban.replace(/\s+/g, "").toUpperCase();
    next.bic = next.bic.replace(/\s+/g, "").toUpperCase();
    if (!validIban(next.iban) || next.bic && !/^[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?$/.test(next.bic)) {
      errorMessage.textContent = "Bitte eine gültige IBAN und, falls vorhanden, eine gültige BIC eingeben.";
      errorMessage.hidden = false;
      return;
    }
    controller?.abort();
    loading = false;
    commit(next);
    message.textContent = "Zahlungsinformationen für diese Rechnung angepasst.";
    message.className = "small mt-2 text-muted";
    bootstrap.Modal.getOrCreateInstance(modal).hide();
  });
  form.addEventListener("submit", event => {
    if (!loading) return;
    event.preventDefault();
    message.textContent = "Die Zahlungsinformationen werden noch geladen. Bitte kurz warten.";
  });
  refresh.disabled = !agreement.value;
  if (agreement.value && value.source === "unresolved") load();
});
