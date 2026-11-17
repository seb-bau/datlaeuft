const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/commission_form.js"), "utf8");
const update = source.match(/    function updateArticleNumbers\(\) \{[\s\S]*?\n    \}/)[0];

function updateNumber(number, edited, code) {
  const input = {value: number, dataset: {articleNumberEdited: String(edited)}};
  const row = {querySelector: selector => selector.includes("position_article_number") ? input : {selectedOptions: [{dataset: {articleNumber: code}}]}};
  vm.runInNewContext(update + "\nupdateArticleNumbers();", {positions: {querySelectorAll: () => [row]}});
  return input.value;
}

test("selecting a service position supplies its article number", () => {
  assert.equal(updateNumber("", false, "KAT-1"), "KAT-1");
  assert.equal(updateNumber("KAT-1", false, "KAT-2"), "KAT-2");
});

test("an explicitly cleared or freely edited article number stays unchanged", () => {
  assert.equal(updateNumber("", true, "KAT-1"), "");
  assert.equal(updateNumber("EIGEN-4", true, "KAT-2"), "EIGEN-4");
});
