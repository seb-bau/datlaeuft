const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const {calculateTotals, formatMoney} = require("../app/static/js/document_totals.js");
const source = fs.readFileSync(path.join(__dirname, "../app/static/js/document_totals.js"), "utf8");

const row = (quantity = "1", unitPriceNet = "100", vatRate = "19") => ({quantity, unitPriceNet, vatRate});

test("net, VAT and gross are calculated from quantity and unit price", () => {
  assert.deepEqual(calculateTotals([row("2", "10", "19")]), {net: 2000n, vat: 380n, gross: 2380n});
});

test("comma and point decimals give the same totals", () => {
  assert.deepEqual(calculateTotals([row("2,5", "4,995", "19,00")]), {net: 1249n, vat: 237n, gross: 1486n});
  assert.deepEqual(calculateTotals([row("2.5", "4.995", "19.00")]), {net: 1249n, vat: 237n, gross: 1486n});
});

test("rounding happens per position before adding VAT and document totals", () => {
  assert.deepEqual(calculateTotals([row("1", "0.03"), row("1", "0.03")]), {net: 6n, vat: 2n, gross: 8n});
  assert.deepEqual(calculateTotals([row("1", "1.005")]), {net: 101n, vat: 19n, gross: 120n});
  assert.deepEqual(calculateTotals([row("1", "0.005", "100")]), {net: 1n, vat: 1n, gross: 2n});
});

test("decimal multiplication avoids floating point cent errors", () => {
  assert.deepEqual(calculateTotals([row("3", "0.29")]), {net: 87n, vat: 17n, gross: 104n});
  assert.deepEqual(calculateTotals([row("3.3333", "0.3333")]), {net: 111n, vat: 21n, gross: 132n});
});

test("spaces and scientific notation accepted by the backend retain their numeric value", () => {
  assert.deepEqual(calculateTotals([row("1 000", "0,10")]), {net: 10000n, vat: 1900n, gross: 11900n});
  assert.deepEqual(calculateTotals([row("1e2", "1e-1")]), {net: 1000n, vat: 190n, gross: 1190n});
});

test("mixed and zero VAT rates are combined without rounding across rates", () => {
  assert.deepEqual(calculateTotals([row("1", "100", "0"), row("1", "100", "7"), row("1", "100", "19")]), {net: 30000n, vat: 2600n, gross: 32600n});
});

test("negative unit prices round half up away from zero", () => {
  assert.deepEqual(calculateTotals([row("1", "-1.005")]), {net: -101n, vat: -19n, gross: -120n});
  assert.deepEqual(calculateTotals([row("1", "-0.005", "100")]), {net: -1n, vat: -1n, gross: -2n});
});

test("zero prices and an empty document reset every total to zero", () => {
  assert.deepEqual(calculateTotals([row("1", "0")]), {net: 0n, vat: 0n, gross: 0n});
  assert.deepEqual(calculateTotals([]), {net: 0n, vat: 0n, gross: 0n});
});

test("invalid or incomplete numeric inputs do not leave stale totals visible", () => {
  for (const field of ["quantity", "unitPriceNet", "vatRate"]) {
    for (const value of ["", "-", "NaN", "Infinity", "1.2.3", "1.000,00"]) {
      assert.equal(calculateTotals([{...row(), [field]: value}]), null, `${field}: ${value}`);
    }
  }
  assert.equal(calculateTotals([row("1", "100", "101")]), null);
  assert.equal(calculateTotals([row("1", "100", "-1")]), null);
});

test("amounts use German separators and preserve exact large cent values", () => {
  assert.equal(formatMoney(123456n, "EUR"), "1.234,56 EUR");
  assert.equal(formatMoney(-120n, "EUR"), "-1,20 EUR");
  assert.equal(formatMoney(9007199254740993n, "EUR"), "90.071.992.547.409,93 EUR");
});

class Element {
  constructor() { this.listeners = {}; this.hidden = true; this.textContent = "veraltete Summe"; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  dispatchEvent(event) { (this.listeners[event.type] || []).forEach(handler => handler(event)); }
}

function fixture(kind, initialRows) {
  const positions = new Element();
  const summary = new Element();
  summary.dataset = {positionsTarget: `${kind}Positions`, currency: "EUR"};
  const outputs = {net: new Element(), vat: new Element(), gross: new Element()};
  const error = new Element();
  const rows = initialRows.map(values => ({...values}));
  positions.querySelectorAll = () => rows.map(values => ({querySelector: selector => {
    const field = selector.includes("quantity") ? "quantity" : selector.includes("unit_price_net") ? "unitPriceNet" : "vatRate";
    return {value: values[field]};
  }}));
  summary.querySelector = selector => selector.includes("totals-error") ? error : outputs[selector.match(/="(\w+)"/)[1]];
  let mounted;
  let mutation;
  let observed;
  vm.runInNewContext(source, {
    document: {
      addEventListener: (type, handler) => { mounted = handler; },
      querySelectorAll: () => [summary],
      getElementById: id => id === `${kind}Positions` ? positions : null
    },
    MutationObserver: class {
      constructor(callback) { mutation = callback; }
      observe(target, options) { observed = {target, options}; }
    }
  });
  mounted();
  return {positions, outputs, error, rows, mutation, observed};
}

test("both document types replace server-rendered totals immediately on initialization", () => {
  for (const kind of ["offer", "invoice"]) {
    const state = fixture(kind, [row("2", "10")]);
    assert.equal(state.outputs.net.textContent, "20,00 EUR");
    assert.equal(state.outputs.vat.textContent, "3,80 EUR");
    assert.equal(state.outputs.gross.textContent, "23,80 EUR");
    assert.equal(state.error.hidden, true);
    assert.equal(state.observed.target, state.positions);
    assert.equal(state.observed.options.childList, true);
  }
});

test("typing quantity, unit price or VAT updates totals before saving or leaving the field", () => {
  for (const kind of ["offer", "invoice"]) {
    const state = fixture(kind, [row()]);
    state.rows[0].quantity = "2";
    state.positions.dispatchEvent({type: "input"});
    assert.equal(state.outputs.gross.textContent, "238,00 EUR");
    state.rows[0].unitPriceNet = "10,00";
    state.positions.dispatchEvent({type: "input"});
    assert.equal(state.outputs.gross.textContent, "23,80 EUR");
    state.rows[0].vatRate = "7";
    state.positions.dispatchEvent({type: "input"});
    assert.equal(state.outputs.gross.textContent, "21,40 EUR");
  }
});

test("adding or deleting a position updates totals including deletion of the last row", () => {
  for (const kind of ["offer", "invoice"]) {
    const state = fixture(kind, [row()]);
    state.rows.push(row("1", "50", "7"));
    state.mutation();
    assert.equal(state.outputs.gross.textContent, "172,50 EUR");
    state.rows.splice(0, 1);
    state.mutation();
    assert.equal(state.outputs.gross.textContent, "53,50 EUR");
    state.rows.length = 0;
    state.mutation();
    for (const output of Object.values(state.outputs)) assert.equal(output.textContent, "0,00 EUR");
  }
});

test("catalogue prices filled programmatically are picked up by their bubbling input event", () => {
  const state = fixture("invoice", [row("1", "0")]);
  state.rows[0].unitPriceNet = "12.9200";
  state.rows[0].vatRate = "19.00";
  state.positions.dispatchEvent({type: "input"});
  assert.equal(state.outputs.gross.textContent, "15,37 EUR");
});

test("change events also refresh totals and invalid entries recover after correction", () => {
  const state = fixture("offer", [row()]);
  state.rows[0].unitPriceNet = "";
  state.positions.dispatchEvent({type: "input"});
  assert.equal(state.outputs.gross.textContent, "– EUR");
  assert.equal(state.error.hidden, false);
  state.rows[0].unitPriceNet = "5";
  state.positions.dispatchEvent({type: "change"});
  assert.equal(state.outputs.gross.textContent, "5,95 EUR");
  assert.equal(state.error.hidden, true);
});
