// Hands wiring guide. No framework, no inline HTML from data: every string from the
// server (step text, vision findings) is inserted with textContent.
"use strict";
(function () {
  const token = new URLSearchParams(location.search).get("t") || "";
  const $ = (id) => document.getElementById(id);
  let state = null, cur = 0;

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers || {}, token ? { "X-Hands-Token": token } : {});
    const r = await fetch(path, opts);
    const ct = r.headers.get("Content-Type") || "";
    return ct.includes("json") ? r.json() : r.text();
  }

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }

  async function load(keepPos) {
    state = await api("/api/state");
    if (state.error) { $("title").textContent = state.error; return; }
    if (!keepPos) { cur = state.steps.findIndex((s) => !s.done); if (cur < 0) cur = state.steps.length - 1; }
    render();
  }

  function renderList() {
    const list = $("list");
    list.replaceChildren();
    state.steps.forEach((s) => {
      const li = el("li");
      if (s.done) li.classList.add("done");
      if (s.n === cur) li.setAttribute("aria-current", "step");
      const b = el("button", s.n + ". " + s.title);
      b.type = "button";
      b.addEventListener("click", () => { cur = s.n; render(); $("step").focus(); });
      li.appendChild(b);
      list.appendChild(li);
    });
  }

  function renderPower() {
    const p = state.power;
    $("powerBlocked").hidden = p.ready;
    $("powerReady").hidden = !p.ready;
    const ul = $("powerReasons");
    ul.replaceChildren();
    p.reasons.forEach((r) => ul.appendChild(el("li", r)));
  }

  function renderCheck() {
    $("photoArea").hidden = !state.vision;
    $("photoBtn").hidden = !state.camera;
    $("selfArea").hidden = state.vision;
    if (!state.vision) {
      const ul = $("selfList");
      if (!ul.childElementCount) {
        state.selfcheck_items.forEach((it) => {
          const li = el("li"), lab = el("label"), cb = el("input");
          cb.type = "checkbox"; cb.value = it.id;
          cb.checked = state.selfcheck_done.includes(it.id);
          cb.addEventListener("change", saveSelfcheck);
          lab.appendChild(cb); lab.appendChild(el("span", it.text));
          li.appendChild(lab); ul.appendChild(li);
        });
      }
    }
  }

  async function saveSelfcheck() {
    const items = Array.from(document.querySelectorAll("#selfList input:checked")).map((c) => c.value);
    const r = await api("/api/selfcheck", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify({ items }) });
    if (r.power) { state.power = r.power; renderPower(); }
  }

  async function render() {
    const s = state.steps[cur];
    const total = state.steps.length;
    const done = state.steps.filter((x) => x.done).length;
    $("kind").textContent = s.kind.replace("_", " ") + " · step " + s.n + " of " + (total - 1);
    $("title").textContent = s.title;
    $("detail").textContent = s.detail;
    $("progress").textContent = done + " / " + total + " done";
    const pct = Math.round((100 * done) / total);
    $("bar").setAttribute("aria-valuenow", String(pct));
    $("barFill").style.width = pct + "%";
    $("prev").disabled = cur === 0;
    $("done").textContent = s.done ? "Next step →" : (cur === total - 1 ? "Done ✓" : "Done, next step →");
    renderList();
    renderCheck();
    renderPower();
    const svg = await api("/api/steps/" + s.n + "/svg");
    $("svg").innerHTML = typeof svg === "string" && svg.startsWith("<svg") ? svg : "";
    $("svg").setAttribute("aria-label", "Breadboard diagram for step " + s.n + ": " + s.title);
  }

  async function markDone() {
    const s = state.steps[cur];
    if (!s.done) await api("/api/steps/" + s.n + "/done", { method: "POST" });
    await load(true);
    if (cur < state.steps.length - 1) cur++;
    render();
  }

  function showPhoto(r) {
    const box = $("photoResult");
    box.replaceChildren();
    if (r.error) { box.appendChild(el("p", r.error, "bad")); return; }
    if (r.ok) { box.appendChild(el("p", "Wiring matches the plan.", "ok")); }
    else {
      box.appendChild(el("p", (r.discrepancies || []).length + " problem(s) found. Fix them with USB unplugged, then check again:", "bad"));
      const ul = el("ul");
      (r.discrepancies || []).forEach((d) => {
        const li = el("li");
        li.appendChild(el("strong", d.message));
        li.appendChild(document.createTextNode(" — " + d.instruction));
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }
  }

  $("done").addEventListener("click", markDone);
  $("prev").addEventListener("click", () => { if (cur > 0) { cur--; render(); } });
  $("photoBtn").addEventListener("click", async () => {
    $("photoResult").textContent = "Taking and checking a photo…";
    showPhoto(await api("/api/photo_check", { method: "POST" }));
    await load(true);
  });
  $("photoFile").addEventListener("change", async (ev) => {
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    $("photoResult").textContent = "Uploading and checking the photo…";
    showPhoto(await api("/api/photo", { method: "POST", headers: { "Content-Type": f.type || "application/octet-stream" }, body: f }));
    ev.target.value = "";
    await load(true);
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest && e.target.closest("input, button, summary, label")) return;
    if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); markDone(); }
    if (e.key === "ArrowLeft" && cur > 0) { cur--; render(); }
  });
  if (window.matchMedia("(max-width: 860px)").matches) $("stepsBox").open = false;
  load(false);
})();
