const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/commission_form.js"), "utf8");

function functionSource(name) {
    let start = source.indexOf(`function ${name}(`);
    assert.notEqual(start, -1);
    if (source.slice(start - 6, start) === "async ") {
        start -= 6;
    }
    const remainder = source.slice(start);
    const next = /\n +(?:async )?function /.exec(remainder);
    return next ? remainder.slice(0, next.index) : remainder;
}

class Element {
    constructor(tag = "div") {
        this.tag = tag;
        this.children = [];
        this.listeners = {};
        this.dataset = {};
        this.classList = {add() {}, remove() {}};
        this.value = "";
        this._text = "";
    }
    set value(value) { this._value = String(value); }
    get value() { return this._value; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    appendChild(child) { this.children.push(child); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this._text = ""; this.children = children; }
    addEventListener(name, handler) { this.listeners[name] = handler; }
    click() { this.listeners.click(); }
}

function form(items = []) {
    const sandbox = {
        document: {createElement: tag => new Element(tag)},
        window: {location: {origin: "https://example.invalid"}},
        URL, canCreate: true, objectSearchUrl: "/objects/search", objectSearchRequestId: 0,
        fetch: async () => ({ok: true, json: async () => ({items})}),
        tenantNotificationPreference: "true", scheduled: [],
        updateFieldState() {}, updateObjectState() {}, updateAssignmentControls() {}, updateAssignmentPreview() {},
        hasAssignmentRuleValue: () => false, isInternalAssignmentLocked: () => false,
        applyAutomaticValue: (element, value) => { element.value = value; },
        clearAutomaticValue: element => { element.value = ""; },
    };
    for (const name of ["objectType", "objectId", "objectSummary", "objectResults", "objectSearchContext", "notifyTenants", "companyCode", "craftsmanUser", "submitCommission"]) {
        sandbox[name] = new Element();
    }
    sandbox.objectSearchContext.value = "address";
    sandbox.scheduleObjectSearch = delay => sandbox.scheduled.push(delay);
    vm.createContext(sandbox);
    vm.runInContext([
        "objectTypeLabel", "objectHeading", "futureContractBadge", "retrospectiveCommissionSelected", "updateTenantNotificationOptions", "objectText",
        "selectObject", "showObjectSearchMessage", "loadObjects",
    ].map(functionSource).join("\n"), sandbox);
    const listenerStart = source.indexOf("    objectSearchContext.addEventListener(");
    const listenerEnd = source.indexOf("    objectSearch.addEventListener(", listenerStart);
    assert.ok(listenerStart >= 0 && listenerEnd > listenerStart);
    vm.runInContext(source.slice(listenerStart, listenerEnd), sandbox);
    return sandbox;
}

const building = {
    object_type: "building", object_id: 2, label: "Musterstraße 1", company_code: {erp_id: 77},
    building: {id_num: "GE-1", address: "Musterstraße 1"}, economic_unit: {id_num: "WE-1"},
};
const futureContract = {
    ...building, object_type: "license_agreement", object_id: 4, label: "Künftiger Mieter",
    tenant_name: "Künftiger Mieter", is_future: true, is_vacancy: false,
    license_agreement: {erp_id: 4004, id_num: "MV-1", start_date: "2027-01-01"},
    use_unit: {id_num: "NE-1", floor: "1. OG", position: "links", description: "Wohnung am Innenhof", address: "Musterstraße 1"},
};

test("building search renders future tenants and selects the contract as the order target", async () => {
    const state = form([building, futureContract]);
    await state.loadObjects("Musterstraße", "address");
    const [buildingButton, contractButton] = state.objectResults.children;
    assert.ok(contractButton.children[0].textContent.startsWith("Künftiger Mieter"));
    assert.match(contractButton.children[0].className, /fw-semibold/);
    assert.doesNotMatch(contractButton.children[0].textContent, /MV-1/);
    assert.match(contractButton.children[1].textContent, /Vertrag MV-1/);
    assert.match(contractButton.textContent, /Vertrag ab 01\.01\.2027/);
    const badge = contractButton.children[0].children[0];
    assert.match(badge.className, /bg-primary-subtle/);
    contractButton.click();
    assert.equal(state.objectType.value, "license_agreement");
    assert.equal(state.objectId.value, "4");
    assert.equal(state.objectSearchContext.value, "address");
    assert.match(state.objectSummary.textContent, /Vertrag ab 01\.01\.2027/);
    assert.ok(state.objectSummary.children[0].textContent.startsWith("Künftiger Mieter"));
    assert.doesNotMatch(state.objectSummary.children[0].textContent, /MV-1/);
    assert.match(state.objectSummary.children[1].textContent, /Vertrag MV-1/);
    assert.equal(state.notifyTenants.disabled, false);
    assert.equal(state.submitCommission.disabled, false);
    buildingButton.click();
    assert.equal(state.objectType.value, "building");
    assert.equal(state.objectId.value, "2");
    assert.equal(state.notifyTenants.disabled, true);
    assert.doesNotMatch(state.objectSummary.textContent, /Vertrag ab/);
});

test("changing the search mode preserves the explicitly selected order target", () => {
    const state = form();
    state.selectObject(futureContract);
    state.objectSearchContext.value = "use_unit";
    state.objectSearchContext.listeners.change();
    assert.equal(state.objectType.value, "license_agreement");
    assert.equal(state.objectId.value, "4");
    assert.equal(state.submitCommission.disabled, false);
    assert.deepEqual(state.scheduled, [0]);
});

test("vacancies remain selectable and never enable tenant notifications", async () => {
    for (const objectType of ["license_agreement", "use_unit"]) {
        const vacancy = {
            ...futureContract, object_type: objectType, label: "Leerstand", tenant_name: null,
            is_vacancy: true, is_future: false,
            license_agreement: objectType === "license_agreement" ? futureContract.license_agreement : null,
        };
        const state = form([vacancy]);
        await state.loadObjects("Musterstraße", "address");
        state.objectResults.children[0].click();
        assert.equal(state.objectType.value, objectType);
        assert.match(state.objectSummary.textContent, /Leerstand/);
        assert.equal(state.objectSummary.textContent.split("Leerstand").length - 1, 1);
        assert.equal(state.notifyTenants.disabled, true);
        assert.equal(state.notifyTenants.value, "false");
        assert.equal(state.submitCommission.disabled, false);
    }
});

test("the future hint is absent for current contracts and other object levels", () => {
    const state = form();
    assert.equal(state.futureContractBadge(building), null);
    assert.equal(state.futureContractBadge({...futureContract, is_future: false}), null);
    assert.equal(state.futureContractBadge({...futureContract, license_agreement: null}), null);
});

test("a retrospective order keeps its object selection but disables tenant notifications", () => {
    const state = form();
    state.document.getElementById = () => ({value: "true"});
    state.selectObject(futureContract);
    assert.equal(state.objectType.value, "license_agreement");
    assert.equal(state.notifyTenants.disabled, true);
    assert.equal(state.notifyTenants.value, "false");
});

test("results and selected summaries display only the selected level number with location and address", async () => {
    const unit = {
        ...futureContract, object_type: "use_unit", label: "NE-1", tenant_name: null,
        license_agreement: null, is_future: false,
    };
    const economic = {
        object_type: "economic_unit", object_id: 1, economic_unit: {id_num: "WE-1", name: "Siedlung"},
    };
    for (const [item, expectedNumber] of [[futureContract, "MV-1"], [unit, "NE-1"], [building, "GE-1"], [economic, "WE-1"]]) {
        const state = form([item]);
        await state.loadObjects("Musterstraße", "all");
        const button = state.objectResults.children[0];
        button.click();
        for (const element of [button, state.objectSummary]) {
            assert.equal(element.textContent.split(expectedNumber).length - 1, 1);
            for (const otherNumber of ["MV-1", "NE-1", "GE-1", "WE-1"].filter(number => number !== expectedNumber)) {
                assert.ok(!element.textContent.includes(otherNumber));
            }
            if (["license_agreement", "use_unit"].includes(item.object_type)) {
                assert.match(element.textContent, /Wohnung am Innenhof/);
                assert.doesNotMatch(element.textContent, /1\. OG/);
                assert.doesNotMatch(element.textContent, /links/);
            }
            if (item.object_type !== "economic_unit") {
                assert.match(element.textContent, /Musterstraße 1/);
            }
        }
    }
});

test("only the location description is displayed alongside the unit address", () => {
    const state = form();
    state.selectObject({
        ...futureContract,
        use_unit: {id_num: "NE-1", floor: "EG", position: "rechts", description: "Hinterhaus", address: "Wehrenboldstraße 94"},
    });
    assert.match(state.objectSummary.textContent, /Wehrenboldstraße 94/);
    assert.doesNotMatch(state.objectSummary.textContent, /Musterstraße 1/);
    assert.equal(state.objectSummary.textContent.split("Hinterhaus").length - 1, 1);
    assert.doesNotMatch(state.objectSummary.textContent, /EG|rechts/);
});

test("missing numbers and descriptions never promote ancestor numbers or other location fields", () => {
    const state = form();
    state.selectObject({
        ...futureContract, license_agreement: {...futureContract.license_agreement, id_num: null},
        use_unit: {...futureContract.use_unit, description: null},
    });
    assert.match(state.objectSummary.textContent, /Künftiger Mieter/);
    assert.match(state.objectSummary.textContent, /Musterstraße 1/);
    assert.doesNotMatch(state.objectSummary.textContent, /1\. OG|links/);
    for (const number of ["NE-1", "GE-1", "WE-1"]) {
        assert.ok(!state.objectSummary.textContent.includes(number));
    }
});
