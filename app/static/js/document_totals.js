(function () {
  function parseDecimal(value) {
    const text = String(value ?? "").trim().replaceAll(" ", "").replace(",", ".");
    if (text.length > 80) return null;
    const match = /^([+-]?)(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d{1,2}))?$/.exec(text);
    if (!match) return null;
    const fraction = match[3] ?? match[4] ?? "";
    const scale = fraction.length - Number(match[5] || 0);
    const coefficient = (match[1] === "-" ? -1n : 1n) * BigInt((match[2] || "0") + fraction);
    return {
      coefficient: scale < 0 ? coefficient * 10n ** BigInt(-scale) : coefficient,
      factor: scale < 0 ? 1n : 10n ** BigInt(scale)
    };
  }

  function roundHalfUp(numerator, denominator) {
    const sign = numerator < 0n ? -1n : 1n;
    const absolute = numerator * sign;
    return sign * (absolute / denominator + (absolute % denominator * 2n >= denominator ? 1n : 0n));
  }

  function calculateTotals(rows) {
    let net = 0n;
    let vat = 0n;
    for (const row of rows) {
      const quantity = parseDecimal(row.quantity);
      const price = parseDecimal(row.unitPriceNet);
      const rate = parseDecimal(row.vatRate);
      if (!quantity || !price || !rate) return null;
      if (rate.coefficient < 0n || rate.coefficient > 100n * rate.factor) return null;
      const lineNet = roundHalfUp(quantity.coefficient * price.coefficient * 100n, quantity.factor * price.factor);
      const lineVat = roundHalfUp(lineNet * rate.coefficient, 100n * rate.factor);
      net += lineNet;
      vat += lineVat;
    }
    return {net, vat, gross: net + vat};
  }

  function formatMoney(cents, currency) {
    const absolute = cents < 0n ? -cents : cents;
    const whole = new Intl.NumberFormat("de-DE", {maximumFractionDigits: 0}).format(absolute / 100n);
    return `${cents < 0n ? "-" : ""}${whole},${String(absolute % 100n).padStart(2, "0")} ${currency}`;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {calculateTotals, formatMoney};
  }
  if (typeof document === "undefined") return;

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-document-totals]").forEach(summary => {
      const positions = document.getElementById(summary.dataset.positionsTarget);
      if (!positions) return;
      const currency = summary.dataset.currency || "EUR";
      const error = summary.querySelector("[data-document-totals-error]");

      function updateTotals() {
        const rows = Array.from(positions.querySelectorAll(".offer-position-row, .invoice-position-row"), row => ({
          quantity: row.querySelector('[name="position_quantity"]')?.value,
          unitPriceNet: row.querySelector('[name="position_unit_price_net"]')?.value,
          vatRate: row.querySelector('[name="position_vat_rate"]')?.value
        }));
        const totals = calculateTotals(rows);
        for (const field of ["net", "vat", "gross"]) {
          const output = summary.querySelector(`[data-document-total="${field}"]`);
          const value = totals ? formatMoney(totals[field], currency) : `– ${currency}`;
          if (output && output.textContent !== value) output.textContent = value;
        }
        if (error) error.hidden = totals !== null;
      }

      positions.addEventListener("input", updateTotals);
      positions.addEventListener("change", updateTotals);
      new MutationObserver(updateTotals).observe(positions, {childList: true});
      updateTotals();
    });
  });
})();
