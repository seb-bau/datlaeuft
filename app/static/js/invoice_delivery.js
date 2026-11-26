document.addEventListener("DOMContentLoaded", () => {
  function newRequestId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const value = [...bytes].map(byte => byte.toString(16).padStart(2, "0")).join("");
    return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
  }
  const attachments = document.getElementById("invoiceAttachments");
  const attachmentForm = document.getElementById("invoiceAttachmentForm");
  const attachmentList = document.getElementById("invoiceAttachmentList");
  const attachmentMessage = document.getElementById("invoiceAttachmentMessage");
  let attachmentBusy = false;

  async function jsonResponse(response) {
    const data = await response.json().catch(() => ({}));
    if (!response.ok || response.redirected) throw new Error(data.message || "Der Vorgang konnte nicht ausgeführt werden. Bitte die Seite neu laden.");
    return data;
  }

  function attachmentFeedback(text, error = false) {
    attachmentMessage.textContent = text;
    attachmentMessage.className = "small mt-2 " + (error ? "text-danger" : "text-muted");
  }

  function renderAttachments(data) {
    attachmentList.replaceChildren();
    data.items.forEach(item => {
      const row = document.createElement("li");
      row.className = "list-group-item d-flex align-items-center justify-content-between gap-3 px-0";
      const link = document.createElement("a");
      link.href = attachments.dataset.documentUrl.replace(/\/0$/, "/" + item.id);
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = `${item.name} · ${item.pages} ${item.pages === 1 ? "Seite" : "Seiten"}`;
      row.appendChild(link);
      if (data.editable && attachmentForm) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-sm btn-outline-danger";
        remove.textContent = "Entfernen";
        remove.addEventListener("click", async () => {
          if (attachmentBusy) return;
          attachmentBusy = true;
          remove.disabled = true;
          try {
            await jsonResponse(await fetch(attachments.dataset.documentUrl.replace(/\/0$/, "/" + item.id), {
              method: "DELETE", headers: {"X-CSRFToken": attachmentForm.elements.csrf_token.value, Accept: "application/json"}
            }));
            await loadAttachments();
            attachmentFeedback("PDF-Anhang entfernt.");
          } catch (error) { attachmentFeedback(error.message, true); }
          finally { attachmentBusy = false; remove.disabled = false; }
        });
        row.appendChild(remove);
      }
      attachmentList.appendChild(row);
    });
    if (!data.items.length) {
      const row = document.createElement("li");
      row.className = "list-group-item text-muted px-0";
      row.textContent = "Noch keine PDF-Anhänge.";
      attachmentList.appendChild(row);
    }
  }

  async function loadAttachments() {
    renderAttachments(await jsonResponse(await fetch(attachments.dataset.endpoint, {cache: "no-store", headers: {Accept: "application/json"}})));
  }

  if (attachments) loadAttachments().catch(error => attachmentFeedback(error.message, true));
  attachmentForm?.addEventListener("submit", async event => {
    event.preventDefault();
    if (attachmentBusy) return;
    const data = new FormData(attachmentForm);
    const upload = document.getElementById("invoiceAttachmentUpload");
    const files = document.getElementById("invoiceAttachmentFiles");
    attachmentBusy = true;
    upload.disabled = true;
    files.disabled = true;
    attachmentFeedback("PDF-Dateien werden hochgeladen …");
    try {
      renderAttachments(await jsonResponse(await fetch(attachments.dataset.endpoint, {method: "POST", body: data, headers: {Accept: "application/json"}})));
      attachmentForm.reset();
      attachmentFeedback("PDF-Anhänge gespeichert.");
    } catch (error) { attachmentFeedback(error.message, true); }
    finally { attachmentBusy = false; upload.disabled = false; files.disabled = false; }
  });

  const cancelForm = document.getElementById("invoice-cancel-form");
  cancelForm?.addEventListener("submit", event => {
    const button = event.submitter;
    if (button) {
      button.disabled = true;
      button.textContent = "Stornorechnung wird erstellt …";
    }
  });

  const delivery = document.getElementById("invoiceDelivery");
  if (!delivery) return;
  const form = document.getElementById("invoiceDeliveryForm");
  const modes = document.getElementById("invoiceDeliveryModes");
  const client = document.getElementById("invoiceDeliveryClient");
  const system = document.getElementById("invoiceDeliverySystem");
  const print = document.getElementById("invoiceDeliveryPrint");
  const dms = document.getElementById("invoiceDeliveryDms");
  const dmsOptions = document.getElementById("invoiceDmsOptions");
  const postal = document.getElementById("invoiceDmsPostal");
  const portal = document.getElementById("invoiceDmsPortal");
  const postalAllowed = Boolean(postal && !postal.disabled);
  const portalAllowed = Boolean(portal && !portal.disabled);
  const official = document.getElementById("invoiceDeliveryOfficial");
  const preview = document.getElementById("invoiceDeliveryPreview");
  const body = document.getElementById("invoiceDeliveryBody");
  const resetBody = document.getElementById("invoiceDeliveryResetBody");
  const send = document.getElementById("invoiceDeliverySend");
  const message = document.getElementById("invoiceDeliveryMessage");
  let requestId;
  let recipientsLoaded = false;
  let activePreview;
  let busy = false;
  let waiting = Boolean(delivery.dataset.notificationId || modes.disabled);
  let pollCount = 0;
  let templateBody = "";
  let bodyEdited = false;

  body.addEventListener("input", () => { bodyEdited = true; });
  resetBody.addEventListener("click", () => {
    if (busy || waiting) return;
    body.value = templateBody;
    bodyEdited = false;
  });

  function feedback(text, error = false) {
    message.textContent = text;
    message.className = "small mt-3 mb-0 " + (error ? "text-danger" : "text-muted");
  }

  function updateControls() {
    modes.disabled = busy || waiting;
    if (dmsOptions) dmsOptions.disabled = busy || waiting;
    official.disabled = busy || waiting;
    body.readOnly = busy || waiting;
    resetBody.disabled = body.readOnly;
    send.disabled = busy || waiting || preview.hidden;
  }

  function showState(item) {
    if (!item) return;
    const label = {
      pending: "Versand vorgemerkt",
      sending: "E-Mail wird versendet",
      sent: item.test_delivery ? "E-Mail an Testadresse versendet. Die Rechnung bleibt offen." : "E-Mail versendet",
      failed: "Versand fehlgeschlagen. Das System versucht es erneut.",
      uncertain: "Versand nicht bestätigt. Bitte den Versand in den E-Mail-Protokollen prüfen.",
      discarded: "Versand nicht ausgeführt. Die Rechnung wurde geändert oder storniert."
    }[item.state] || "Versandstatus unbekannt";
    const detail = [label, item.recipient, item.message].filter(Boolean).join(" · ");
    feedback(detail, ["failed", "uncertain", "discarded"].includes(item.state));
    const sent = item.state === "sent" && !item.test_delivery;
    const wasWaiting = waiting;
    waiting = sent || ["pending", "sending", "failed", "uncertain"].includes(item.state);
    if (sent) window.location.assign(delivery.dataset.invoiceUrl);
    if (item.state === "sent" || item.state === "discarded") requestId = newRequestId();
    if (wasWaiting && !waiting && delivery.dataset.notificationId) updateMode();
    updateControls();
  }

  async function loadStatus(notificationId) {
    const url = new URL(delivery.dataset.statusUrl, window.location.origin);
    url.searchParams.set("notification_id", notificationId);
    const data = await jsonResponse(await fetch(url, {cache: "no-store", headers: {Accept: "application/json"}}));
    showState(data.item);
    if (data.item && ["pending", "sending", "failed"].includes(data.item.state) && pollCount++ < 60) {
      setTimeout(() => loadStatus(notificationId).catch(error => feedback(error.message, true)), 2000);
    }
  }

  async function updateMode() {
    document.getElementById("invoiceDeliveryClientPanel").hidden = !client.checked || waiting;
    document.getElementById("invoiceDeliverySystemPanel").hidden = !system.checked || waiting;
    document.getElementById("invoiceDeliveryPrintPanel").hidden = !print.checked || waiting;
    const dmsPanel = document.getElementById("invoiceDeliveryDmsPanel");
    if (dmsPanel) dmsPanel.hidden = !dms.checked || waiting;
    updateDmsSelection();
    if (!system.checked || waiting || recipientsLoaded) return;
    official.disabled = true;
    feedback("Empfänger werden geladen …");
    try {
      const data = await jsonResponse(await fetch(delivery.dataset.recipientsUrl, {cache: "no-store", headers: {Accept: "application/json"}}));
      official.replaceChildren(new Option("Bitte auswählen", ""));
      data.items.forEach(item => official.add(new Option(item.name, String(item.id))));
      recipientsLoaded = true;
      const missingRecipient = delivery.dataset.companyRecipient === "true"
        ? "Für das empfangende Buchwerk ist keine E-Mail-Adresse für Rechnungseingang hinterlegt."
        : "Keine verfügbaren Empfänger gefunden.";
      feedback(data.items.length ? "" : missingRecipient);
    } catch (error) { feedback(error.message, true); }
    finally { updateControls(); }
  }

  function updateDmsSelection() {
    if (!dmsOptions) return;
    const postalSelected = postalAllowed && postal.checked && !print.checked;
    document.getElementById("invoiceDmsPostalOption").hidden = print.checked;
    postal.disabled = !postalAllowed || print.checked;
    portal.disabled = !portalAllowed || postalSelected;
    document.getElementById("invoiceDmsPortalViaPostal").hidden = !postalSelected;
    const dmsSubmit = document.getElementById("invoiceDeliveryDmsSubmit");
    if (dmsSubmit) dmsSubmit.disabled = !postalSelected && !(portalAllowed && portal.checked);
  }

  official.addEventListener("change", async () => {
    activePreview?.abort();
    preview.hidden = true;
    updateControls();
    if (!official.value) { feedback(""); return; }
    const selected = official.value;
    const controller = new AbortController();
    activePreview = controller;
    requestId = newRequestId();
    feedback("E-Mail-Adresse und Vorlage werden geladen …");
    try {
      const url = new URL(delivery.dataset.previewUrl, window.location.origin);
      url.searchParams.set("official_id", selected);
      const data = await jsonResponse(await fetch(url, {signal: controller.signal, cache: "no-store", headers: {Accept: "application/json"}}));
      if (controller.signal.aborted || official.value !== selected) return;
      document.getElementById("invoiceDeliveryRecipient").textContent = data.recipient;
      document.getElementById("invoiceDeliverySubject").textContent = data.subject;
      templateBody = data.body;
      if (!bodyEdited) body.value = templateBody;
      preview.hidden = false;
      updateControls();
      feedback("");
    } catch (error) {
      if (!controller.signal.aborted && official.value === selected) feedback(error.message, true);
    }
  });

  send.addEventListener("click", async () => {
    if (busy || waiting || send.disabled || !official.value || !body.reportValidity()) return;
    busy = true;
    updateControls();
    feedback("Rechnung wird zum Versand übergeben …");
    try {
      const item = await jsonResponse(await fetch(delivery.dataset.sendUrl, {
        method: "POST", headers: {Accept: "application/json", "Content-Type": "application/json", "X-CSRFToken": document.getElementById("invoiceDeliveryCsrf").value},
        body: JSON.stringify({
          official_id: official.value, request_id: requestId, body: body.value, complete_invoice: true,
          postal: Boolean(postal?.checked && !postal.disabled),
          portal: Boolean(portal?.checked && !portal.disabled),
        })
      }));
      showState(item);
      pollCount = 0;
      if (waiting && !["sent", "uncertain"].includes(item.state)) await loadStatus(item.id);
    } catch (error) { feedback(error.message, true); }
    finally {
      busy = false;
      updateControls();
    }
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || waiting || system.checked) return;
    const data = new FormData(form);
    busy = true;
    updateControls();
    if (event.submitter) event.submitter.disabled = true;
    feedback(dms?.checked ? "Rechnung wird zur DMS-Übergabe vorgemerkt …" : "Datei wird erstellt …");
    try {
      const response = await fetch(form.action, {method: "POST", body: data});
      if (dms?.checked) {
        await jsonResponse(response);
        waiting = true;
        window.location.assign(delivery.dataset.invoiceUrl);
        return;
      }
      const contentType = response.headers.get("Content-Type") || "";
      if (!response.ok || response.redirected || !["application/pdf", "message/rfc822"].some(type => contentType.includes(type))) {
        const error = contentType.includes("application/json") ? await response.json() : {};
        throw new Error(error.message || "Die Datei konnte nicht heruntergeladen werden.");
      }
      const filename = response.headers.get("Content-Disposition")?.match(/filename="?([^";]+)/i)?.[1];
      const fileUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = fileUrl;
      link.download = filename || (print.checked ? "Rechnung.pdf" : "Rechnung.eml");
      document.body.appendChild(link);
      link.click();
      link.remove();
      waiting = true;
      feedback("Datei heruntergeladen. Die Rechnung wurde abgeschlossen.");
      setTimeout(() => {
        URL.revokeObjectURL(fileUrl);
        window.location.assign(delivery.dataset.invoiceUrl);
      }, 1000);
    } catch (error) { feedback(error.message, true); }
    finally {
      busy = false;
      if (event.submitter) event.submitter.disabled = waiting;
      updateControls();
    }
  });

  [client, system, print, dms].forEach(mode => mode?.addEventListener("change", updateMode));
  [postal, portal].forEach(option => option?.addEventListener("change", updateDmsSelection));
  updateMode();
  if (delivery.dataset.notificationId) loadStatus(delivery.dataset.notificationId).catch(error => feedback(error.message, true));
});
