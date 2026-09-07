// Both are served by api/main.py, so every path here is same-origin and
// relative: no host to keep in sync, no CORS, nothing to change when the
// port does.
const DATA = "/data/ui/";

const CHECK = '<svg viewBox="0 0 12 12"><path d="M2.5 6.2l2.3 2.3L9.5 3.8"/></svg>';
const CHEVRON = '<svg viewBox="0 0 16 16"><path d="M6 3l5 5-5 5"/></svg>';

const el = {
  gear: document.getElementById("gear"),
  panel: document.getElementById("panel"),
  body: document.getElementById("panel-body"),
  title: document.getElementById("panel-title"),
  count: document.getElementById("panel-count"),
  back: document.getElementById("back"),
  foot: document.getElementById("panel-foot"),
  discard: document.getElementById("discard"),
  cancel: document.getElementById("cancel"),
  save: document.getElementById("save"),
  summary: document.getElementById("summary"),
  run: document.getElementById("run"),
  runTitle: document.getElementById("run-title"),
  runSub: document.getElementById("run-sub"),
  since: document.getElementById("since"),
  sinceValue: document.getElementById("since-value"),
};

let data = { nace: null, sizes: null, regions: null };

// saved is what the pipeline would run with; draft is what the user is
// editing. Nothing moves from draft to saved without the save button.
// Both start empty and are replaced in boot() by whatever the pipeline
// would actually run with - the last saved brief, or its built-in ICP.
let saved = blank();
let draft = blank();

// What the pipeline falls back to when these two fields are left empty,
// as GET /api/icp reported it. Kept so the hints under "Vlastní okruh"
// can state the real default instead of repeating "Plzeň" and "150" in
// this file - the same reason the NACE list is not copied here either.
let icpDefault = { from: "", km: null };

let view = { name: "root", division: null };
let dirty = false;
let exitArmed = false;

// Kraje and the custom radius describe the same thing two ways, so only
// one shows at a time. Not part of the filter state itself - just which
// half of the view is on screen right now.
let regionMode = "regions";

function blank() {
  return { nace: new Set(), sizes: new Set(), regions: new Set(), km: null, from: "", origin: null };
}

// 6400 municipalities with coordinates, the same file the pipeline uses
// for distance. Fetched the first time the Kraje view is opened rather
// than on load - it is the largest of the four files and most sessions
// never touch the radius.
let places = null;

async function ensurePlaces() {
  if (places) return places;
  const payload = await fetch(DATA + "obce.json").then((r) => r.json());
  places = Object.values(payload).map((place) => ({
    name: place.name,
    lat: place.lat,
    lon: place.lon,
    key: fold(place.name),
  }));
  return places;
}

// Nobody types Plzeň with the caron, so matching happens on a folded
// copy of the name while the suggestion still shows the real spelling.
function fold(text) {
  return text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

function suggest(text, limit = 6) {
  const needle = fold(text.trim());
  if (!needle || !places) return [];

  const starts = [];
  const inside = [];
  for (const place of places) {
    if (place.key.startsWith(needle)) starts.push(place);
    else if (place.key.includes(needle)) inside.push(place);
  }

  // "Plzeň" must beat "Plzeň 1" - a city is split into numbered
  // districts, and the plain name is almost always what was meant.
  const rank = (a, b) =>
    (a.key === needle ? 0 : 1) - (b.key === needle ? 0 : 1)
    || a.key.length - b.key.length
    || a.name.localeCompare(b.name, "cs");

  starts.sort(rank);
  inside.sort(rank);
  return starts.concat(inside).slice(0, limit);
}

function clone(state) {
  return {
    nace: new Set(state.nace),
    sizes: new Set(state.sizes),
    regions: new Set(state.regions),
    km: state.km,
    from: state.from,
    origin: state.origin,
  };
}

function same(a, b) {
  for (const key of ["nace", "sizes", "regions"]) {
    if (a[key].size !== b[key].size) return false;
    for (const value of a[key]) if (!b[key].has(value)) return false;
  }
  return a.km === b.km && a.from === b.from && originName(a) === originName(b);
}

function originName(state) {
  return state.origin ? state.origin.name : null;
}

const nf = new Intl.NumberFormat("cs-CZ");

async function boot() {
  const [nace, sizes, regions] = await Promise.all([
    fetch(DATA + "nace.json").then((r) => r.json()),
    fetch(DATA + "sizes.json").then((r) => r.json()),
    fetch(DATA + "regions.json").then((r) => r.json()),
  ]);

  // Everything the user scans is ordered by how many companies sit
  // behind it - the biggest pools are the ones worth deciding about.
  nace.divisions.sort((a, b) => b.count - a.count);
  nace.divisions.forEach((d) => d.codes.sort((a, b) => b.count - a.count));

  data = { nace, sizes, regions };

  // After data: the ICP arrives as NACE divisions, and turning those
  // into the codes the screens tick needs the codebook already loaded.
  saved = await loadIcp();
  draft = clone(saved);
  renderSummary();

  // Asked once on load and then only while something is running: it fills
  // the timer, and it picks a run back up if the page was reloaded while
  // one was going.
  refreshRun();

  // The panel can be opened before the fetches land; redraw whatever
  // view is showing rather than leaving it empty.
  if (el.panel.dataset.open === "1") go(view.name, view.division);
}

/* the saved brief ------------------------------------------------------- */

// The filters open on the ICP the pipeline already has rather than on an
// empty form. It is asked for instead of repeated here on purpose: the
// list of NACE divisions lives in pipeline/sources/res_bulk.py, and a
// second copy in this file would be one nobody remembers to update.
//
// Without the API there is nothing to save to either, so an empty form
// is the honest fallback - the console says why.
async function loadIcp() {
  try {
    const response = await fetch("/api/icp");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return fromIcp(await response.json());
  } catch (error) {
    console.error("Could not load the ICP", error);
    return blank();
  }
}

function fromIcp(icp) {
  const state = blank();
  expandNace(icp.nace).forEach((code) => state.nace.add(code));
  (icp.katpo || []).forEach((code) => state.sizes.add(code));
  (icp.regions || []).forEach((code) => state.regions.add(code));

  const location = icp.location || {};
  state.km = location.km ?? null;
  state.from = location.from || "";
  state.origin = location.origin || null;
  icpDefault = {
    from: state.from || (location.origin || {}).name || "",
    km: state.km,
  };
  return state;
}

// The pipeline names whole divisions ("28"), the screens tick the codes
// inside them ("28110"). A division stands for all of its codes - the
// same expansion toggleDivision() does when a row is clicked, so a brief
// written either way arrives at the same set of checkboxes.
function expandNace(values) {
  const codes = [];
  for (const value of values || []) {
    const division = data.nace.divisions.find((d) => d.code === value);
    if (division) division.codes.forEach((code) => codes.push(code.code));
    else codes.push(value);
  }
  return codes;
}

/* panel open / close ---------------------------------------------------- */

el.gear.addEventListener("click", () => {
  el.panel.dataset.open === "1" ? tryClose() : open();
});

function open() {
  el.panel.dataset.open = "1";
  el.panel.setAttribute("aria-hidden", "false");
  el.gear.setAttribute("aria-expanded", "true");
  draft = clone(saved);
  dirty = false;
  exitArmed = false;
  go("root");

  // Clicks are ignored until the box stops moving; see the note in the
  // stylesheet next to .panel.
  setTimeout(() => { el.panel.dataset.settled = "1"; }, 280);
}

function close() {
  el.panel.dataset.open = "0";
  el.panel.dataset.settled = "0";
  el.panel.style.maxHeight = "0px";
  el.panel.setAttribute("aria-hidden", "true");
  el.gear.setAttribute("aria-expanded", "false");
  dirty = false;
  exitArmed = false;
  el.discard.textContent = "";
}

// First click on an unsaved panel warns; the second one closes anyway.
// Losing edits silently is worse than one extra click.
function tryClose() {
  if (dirty && !exitArmed) {
    exitArmed = true;
    el.discard.textContent = "Neuloženo — zavřít znovu pro zahození";
    sizePanel();
    return;
  }
  close();
}

el.back.addEventListener("click", () => {
  go(view.name === "nace-codes" ? "nace" : "root");
});

el.save.addEventListener("click", async () => {
  if (!dirty) return;

  el.save.disabled = true;
  el.discard.textContent = "Ukládám…";
  try {
    const response = await fetch("/api/icp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        nace: [...draft.nace],
        sizes: [...draft.sizes],
        regions: [...draft.regions],
        km: draft.km,
        from: draft.from,
        origin: draft.origin,
      }),
    });

    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    saved = clone(draft);
    dirty = false;
    exitArmed = false;
    markDirty();
    renderSummary();

    // Saving is the end of the errand, so the panel gets out of the way
    // by itself. Only on success: a failed save has to keep the edits on
    // screen next to the reason it failed.
    close();
  } catch (error) {
    el.discard.textContent = "Uložení selhalo — běží API?";
    console.error("Could not save filters", error);
    sizePanel();
  } finally {
    el.save.disabled = false;
  }
});

el.cancel.addEventListener("click", () => {
  draft = clone(saved);
  go(view.name, view.division);
});

/* the run --------------------------------------------------------------- */

// Both screens run the pipeline, so the behaviour lives in run-control.js
// and this passes it the tile it should drive. What is local to this page
// is only where to go afterwards: the week is the result of the run, so
// the run ends by opening it.
const runControl = mountRunControl({
  button: el.run,
  title: el.runTitle,
  sub: el.runSub,
  since: el.since,
  sinceValue: el.sinceValue,
  onFinished: () => { window.location.href = "/"; },
});

function refreshRun() {
  if (runControl) runControl.refresh();
}

// The footer follows the edits, not the screen: once something is
// changed the save button stays reachable from every view, including
// the top-level list.
function markDirty() {
  dirty = !same(draft, saved);
  if (!dirty) {
    exitArmed = false;
    el.discard.textContent = "";
  }
  el.foot.hidden = !dirty;

  // Showing or hiding the footer changes how tall the panel needs to
  // be, so the box has to follow in the same step.
  sizePanel();
}

function countActive(state) {
  return state.nace.size + state.sizes.size + state.regions.size + (state.km ? 1 : 0);
}

/* navigation ------------------------------------------------------------ */

const DEPTH = { root: 0, nace: 1, size: 1, regions: 1, "nace-codes": 2 };

function go(name, division) {
  const back = DEPTH[name] < DEPTH[view.name];
  const first = el.body.innerHTML === "";

  view = { name, division: division ?? null };

  const draw = () => {
    el.back.hidden = name === "root";
    el.body.scrollTop = 0;

    if (name === "root") { el.title.textContent = "Filtry"; renderRoot(); }
    if (name === "nace") { el.title.textContent = "Obor"; renderNace(); }
    if (name === "nace-codes") { el.title.textContent = divisionOf(division).name || division; renderCodes(division); }
    if (name === "size") { el.title.textContent = "Velikost"; renderSizes(); }
    if (name === "regions") {
      el.title.textContent = "Kraje";
      // Re-derive only on entry - a segment click updates regionMode
      // itself, and re-deriving on every render would fight that.
      regionMode = draft.km ? "custom" : "regions";
      renderRegions();
    }

    markDirty();
    sizePanel();
  };

  if (first) {
    draw();
    return;
  }
  swap(draw, back);
}

// Single path for replacing panel-body's content wholesale - a level
// change via go(), a tab switch, anything that swaps one screen of UI
// for a structurally different one. Any future view swap must call this
// rather than re-rendering directly, or it silently loses the motion
// every other transition has.
//
// In-place updates that redraw the SAME screen (a checkbox flipping, a
// count refreshing) are not this - those call their render*() function
// directly, on purpose: animating every keystroke would be noise, not
// feedback.
function swap(draw, back) {
  el.body.dataset.dir = back ? "back" : "forward";
  el.body.dataset.phase = "leave";

  setTimeout(() => {
    draw();
    el.body.dataset.phase = "enter";
    requestAnimationFrame(() => { el.body.dataset.phase = "in"; });
  }, 110);
}

// The panel animates to whatever the current view needs, so stepping a
// level deeper moves the box instead of leaving dead space.
//
// Measured by letting the box size itself for one frame, then putting the
// constraint back: offsetHeight then reports the exact number max-height
// needs under border-box, borders included. Guessing at scrollHeight plus
// a constant was off by a pixel or two depending on the view.
function sizePanel() {
  if (el.panel.dataset.open !== "1") return;

  const previous = el.panel.style.maxHeight;
  el.panel.style.maxHeight = "none";
  const natural = el.panel.offsetHeight;

  el.panel.style.maxHeight = previous;
  void el.panel.offsetHeight; // flush, so the transition starts from where it was
  el.panel.style.maxHeight = natural + "px";
}

function divisionOf(code) {
  return data.nace.divisions.find((d) => d.code === code) || { name: code, codes: [] };
}

/* views ----------------------------------------------------------------- */

function renderRoot() {
  el.count.textContent = "";
  const items = [
    ["nace", "Obor", summaryNace()],
    ["size", "Velikost", summarySizes()],
    ["regions", "Kraje", summaryRegions()],
  ];
  el.body.innerHTML = items.map(([key, label, sub]) => `
    <button class="row" data-go="${key}" type="button">
      <span class="row-label">
        <span class="row-name">${label}</span>
        <span class="row-sub">${sub}</span>
      </span>
      <span class="row-open">${CHEVRON}</span>
    </button>`).join("");

  el.body.querySelectorAll("[data-go]").forEach((node) => {
    node.addEventListener("click", () => go(node.dataset.go));
  });
}

function renderNace() {
  el.count.textContent = `${nf.format(sumSelectedNace())} firem`;

  el.body.innerHTML = data.nace.divisions.map((division) => {
    // A division holding one code has nothing to open - the checkbox
    // already is that code.
    const splits = division.codes.length > 1;
    return `
    <div class="row" data-division="${division.code}">
      <span class="box" data-box="${division.code}" data-state="${divisionState(division)}">${CHECK}</span>
      <span class="row-label">
        <span class="row-name">${division.name || division.code}</span>
        ${splits ? `<span class="row-sub">${division.codes.length} kódů</span>` : ""}
      </span>
      <span class="row-count">${nf.format(division.count)}</span>
      ${splits
        ? `<span class="row-open" data-open="${division.code}">${CHEVRON}</span>`
        : `<span class="row-open-spacer"></span>`}
    </div>`;
  }).join("");

  el.body.querySelectorAll("[data-division]").forEach((node) => {
    node.addEventListener("click", () => toggleDivision(node.dataset.division));
  });
  el.body.querySelectorAll("[data-open]").forEach((node) => {
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      go("nace-codes", node.dataset.open);
    });
  });
}

function renderCodes(divisionCode) {
  const division = divisionOf(divisionCode);
  el.count.textContent = `${nf.format(division.count)} firem`;

  el.body.innerHTML = division.codes.map((code) => `
    <div class="row" data-code="${code.code}">
      <span class="box" data-state="${draft.nace.has(code.code) ? "on" : "off"}">${CHECK}</span>
      <span class="row-label">
        <span class="row-name">${code.name || "Bez názvu"}</span>
      </span>
      <span class="row-count">${nf.format(code.count)}</span>
    </div>`).join("");

  el.body.querySelectorAll("[data-code]").forEach((node) => {
    node.addEventListener("click", () => toggleCode(node.dataset.code, divisionCode));
  });
}

function renderSizes() {
  el.count.textContent = `${nf.format(sumSelected(data.sizes, draft.sizes))} firem`;
  el.body.innerHTML = data.sizes.map((band) => `
    <div class="row" data-size="${band.code}">
      <span class="box" data-state="${draft.sizes.has(band.code) ? "on" : "off"}">${CHECK}</span>
      <span class="row-label">
        <span class="row-name">${band.label}</span>
      </span>
      <span class="row-count">${nf.format(band.count)}</span>
    </div>`).join("");

  el.body.querySelectorAll("[data-size]").forEach((node) => {
    node.addEventListener("click", () => {
      toggle(draft.sizes, node.dataset.size);
      renderSizes();
      markDirty();
    });
  });
}

function renderRegions() {
  el.count.textContent = regionMode === "custom"
    ? ""
    : `${nf.format(sumSelected(data.regions, draft.regions))} firem`;

  const segmented = `
    <div class="segmented">
      <button class="seg-item" type="button" data-mode="regions" aria-pressed="${regionMode === "regions"}">Kraje</button>
      <button class="seg-item" type="button" data-mode="custom" aria-pressed="${regionMode === "custom"}">Vlastní okruh</button>
    </div>`;

  el.body.innerHTML = segmented + (regionMode === "regions" ? regionsList() : customRadius());

  el.body.querySelectorAll("[data-mode]").forEach((node) => {
    node.addEventListener("click", () => setRegionMode(node.dataset.mode));
  });

  if (regionMode === "regions") {
    el.body.querySelectorAll("[data-region]").forEach((node) => {
      node.addEventListener("click", () => {
        toggle(draft.regions, node.dataset.region);
        renderRegions();
        markDirty();
      });
    });
  } else {
    wireCustomRadius();
  }
}

// Switching modes clears whichever half is being left - the two are
// alternate ways to say the same filter, not two filters at once.
// Kraje is the left-hand tab, Vlastní okruh the right-hand one, so the
// motion follows the same left/right logic go() uses for depth.
function setRegionMode(mode) {
  if (mode === regionMode) return;
  const back = mode === "regions";

  if (mode === "custom") draft.regions.clear();
  else { draft.km = null; draft.from = ""; draft.origin = null; }
  regionMode = mode;

  swap(() => { renderRegions(); markDirty(); }, back);
}

function regionsList() {
  return data.regions.map((region) => `
    <div class="row" data-region="${region.code}">
      <span class="box" data-state="${draft.regions.has(region.code) ? "on" : "off"}">${CHECK}</span>
      <span class="row-label">
        <span class="row-name">${region.name || region.code}</span>
      </span>
      <span class="row-count">${nf.format(region.count)}</span>
    </div>`).join("");
}

function customRadius() {
  return `
    <div class="field-set">
      <div class="field">
        <label class="field-label" for="from">Odkud měřit</label>
        <input id="from" type="text" placeholder="${icpDefault.from}" value="${draft.from}" autocomplete="off">
        <div class="suggest" id="suggest"></div>
      </div>
      <div class="field">
        <label class="field-label" for="km">Poloměr v km</label>
        <input id="km" type="text" inputmode="numeric" placeholder="${icpDefault.km ?? ""}" value="${draft.km ?? ""}" autocomplete="off">
      </div>
    </div>`;
}

function wireCustomRadius() {
  const from = el.body.querySelector("#from");
  const km = el.body.querySelector("#km");
  const list = el.body.querySelector("#suggest");

  ensurePlaces().then(() => { if (document.activeElement === from) showSuggestions(from, list); });

  from.addEventListener("input", () => {
    draft.from = from.value.trim();
    // Typing past a chosen place drops it: the stored coordinates must
    // never disagree with the text the user is looking at.
    draft.origin = null;
    showSuggestions(from, list);
    markDirty();
  });
  from.addEventListener("focus", () => showSuggestions(from, list));
  from.addEventListener("blur", () => {
    // Late enough for a click on a suggestion to land first.
    setTimeout(() => { list.innerHTML = ""; sizePanel(); }, 120);
  });

  km.addEventListener("input", () => {
    const digits = km.value.replace(/\D/g, "");
    if (digits !== km.value) km.value = digits;
    draft.km = digits ? Number(digits) : null;
    markDirty();
  });
}

function showSuggestions(input, list) {
  const matches = suggest(input.value);
  if (!matches.length || (draft.origin && draft.origin.name === input.value)) {
    list.innerHTML = "";
    sizePanel();
    return;
  }

  list.innerHTML = matches.map((place, index) => `
    <button class="suggest-item" type="button" data-place="${index}">
      ${place.name}
    </button>`).join("");

  list.querySelectorAll("[data-place]").forEach((node) => {
    node.addEventListener("mousedown", (event) => {
      event.preventDefault();
      const place = matches[Number(node.dataset.place)];
      input.value = place.name;
      draft.from = place.name;
      draft.origin = { name: place.name, lat: place.lat, lon: place.lon };
      list.innerHTML = "";
      markDirty();
    });
  });

  sizePanel();
}

/* selection ------------------------------------------------------------- */

function toggle(set, value) {
  set.has(value) ? set.delete(value) : set.add(value);
}

// Ticking a whole division stores every one of its codes, so removing a
// single code later needs no special case - the set is always a plain
// list of codes the pipeline will filter on.
function toggleDivision(divisionCode) {
  const division = divisionOf(divisionCode);
  const state = divisionState(division);
  division.codes.forEach((code) => {
    if (state === "on") draft.nace.delete(code.code);
    else draft.nace.add(code.code);
  });
  renderNace();
  markDirty();
}

function toggleCode(code, divisionCode) {
  toggle(draft.nace, code);
  renderCodes(divisionCode);
  markDirty();
}

function divisionState(division) {
  const picked = division.codes.filter((code) => draft.nace.has(code.code)).length;
  if (picked === 0) return "off";
  return picked === division.codes.length ? "on" : "some";
}

/* counts ---------------------------------------------------------------- */

function sumSelected(list, set) {
  return list.reduce((total, item) => total + (set.has(item.code) ? item.count : 0), 0);
}

function sumSelectedNace() {
  let total = 0;
  for (const division of data.nace.divisions) {
    for (const code of division.codes) if (draft.nace.has(code.code)) total += code.count;
  }
  return total;
}

/* summaries ------------------------------------------------------------- */

function summaryNace() {
  const divisions = data.nace.divisions.filter((d) => divisionState(d) !== "off").length;
  return divisions ? `${divisions} oborů · ${nf.format(sumSelectedNace())} firem` : "vše";
}

function summarySizes() {
  if (!draft.sizes.size) return "vše";
  return `${draft.sizes.size} pásem · ${nf.format(sumSelected(data.sizes, draft.sizes))} firem`;
}

function summaryRegions() {
  if (draft.km) return `${draft.km} km od ${draft.from || "—"}`;
  if (!draft.regions.size) return "celá ČR";
  return `${draft.regions.size} krajů · ${nf.format(sumSelected(data.regions, draft.regions))} firem`;
}

// The subtitle of the filters tile: what the next run would use. It
// describes `saved`, never `draft` - the tile has to keep saying what the
// pipeline would do while somebody is editing something else inside the
// panel. Written short on purpose; the panel is one click away for the
// full list.
function renderSummary() {
  if (!countActive(saved)) {
    el.summary.textContent = "Celá ČR, všechny obory a velikosti";
    return;
  }

  const parts = [];
  if (saved.nace.size) parts.push(`<b>${saved.nace.size}</b> oborových kódů`);
  if (saved.sizes.size) parts.push(`<b>${saved.sizes.size}</b> pásem velikosti`);
  if (saved.regions.size) parts.push(`<b>${saved.regions.size}</b> krajů`);
  if (saved.km) parts.push(`<b>${saved.km} km</b> od ${saved.from || "—"}`);
  el.summary.innerHTML = parts.join(" · ");
}

boot();
