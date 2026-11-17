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

  const modal = document.getElementById("invoiceDeliveryModal");
  if (!modal) return;
  const client = document.getElementById("invoiceDeliveryClient");
  const system = document.getElementById("invoiceDeliverySystem");
  const official = document.getElementById("invoiceDeliveryOfficial");
  const preview = document.getElementById("invoiceDeliveryPreview");
  const body = document.getElementById("invoiceDeliveryBody");
  const resetBody = document.getElementById("invoiceDeliveryResetBody");
  const send = document.getElementById("invoiceDeliverySend");
  const message = document.getElementById("invoiceDeliveryMessage");
  const lastState = document.getElementById("invoiceDeliveryLastState");
  let requestId;
  let recipientsLoaded = false;
  let activePreview;
  let modalOpen = false;
  let sending = false;
  let pollTimer;
  let pollCount = 0;
  let templateBody = "";
  let bodyEdited = false;

  body.addEventListener("input", () => { bodyEdited = true; });
  resetBody.addEventListener("click", () => {
    if (sending || resetBody.disabled) return;
    body.value = templateBody;
    bodyEdited = false;
  });

  function feedback(text, error = false) {
    message.textContent = text;
    message.className = "small mt-3 " + (error ? "text-danger" : "text-muted");
  }

  function showState(item) {
    if (!item) return;
    const text = {
      pending: "Versand vorgemerkt", sending: "E-Mail wird versendet", sent: "E-Mail versendet",
      failed: "Versand fehlgeschlagen; das System versucht es erneut", uncertain: "Versand nicht bestätigt; bitte vor einem erneuten Versand prüfen",
      discarded: "Versand nicht ausgeführt, weil die Rechnung geändert oder storniert wurde"
    }[item.state] || "Versandstatus unbekannt";
    const detail = text + (item.recipient ? " · " + item.recipient : "") + (item.message ? " · " + item.message : "");
    lastState.textContent = detail;
    if (modalOpen) feedback(detail, ["failed", "uncertain", "discarded"].includes(item.state));
  }

  async function loadStatus(notificationId) {
    const url = new URL(modal.dataset.statusUrl, window.location.origin);
    if (notificationId) url.searchParams.set("notification_id", notificationId);
    const data = await jsonResponse(await fetch(url, {cache: "no-store", headers: {Accept: "application/json"}}));
    showState(data.item);
    if (notificationId && data.item && ["pending", "sending", "failed"].includes(data.item.state) && pollCount++ < 30) {
      pollTimer = setTimeout(() => loadStatus(notificationId).catch(error => feedback(error.message, true)), 2000);
    }
  }

  async function updateMode() {
    document.getElementById("invoiceDeliveryClientPanel").hidden = system.checked;
    document.getElementById("invoiceDeliverySystemPanel").hidden = !system.checked;
    if (!system.checked || recipientsLoaded) return;
    official.disabled = true;
    feedback("Sachbearbeiter werden geladen …");
    try {
      const data = await jsonResponse(await fetch(modal.dataset.recipientsUrl, {cache: "no-store", headers: {Accept: "application/json"}}));
      official.replaceChildren(new Option("Bitte auswählen", ""));
      data.items.forEach(item => official.add(new Option(item.name, String(item.id))));
      recipientsLoaded = true;
      feedback(data.items.length ? "" : "Keine verfügbaren Sachbearbeiter gefunden.");
    } catch (error) { feedback(error.message, true); }
    finally { official.disabled = false; }
  }

  official.addEventListener("change", async () => {
    activePreview?.abort();
    preview.hidden = true;
    send.disabled = true;
    if (!official.value) { feedback(""); return; }
    const selected = official.value;
    const controller = new AbortController();
    activePreview = controller;
    requestId = newRequestId();
    feedback("E-Mail-Adresse und Vorlage werden geladen …");
    try {
      const url = new URL(modal.dataset.previewUrl, window.location.origin);
      url.searchParams.set("official_id", selected);
      const data = await jsonResponse(await fetch(url, {signal: controller.signal, cache: "no-store", headers: {Accept: "application/json"}}));
      if (controller.signal.aborted || !modalOpen || official.value !== selected) return;
      document.getElementById("invoiceDeliveryRecipient").textContent = data.recipient;
      document.getElementById("invoiceDeliverySubject").textContent = data.subject;
      templateBody = data.body;
      if (!bodyEdited) body.value = templateBody;
      body.readOnly = false;
      resetBody.disabled = false;
      preview.hidden = false;
      send.disabled = false;
      feedback("");
    } catch (error) {
      if (!controller.signal.aborted && modalOpen && official.value === selected) feedback(error.message, true);
    }
  });

  send.addEventListener("click", async () => {
    if (sending || send.disabled || !official.value) return;
    if (!body.reportValidity()) return;
    sending = true;
    send.disabled = true;
    official.disabled = true;
    client.disabled = true;
    system.disabled = true;
    body.readOnly = true;
    resetBody.disabled = true;
    feedback("Rechnung wird zum Versand übergeben …");
    try {
      const item = await jsonResponse(await fetch(modal.dataset.sendUrl, {
        method: "POST", headers: {Accept: "application/json", "Content-Type": "application/json", "X-CSRFToken": document.getElementById("invoiceDeliveryCsrf").value},
        body: JSON.stringify({official_id: official.value, request_id: requestId, body: body.value})
      }));
      showState(item);
      clearTimeout(pollTimer);
      pollCount = 0;
      await loadStatus(item.id);
    } catch (error) { feedback(error.message, true); send.disabled = false; }
    finally {
      sending = false; official.disabled = false; client.disabled = false; system.disabled = false;
      body.readOnly = send.disabled;
      resetBody.disabled = send.disabled;
    }
  });

  client.addEventListener("change", updateMode);
  system.addEventListener("change", updateMode);
  modal.addEventListener("show.bs.modal", () => {
    modalOpen = true;
    requestId = newRequestId();
    recipientsLoaded = false;
    official.value = "";
    templateBody = "";
    bodyEdited = false;
    body.value = "";
    body.readOnly = false;
    resetBody.disabled = false;
    preview.hidden = true;
    send.disabled = true;
    feedback("");
    updateMode();
  });
  modal.addEventListener("hide.bs.modal", event => { if (sending) event.preventDefault(); });
  modal.addEventListener("hidden.bs.modal", () => { modalOpen = false; activePreview?.abort(); });
  loadStatus().catch(() => {});
});
