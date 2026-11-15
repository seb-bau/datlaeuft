const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/ticket_photo_selection.js"), "utf8");

class Element {
    constructor(tag = "div") {
        this.tag = tag;
        this.children = [];
        this.listeners = {};
        this.dataset = {};
        this.style = {};
        this.value = "";
        this.files = [];
        this.className = "";
        this.textContent = "";
        this.classList = {
            add: value => { this.className += " " + value; },
            remove: value => { this.className = this.className.split(" ").filter(item => item !== value).join(" "); },
            toggle: (value, enabled) => { if (enabled) this.classList.add(value); else this.classList.remove(value); },
            contains: value => this.className.split(" ").includes(value),
        };
    }
    append(...items) { this.children.push(...items); }
    appendChild(item) { this.children.push(item); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this[key] = value; }
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
    dispatchEvent(event) { (this.listeners[event.type] || []).forEach(listener => listener(event)); }
    click() { this.dispatchEvent({type: "click"}); }
    focus() { this.focused = true; }
}

const photos = [
    {file_guid: "kitchen", filename: "kueche.png", thumbnail_url: "/kitchen/thumb", content_url: "/kitchen/content"},
    {file_guid: "bathroom", filename: "bad.png", thumbnail_url: "/bathroom/thumb", content_url: "/bathroom/content"},
];

function fixture({initial = null, replies = [{ok: true, items: photos}], ticket = true} = {}) {
    const elements = Object.fromEntries([
        "commissionForm", "photoUpload", "uploadedPhotoPreviews", "ticketPhotoSelection", "ticketPhotoSelectionReady",
        "ticketPhotoGallery", "ticketPhotoStatus", "retryTicketPhotos", "restoreTicketPhotos", "initialTicketPhotoSelection",
    ].map(id => [id, new Element()]));
    elements.ticketPhotoSelection.dataset.photosUrl = "/ticket/photos";
    elements.initialTicketPhotoSelection.textContent = JSON.stringify(initial);
    if (!ticket) delete elements.ticketPhotoSelection;
    let mounted;
    const revoked = [];
    const sandbox = {
        document: {
            getElementById: id => elements[id], createElement: tag => new Element(tag),
            addEventListener: (type, handler) => { if (type === "DOMContentLoaded") mounted = handler; },
        },
        window: {addEventListener() {}},
        Event: class {constructor(type) {this.type = type;}},
        AbortController, setTimeout, clearTimeout,
        URL: {createObjectURL: file => "blob:" + file.name, revokeObjectURL: url => revoked.push(url)},
        DataTransfer: class {
            constructor() { this.files = []; this.items = {add: file => this.files.push(file)}; }
        },
        fetch: async () => {
            const reply = replies.shift();
            if (reply instanceof Error) throw reply;
            return {ok: reply.ok, json: async () => reply};
        },
    };
    vm.runInNewContext(source, sandbox);
    mounted();
    return {elements, revoked};
}

function descendants(element) {
    return element.children.flatMap(child => [child, ...descendants(child)]);
}

function submittedGuids(state) {
    return descendants(state.elements.ticketPhotoGallery).filter(item => item.name === "ticket_photo_guid").map(item => item.value);
}

function removePhoto(column) {
    descendants(column).find(item => item.tag === "button").click();
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test("ticket photos appear selected by default, can be removed and restored before submission", async () => {
    const state = fixture();
    await settle();
    assert.equal(state.elements.ticketPhotoSelectionReady.value, "true");
    assert.deepEqual(submittedGuids(state), ["kitchen", "bathroom"]);
    removePhoto(state.elements.ticketPhotoGallery.children[1]);
    assert.deepEqual(submittedGuids(state), ["kitchen"]);
    assert.match(state.elements.ticketPhotoStatus.textContent, /1 von 2/);
    assert.equal(state.elements.restoreTicketPhotos.classList.contains("d-none"), false);
    state.elements.restoreTicketPhotos.click();
    assert.deepEqual(submittedGuids(state), ["kitchen", "bathroom"]);
});

test("an intentionally empty selection survives validation and remains submittable", async () => {
    const state = fixture({initial: []});
    await settle();
    assert.deepEqual(submittedGuids(state), []);
    assert.match(state.elements.ticketPhotoStatus.textContent, /0 von 2/);
    let prevented = false;
    state.elements.commissionForm.dispatchEvent({type: "submit", preventDefault() {prevented = true;}});
    assert.equal(prevented, false);
});

test("a previous partial selection is preserved when the form is redisplayed", async () => {
    const state = fixture({initial: ["bathroom"]});
    await settle();
    assert.deepEqual(submittedGuids(state), ["bathroom"]);
});

test("loading failures block submission and allow retry without changing other form fields", async () => {
    const state = fixture({replies: [new TypeError("Failed to fetch"), {ok: true, items: photos}]});
    state.elements.photoUpload.value = "local-image.jpg";
    await settle();
    assert.equal(state.elements.ticketPhotoSelectionReady.value, "");
    assert.match(state.elements.ticketPhotoStatus.textContent, /Ticketfotos konnten nicht geladen werden/);
    let prevented = false;
    state.elements.commissionForm.dispatchEvent({type: "submit", preventDefault() {prevented = true;}});
    assert.equal(prevented, true);
    state.elements.retryTicketPhotos.click();
    await settle();
    assert.equal(state.elements.ticketPhotoSelectionReady.value, "true");
    assert.equal(state.elements.photoUpload.value, "local-image.jpg");
    assert.deepEqual(submittedGuids(state), ["kitchen", "bathroom"]);
});

test("no ticket photos is a successful explicit selection", async () => {
    const state = fixture({replies: [{ok: true, items: []}]});
    await settle();
    assert.equal(state.elements.ticketPhotoSelectionReady.value, "true");
    assert.match(state.elements.ticketPhotoStatus.textContent, /Keine Fotos im Ticket/);
});

test("additional local photos can be previewed and individually removed from the upload", () => {
    const state = fixture({ticket: false});
    state.elements.photoUpload.files = [{name: "eins.jpg"}, {name: "zwei.jpg"}, {name: "drei.jpg"}];
    state.elements.photoUpload.dispatchEvent({type: "change"});
    assert.equal(state.elements.uploadedPhotoPreviews.children.length, 3);
    removePhoto(state.elements.uploadedPhotoPreviews.children[1]);
    assert.deepEqual(state.elements.photoUpload.files.map(file => file.name), ["eins.jpg", "drei.jpg"]);
    assert.equal(state.elements.uploadedPhotoPreviews.children.length, 2);
    assert.ok(state.revoked.includes("blob:zwei.jpg"));
});
