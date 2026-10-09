document.addEventListener("DOMContentLoaded", () => {
  const modal = document.getElementById("documentArticleModal");
  if (!modal) return;

  const selection = document.getElementById("documentArticleSelection");
  const source = document.getElementById("documentArticleSource");
  const search = document.getElementById("documentArticleSearch");
  const results = document.getElementById("documentArticleResults");
  const message = document.getElementById("documentArticleMessage");
  const form = document.getElementById("documentArticleForm");
  const save = document.getElementById("documentArticleSave");
  const back = document.getElementById("documentArticleBack");
  const previous = document.getElementById("documentArticlePrevious");
  const next = document.getElementById("documentArticleNext");
  const itemType = document.getElementById("newArticleType");
  const unit = document.getElementById("newArticle_unit");
  const positions =
    document.getElementById("offerPositions") ||
    document.getElementById("invoicePositions");

  const priceFormat = new Intl.NumberFormat("de-DE", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4
  });

  let page = 1;
  let controller;
  let searchTimer;
  let saving = false;
  let itemTypes = [];

  function showError(text) {
    message.textContent = text;
    message.hidden = !text;
  }

  function endpoint() {
    const url = new URL(
      modal.dataset.endpoint,
      window.location.origin
    );

    url.searchParams.set("source", source.value);
    url.searchParams.set("q", search.value.trim());
    url.searchParams.set("page", String(page));

    return url;
  }

  async function readResponse(response) {
    const data = await response.json().catch(() => ({}));

    if (!response.ok || response.redirected) {
      throw new Error(
        data.error ||
        "Die Anfrage ist fehlgeschlagen. Bitte erneut versuchen."
      );
    }

    return data;
  }

  function addArticle(item) {
    createPositionRow({
      sourceType: item.source_type,
      taskTemplateId: "",
      description: item.name
    });

    const row = positions.lastElementChild;
    const values = {
      position_inventory_item_id:
        item.source_type === "material" ? item.id : "",
      position_local_catalog_item_id:
        item.source_type === "local_catalog" ? item.id : "",
      position_unit: item.unit,
      position_unit_price_net: item.price_net,
      position_vat_rate: item.vat_rate,
      position_source_data: JSON.stringify({
        article_number: item.item_number,
        business_area: item.business_area,
        product_group: item.product_group,
        tax_label: item.tax_label
      })
    };

    for (const [name, value] of Object.entries(values)) {
      row.querySelector(`[name="${name}"]`).value = String(value);
    }

    row.querySelector(".badge").textContent =
      item.source_type === "material"
        ? "Material"
        : "Lokaler Katalog";

    row.dispatchEvent(new Event("input", {bubbles: true}));
    bootstrap.Modal.getOrCreateInstance(modal).hide();
  }

  async function loadArticles() {
    controller?.abort();

    const activeController = new AbortController();
    controller = activeController;

    previous.disabled = true;
    next.disabled = true;
    results.replaceChildren();

    const loadingRow = results.insertRow();
    const loadingCell = loadingRow.insertCell();
    loadingCell.colSpan = 10;
    loadingCell.textContent = "Artikel werden geladen …";

    showError("");

    try {
      const response = await fetch(endpoint(), {
        signal: activeController.signal,
        headers: {Accept: "application/json"}
      });

      const data = await readResponse(response);

      if (activeController.signal.aborted) return;

      itemTypes = data.item_types;
      results.replaceChildren();

      for (const item of data.items) {
        const row = results.insertRow();
        const cells = [
          item.item_number,
          item.name,
          item.business_area,
          item.product_group,
          item.unit,
          priceFormat.format(Number(item.price_net)),
          item.active ? "Ja" : "Nein",
          item.tax_label,
          `${priceFormat.format(Number(item.vat_rate))} %`
        ];

        cells.forEach(value => {
          row.insertCell().textContent = value;
        });

        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-sm btn-outline-primary";
        button.textContent = "Übernehmen";
        button.addEventListener("click", () => addArticle(item));

        row.insertCell().appendChild(button);
      }

      if (!data.items.length) {
        const cell = results.insertRow().insertCell();
        cell.colSpan = 10;
        cell.textContent = "Keine passenden Artikel gefunden.";
      }

      previous.disabled = page === 1;
      next.disabled = !data.has_more;

      document.getElementById("documentArticlePage").textContent =
        `Seite ${page}`;

    } catch (error) {
      if (activeController.signal.aborted) return;

      results.replaceChildren();
      showError(error.message);
    }
  }

  function showSelection() {
    form.hidden = true;
    selection.hidden = false;

    document.getElementById("documentArticleTitle").textContent =
      "Artikel hinzufügen";
  }

  document.getElementById("documentArticleNew").addEventListener(
    "click",
    () => {
      showError("");
      form.reset();

      const local = source.value === "local_catalog";
      selection.hidden = true;
      form.hidden = false;

      document.getElementById("documentArticleTitle").textContent =
        local ? "Katalogartikel anlegen" : "Lagerartikel anlegen";

      const localSection = document.getElementById(
        "newArticleLocalSection"
      );

      localSection.hidden = !local;
      localSection.querySelectorAll("input").forEach(input => {
        input.disabled = !local;
      });

      document.getElementById("newArticleTypeSection").hidden = local;
      itemType.disabled = local;
      itemType.replaceChildren(new Option("Ohne Artikelart", ""));

      itemTypes.forEach(item => {
        itemType.add(new Option(item.name, String(item.id)));
      });

      unit.readOnly = false;
      document.getElementById("newArticle_item_number").focus();
    }
  );

  itemType.addEventListener("change", () => {
    const selected = itemTypes.find(
      item => item.id === Number(itemType.value)
    );

    unit.readOnly = Boolean(selected);

    if (selected) {
      unit.value = selected.unit;
    }
  });

  back.addEventListener("click", () => {
    showError("");
    showSelection();
  });

  modal.addEventListener("show.bs.modal", () => {
    showSelection();
    loadArticles();
  });

  modal.addEventListener("hide.bs.modal", event => {
    if (saving) {
      event.preventDefault();
    }
  });

  source.addEventListener("change", () => {
    page = 1;
    loadArticles();
  });

  search.addEventListener("input", () => {
    clearTimeout(searchTimer);

    searchTimer = setTimeout(() => {
      page = 1;
      loadArticles();
    }, 250);
  });

  previous.addEventListener("click", () => {
    page -= 1;
    loadArticles();
  });

  next.addEventListener("click", () => {
    page += 1;
    loadArticles();
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();

    if (saving) return;

    saving = true;
    save.disabled = true;
    back.disabled = true;
    showError("");

    try {
      const response = await fetch(endpoint(), {
        method: "POST",
        body: new FormData(form),
        headers: {Accept: "application/json"}
      });

      const data = await readResponse(response);
      saving = false;

      if (data.item.active) {
        addArticle(data.item);
      } else {
        showSelection();
        showError(
          "Artikel angelegt. Zur Übernahme muss der Artikel aktiv sein."
        );
      }

    } catch (error) {
      showError(error.message);

    } finally {
      saving = false;
      save.disabled = false;
      back.disabled = false;
    }
  });
});