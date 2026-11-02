// Execute the real admin script against API snapshots and the DOM surface it uses.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const fixture = JSON.parse(fs.readFileSync(0, "utf8"));

class Element {
  constructor() {
    this.children = []; this.label = ""; this.value = ""; this.open = false;
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      toggle: (name, force) => {
        const on = force ?? !classes.has(name);
        if (on) classes.add(name); else classes.delete(name);
        return on;
      }
    };
  }
  set textContent(value) { this.label = String(value); this.children = []; }
  get textContent() { return this.label + this.children.map(c => typeof c === "string" ? c : c.textContent).join(""); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.label = ""; }
  addEventListener() {}
  querySelectorAll() { return []; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const html = fs.readFileSync("app/static/admin.html", "utf8");
const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(m => [m[1], new Element()]));
let overview = fixture.before;
vm.runInNewContext(fs.readFileSync("app/static/admin.js", "utf8"), {
  document: {
    getElementById: id => elements.get(id),
    createElement: () => new Element(),
    createTextNode: text => text,
    querySelectorAll: () => []
  },
  navigator: {},
  fetch: async (url, options) => ({
    ok: true, status: 200,
    json: async () => url === "/admin/api/keys" && options.method === "POST"
      ? {id: "issued-test", key: "sk-myais-test-issued"}
      : overview
  })
});

const tableHas = (table, name) => elements.get(table).children.some(row => row.textContent.includes(name));
(async () => {
  elements.get("admin-token").value = "test-admin-token";
  await elements.get("login-form").onsubmit({preventDefault() {}});
  assert.equal(elements.get("login-status").textContent, "");
  assert.equal(elements.get("base-url").textContent, overview.public_api_base_url);
  assert.equal(elements.get("issued-url").textContent, overview.public_api_base_url);
  elements.get("new-key").onclick();
  elements.get("key-form").onsubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get("issued-dialog").open, true);
  assert.equal(elements.get("issued-key").value, "sk-myais-test-issued");
  assert.equal(elements.get("issued-url").textContent, overview.public_api_base_url);
  assert.equal(elements.get("stat-keys").textContent, "2");
  assert.equal(elements.get("keys").children.length, 2);
  assert(tableHas("keys", "Active control"));
  assert(tableHas("keys", "Archive candidate"));
  for (const name of ["Disabled control", "Expired control", "Inactive owner control"]) {
    assert(!tableHas("keys", name));
    assert(tableHas("archived-keys", name));
  }
  assert.equal(elements.get("archived-keys-dialog").open, false);
  overview = fixture.after;
  await elements.get("refresh").onclick();
  assert.equal(elements.get("base-url").textContent, overview.public_api_base_url);
  assert.equal(elements.get("issued-url").textContent, overview.public_api_base_url);
  assert.equal(elements.get("stat-keys").textContent, "1");
  assert.equal(elements.get("keys").children.length, 1);
  assert(tableHas("keys", "Active control"));
  assert(!tableHas("keys", "Archive candidate"));
  elements.get("show-archived-keys").onclick();
  assert.equal(elements.get("archived-keys-dialog").open, true);
  const archived = overview.keys.find(k => k.id === fixture.key_id);
  const row = elements.get("archived-keys").children.find(r => r.textContent.includes(archived.name));
  assert(row);
  for (const text of [archived.prefix, "legacy-owner", "ais-fast", "Без лимита / Без лимита",
                     new Date(archived.created_at).toLocaleString(), new Date(archived.revoked_at).toLocaleString()]) {
    assert(row.textContent.includes(text), "Missing archive detail: " + text);
  }
  assert(elements.get("usage").textContent.includes("Archive candidate"));
  process.stdout.write("Admin key archive UI: OK\n");
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
