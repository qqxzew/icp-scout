"use strict";

// The week's five, as a catalogue of cards.
//
// Three layers, deliberately separate:
//
//   normalize()  one shape out of whatever the endpoint returns
//   view()       every "what if this field is missing" answer, in one place
//   render()     markup only - it never decides anything
//
// The reason for the split is the failure this component is most likely
// to have: a row arrives with a field the pipeline stopped writing, and a
// renderer that also does the deciding prints "undefined" into a card a
// salesperson then reads on the phone. Here a missing field can only ever
// produce a written reason for its absence, because render() has nothing
// to fall back to.

// One source, and no fixture behind it. A demo file used to sit here as a
// fallback; it meant a broken pipeline still drew five convincing
// companies, which on a screen whose whole subject is "can this be
// trusted" is the worst possible failure. An empty week now says so.
const RESULTS = "/api/results";
// Absolute, not relative: this script draws the page served at "/" since
// the week became the landing screen, and "../card/" from there resolves
// above the site root.
const DOSSIER = "/card/index.html?ico=";
const MAX_CHIPS = 4;

// NO VOCABULARY LIVES HERE. Every Czech word on a card - the class of the
// reason, the production mode, the industry tier - is written by the
// pipeline and arrives in the row, because a second copy of it in this
// file is a copy nobody updates. That is not hypothetical: the mode
// labels were duplicated here once, the pipeline's own names for them
// ("made_to_order", "serial") never matched the guesses, and almost every
// card showed an empty production mode while the dossier one click away
// had it right.

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

/** Czech "how long ago", instrumental case. Days only - the sources
 *  (registry, subsidy file, MPSV) are all dated to the day, and an hour
 *  would be precision nobody measured. */
function ago(days) {
  if (!isNumber(days)) return "";
  if (days <= 0) return "dnes";
  if (days === 1) return "včera";
  return `před ${days} dny`;
}

/** Czech counted forms: 1 / 2-4 / 5+. Worth the three lines - "3 úsudků"
 *  on a card whose whole purpose is to look like careful work reads as
 *  machine output, which is exactly the impression to avoid. */
function plural(n, one, few, many) {
  if (n === 1) return one;
  if (n >= 2 && n <= 4) return few;
  return many;
}

/** Days since an ISO date, for rows that carry the event's date but not
 *  the age the ranking computed. Returns null on anything unparseable
 *  rather than a number derived from NaN. */
function daysSince(iso) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  return Math.max(0, Math.round((Date.now() - then) / 86400000));
}

function money(czk) {
  if (!isNumber(czk)) return null;
  return `${Math.round(czk).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ")} Kč`;
}

// ---------------------------------------------------------------------------
// normalize: one shape out of several
// ---------------------------------------------------------------------------

/** Accepts what select.py::run() returns ({top, qualified}), what
 *  data/ui/results/latest.json holds ({companies}), and a bare array.
 *  Unknown keys are ignored rather than fatal: this page is a reader of
 *  the pipeline's output, and it should degrade to fewer fields, never to
 *  a blank screen, when that output gains or loses one.
 */
function normalize(payload) {
  const all = Array.isArray(payload) ? payload
    : payload?.top || payload?.companies || payload?.rows || [];
  // A company the run held back never gets a card. It has no proven
  // domain or no channel, which means no site was ever read for it, so
  // the card would be a name, an IČO and one line from the register -
  // the "seznam" the brief says the output must not be. run.py stops
  // writing these; this drops any that a file written earlier still
  // carries.
  const rows = all.filter((row) => !(row.undeliverable || []).length
    // And no card for a name nobody can call: a contact block with a
    // person and no channel under it is a blank the salesperson cannot
    // act on. run.py stops writing these too.
    && ((row.contact || {}).email || (row.contact || {}).phone));
  return {
    generated: payload?.generated_at || payload?.generated || null,
    runId: payload?.run_id ?? null,
    // A count from run.py's catalogue(), a list from select.py's own CLI
    // dump. Both mean the same thing and neither should be the reason a
    // header line goes missing.
    qualified: typeof payload?.qualified === "number" ? payload.qualified
      : payload?.qualified?.length ?? null,
    // Held back for want of a proven domain or a channel. A number in the
    // header, never a card: a week of one has to explain itself.
    withheld: typeof payload?.withheld === "number" ? payload.withheld
      : all.length - rows.length,
    rows: forReading(rows.map(view)),
  };
}

/** The five in the order they are worth reading, which is not the order
 *  they were chosen in.
 *
 *  The ranking answers "who deserves the slot" and its seven steps are
 *  argued one by one in select.ordering(); none of that moves. This
 *  answers a different question - which card has the most to read on it -
 *  and it only ever reorders the five that were already chosen. Nobody
 *  enters or leaves the week because of this function.
 *
 *  Sorted on facts with the production mode left out (see
 *  select.is_mode()), and stable, so companies with the same count keep
 *  the ranking's own order. A company whose only facts are mode facts
 *  scores zero and therefore stays exactly where the ranking put it.
 *
 *  Nothing is drawn about this. The card looks exactly as it did; only
 *  the sequence changes, so the number in the corner is the position on
 *  screen and no longer the position in the ranking.
 */
function forReading(rows) {
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => (b.row.signal_facts - a.row.signal_facts) || (a.index - b.index))
    .map((entry) => entry.row);
}

/** One card's worth of decisions. Every branch that answers "what if this
 *  is missing" lives here and nowhere else.
 */
function view(row) {
  const fit = row.fit || {};
  const geo = row.geography || {};
  const reason = row.reason || {};
  const site = row.site_domain ? { domain: row.site_domain, status: row.site_status } : (row.website || {});
  const proven = site.status === "proven";

  // An unclassified row is not class E. E means "some other event", which
  // is a claim about the company; a row that simply never carried a class
  // says so instead of borrowing a label and quietly filing a registry
  // change under "jiná událost".
  const classified = Boolean(reason.class && reason.label);
  const events = row.now_events || row.now || [];
  const lead = events.find((event) => event.value || event.text) || events[0] || {};

  return {
    ico: String(row.ico || "").padStart(8, "0"),
    name: row.name || "(bez názvu v rejstříku)",
    where: [row.city, row.region].filter(Boolean).join(", "),

    classMark: classified ? reason.class : "?",
    classLabel: reason.label || "důvod neklasifikován",
    corroborated: Boolean(reason.corroborated),
    age: ago(reason.age_days ?? lead.age_days ?? daysSince(lead.date)),
    why: lead.value || lead.text || "Událost bez popisu — otevřít podklad.",
    whySource: lead.url || null,
    quote: lead.quote || "",

    facts: [
      { label: "Velikost", value: fit.size || row.size, empty: "neuvedena v RES" },
      { label: "Vzdálenost", ...distance(geo) },
      { label: "Režim", value: fit.mode_label || null, empty: "neurčen" },
      { label: "Obrat", value: money(row.turnover_czk), empty: "nezveřejněn" },
    ],

    chips: chips(row, fit, proven, site),

    contact: contact(row),

    facts_n: row.pain?.verified?.facts ?? row.facts ?? null,
    inferences_n: row.pain?.verified?.inferences ?? row.inferences ?? null,
    // The facts the reading order is built on: everything above minus the
    // production mode. Falls back to the plain count for a results file
    // written before the field existed, so an old week still sorts by
    // something rather than flattening to zero.
    signal_facts: row.pain?.verified?.signal_facts
      ?? row.signal_facts
      ?? row.pain?.verified?.facts
      ?? 0,
  };
}

/** Three states, not two. An unlocated company is not a distant one, and
 *  printing "0 km" or nothing at all for it would say it is near. */
function distance(geo) {
  if (!isNumber(geo.distance_km)) return { value: null, empty: "poloha neurčena" };
  const value = `${Math.round(geo.distance_km)} km`;
  // The radius admits a company on whichever of its addresses is
  // nearest, so this figure can be the seat while the shop floor is
  // hours away. Marked the same way "outside the radius" is - the
  // number stays the answer, the tooltip says what it is a distance to.
  if (isNumber(geo.far_site_km)) {
    return {
      value,
      warn: true,
      title: `Sídlo je ${Math.round(geo.distance_km)} km, ale provozovna`
             + `${geo.far_site ? " " + geo.far_site : ""} je `
             + `${Math.round(geo.far_site_km)} km od ${geo.from || "Plzně"}.`,
    };
  }
  if (geo.preferred !== false) return { value };
  // Marked rather than annotated: the figure is still the answer, and a
  // second word in a 90px column would push the row to two lines on every
  // distant company.
  return { value, warn: true, title: `Mimo preferovaný radius ${geo.limit_km} km od ${geo.from || "Plzně"}.` };
}

function contact(row) {
  const person = row.contact || (row.contacts || [])[0];
  if (!person) return null;
  return {
    name: person.name || null,
    role: person.role || person.role_registered || "",
    email: person.email || null,
    phone: person.phone || null,
  };
}

/** Secondary qualification marks. Ordered by how much they change the
 *  call, and capped in render() - a card that grows a row of chips per
 *  source stops being scannable at the fourth one. */
function chips(row, fit, proven, site) {
  const list = [];
  // Only when the pipeline says it is worth saying: a company in the core
  // of the ICP needs no chip announcing it, and the wording of the other
  // two tiers is the pipeline's, not this file's.
  if (fit.tier_label) list.push({ text: fit.tier_label, warn: true });
  for (const finding of row.negative || []) {
    list.push({ text: finding.reason || String(finding), warn: true });
  }
  if (row.demoted) list.push({ text: "snížená priorita", warn: true });
  // Not a warning and not a boast: this company is in the week because
  // its class of reason would otherwise be missing from it, not because
  // it outranked the company it displaced. Five cards that all look
  // equally earned is the one thing the ranking must not imply.
  if (row.class_slot) list.push({ text: "zástupce třídy " + (row.reason || {}).class });
  for (const sibling of row.group_siblings || []) {
    list.push({ text: `skupina: ${sibling.name || sibling.ico}` });
  }
  if (site.domain) list.push({ text: site.domain + (proven ? "" : " (neprokázaný)"), warn: !proven });
  for (const cert of row.certificates || []) {
    if (cert.standard) list.push({ text: cert.standard });
  }
  return list;
}

// ---------------------------------------------------------------------------
// render
// ---------------------------------------------------------------------------

const template = document.getElementById("wk-card-tpl");

function renderCard(item, index, total) {
  const node = template.content.firstElementChild.cloneNode(true);
  const find = (name) => node.querySelector(`[data-f="${name}"]`);

  node.dataset.class = item.class;
  node.dataset.ico = item.ico;
  node.setAttribute("aria-label", `${index + 1}. ${item.name}`);
  find("rank").textContent = String(index + 1).padStart(2, "0");
  find("sr-rank").textContent = `Pořadí ${index + 1} z ${total}.`;

  find("name").textContent = item.name;
  // IČO and seat on one line, separated the way the dossier separates the
  // parts of an address.
  find("ico").textContent = [`IČO ${item.ico}`, item.where].filter(Boolean).join(" · ");

  const cls = find("class");
  cls.textContent = item.classMark;
  cls.title = item.classLabel;
  let label = item.classLabel;
  if (item.corroborated) {
    // Two different kinds of event at once - the only feature that showed
    // a real lift on the buyer label (1.85). Said in words rather than as
    // a second badge: the card already has one.
    label += " · dvě události";
  }
  find("label").textContent = label;
  find("age").textContent = item.age;

  find("why").textContent = item.why;
  if (item.whySource) find("why-source").appendChild(sourceLink(item.whySource, item.why));
  find("quote").textContent = item.quote;

  const facts = find("facts");
  for (const fact of item.facts) {
    const wrap = el("div", "wk-fact");
    wrap.appendChild(el("dt", null, fact.label));
    const value = el("dd", null, fact.value || fact.empty || "—");
    if (!fact.value) value.dataset.empty = "1";
    if (fact.warn) value.dataset.warn = "1";
    if (fact.title) value.title = fact.title;
    wrap.appendChild(value);
    facts.appendChild(wrap);
  }

  const chipList = find("chips");
  const shown = item.chips.slice(0, MAX_CHIPS);
  for (const chip of shown) {
    const li = el("li", chip.warn ? "wk-chip wk-chip--warn" : "wk-chip", chip.text);
    li.title = chip.text;
    chipList.appendChild(li);
  }
  const hidden = item.chips.slice(MAX_CHIPS);
  if (hidden.length) {
    const more = el("li", "wk-chip wk-chip--more", `+${hidden.length}`);
    more.title = hidden.map((chip) => chip.text).join("\n");
    chipList.appendChild(more);
  }

  find("contact").appendChild(contactBlock(item.contact));

  const evidence = find("evidence");
  if (isNumber(item.facts_n)) {
    evidence.appendChild(el("b", null, item.facts_n));
    evidence.appendChild(document.createTextNode(
      " " + plural(item.facts_n, "ověřený fakt", "ověřené fakty", "ověřených faktů")));
    if (isNumber(item.inferences_n)) {
      evidence.appendChild(document.createTextNode(" · "));
      evidence.appendChild(el("b", null, item.inferences_n));
      evidence.appendChild(document.createTextNode(
        " " + plural(item.inferences_n, "úsudek", "úsudky", "úsudků")));
    }
  }

  const open = find("open");
  open.href = DOSSIER + encodeURIComponent(item.ico);
  open.setAttribute("aria-label", `Otevřít podklad: ${item.name}`);

  return node;
}

/** The dossier's source link: the same icon and the same faint-to-amber
 *  hover, so a line that can be checked looks checkable on both screens. */
function sourceLink(url, label) {
  const a = el("a", "wk-source");
  a.href = url;
  a.target = "_blank";
  a.rel = "noreferrer";
  a.setAttribute("aria-label", `Zdroj: ${label || url}`);
  a.appendChild(icon("external"));
  return a;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#wk-i-${name}`);
  svg.appendChild(use);
  return svg;
}

/** Name, role, channel - and the name is optional.
 *
 *  A company reached only through info@ and a switchboard has no name to
 *  print, and inventing one would be worse than the gap. The role line
 *  then carries what this channel actually is ("obecný kontakt firmy"),
 *  so the card never implies a person who was not found. The channel
 *  itself is the one part that is always there: run.py drops a company
 *  without one rather than sending an empty block.
 */
function contactBlock(person) {
  const wrap = document.createDocumentFragment();
  if (!person) {
    wrap.appendChild(el("p", "wk-role", "Kontakt nedohledán — viz podklad."));
    return wrap;
  }
  if (person.name) wrap.appendChild(el("p", "wk-person", person.name));
  if (person.role) {
    wrap.appendChild(el("p", person.name ? "wk-role" : "wk-person", person.role));
  }
  if (person.email) wrap.appendChild(channel(`mailto:${person.email}`, person.email));
  else if (person.phone) wrap.appendChild(channel(`tel:${person.phone.replace(/\s/g, "")}`, person.phone));
  return wrap;
}

function channel(href, text) {
  const link = el("a", "wk-channel");
  link.href = href;
  link.appendChild(icon("sub"));
  link.appendChild(el("span", null, text));
  return link;
}

// ---------------------------------------------------------------------------
// deck: view switch, navigation, keyboard
// ---------------------------------------------------------------------------

const root = document.querySelector(".wk");
const deck = document.getElementById("wk-deck");
const dots = document.getElementById("wk-dots");
const counter = document.getElementById("wk-counter");
const live = document.getElementById("wk-live");

let cards = [];
let active = 0;
// The card a person asked for, as opposed to the one scrolling happened
// to leave on the left edge. Three cards fit on a wide screen, so after
// End the last card is visible but not leading - and without this the
// observer below would report the leading one while focus sits on the
// last, which is the counter saying 02 while the reader is on 05.
let pinned = null;

const VIEW_KEY = "icp-scout:week-view";

function setView(name) {
  root.dataset.view = name;
  for (const button of document.querySelectorAll("[data-set-view]")) {
    button.setAttribute("aria-pressed", String(button.dataset.setView === name));
  }
  // The carousel role is a lie in grid view - there is nothing to page
  // through - so it is removed rather than left on a static list.
  if (name === "deck") {
    deck.setAttribute("aria-roledescription", "karusel");
  } else {
    deck.removeAttribute("aria-roledescription");
  }
  try { localStorage.setItem(VIEW_KEY, name); } catch { /* private mode */ }
  if (name === "deck") scrollTo(active, "auto");
}

function scrollTo(index, behavior) {
  const card = cards[index];
  if (!card) return;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  deck.scrollTo({ left: card.offsetLeft - deck.offsetLeft, behavior: reduced ? "auto" : (behavior || "smooth") });
}

function setActive(index) {
  if (index === active) return;
  active = index;
  for (const [i, dot] of [...dots.children].entries()) {
    dot.setAttribute("aria-selected", String(i === index));
    dot.tabIndex = i === index ? 0 : -1;
  }
  counter.textContent = `${String(index + 1).padStart(2, "0")} / ${String(cards.length).padStart(2, "0")}`;
  live.textContent = `Firma ${index + 1} z ${cards.length}: ${cards[index]?.dataset.ico || ""}`;
  document.querySelector("[data-scroll='-1']").disabled = index === 0;
  document.querySelector("[data-scroll='1']").disabled = index === cards.length - 1;
}

/** Active card by intersection, not by scroll offset: no handler runs per
 *  scrolled pixel, so the deck stays on the compositor while a finger is
 *  still on it. */
function watch() {
  // A wide deck shows three cards at once, so "is intersecting" describes
  // three of them and the last entry in the batch would win by accident -
  // that is how the counter opened on 04 / 05. The leading card is the
  // one the deck is snapped to, so the active index is the lowest
  // currently-visible one, kept in a set across callbacks.
  const visible = new Set();
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const index = cards.indexOf(entry.target);
      if (index < 0) continue;
      if (entry.isIntersecting && entry.intersectionRatio > 0.6) visible.add(index);
      else visible.delete(index);
    }
    if (pinned !== null && !visible.has(pinned)) pinned = null;
    if (visible.size) setActive(pinned ?? Math.min(...visible));
  }, { root: deck, threshold: [0.61] });
  for (const card of cards) observer.observe(card);
}

function buildDots() {
  dots.textContent = "";
  cards.forEach((card, index) => {
    const dot = el("button", "wk-dot");
    dot.type = "button";
    dot.setAttribute("role", "tab");
    dot.setAttribute("aria-selected", String(index === 0));
    dot.setAttribute("aria-label", `Firma ${index + 1}`);
    dot.tabIndex = index === 0 ? 0 : -1;
    dot.addEventListener("click", () => focusCard(index));
    dots.appendChild(dot);
  });
}

/** Arrow keys inside the tablist move between dots and keep focus there.
 *  onKey() cannot serve here: it moves focus onto the card, which throws a
 *  keyboard user out of the control they were operating. */
function onDotKey(event) {
  const step = { ArrowRight: 1, ArrowLeft: -1 };
  let next = null;
  if (event.key in step) next = active + step[event.key];
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = cards.length - 1;
  if (next === null) return;

  next = Math.min(cards.length - 1, Math.max(0, next));
  event.preventDefault();
  pinned = next;
  setActive(next);
  scrollTo(next);
  dots.children[next]?.focus();
}

function focusCard(index) {
  const card = cards[index];
  if (!card) return;
  pinned = index;
  setActive(index);
  scrollTo(index);
  // focus() would scroll the card into view its own way and fight the
  // snap; preventScroll leaves the positioning to scrollTo above.
  card.focus({ preventScroll: true });
}

function onKey(event) {
  if (root.dataset.view !== "deck") return;
  const step = { ArrowRight: 1, ArrowLeft: -1 };
  if (event.key in step) {
    event.preventDefault();
    focusCard(Math.min(cards.length - 1, Math.max(0, active + step[event.key])));
  } else if (event.key === "Home") {
    event.preventDefault();
    focusCard(0);
  } else if (event.key === "End") {
    event.preventDefault();
    focusCard(cards.length - 1);
  }
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

/** The screen with no cards on it: a message, and nothing left over from
 *  the state that expected cards. `data-empty` is what hides the arrows -
 *  they stayed live over an error message and paged through nothing - and
 *  the subtitle is rewritten too, or it keeps saying "Načítání…" under a
 *  failure that has already been reported. */
function state(className, title, detail, summary) {
  deck.textContent = "";
  const box = el("div", `wk-state ${className}`);
  box.appendChild(el("b", null, title));
  box.appendChild(document.createTextNode(detail));
  deck.appendChild(box);
  dots.textContent = "";
  counter.textContent = "";
  cards = [];
  root.dataset.empty = "1";
  document.getElementById("wk-sub").textContent = summary;
}

/** A line of text, not three grey boxes pretending to be cards. The rest
 *  of the site says "Načítám výsledky…" in one line and so does this. */
function loading() {
  deck.textContent = "";
  deck.appendChild(el("p", "wk-state", "Načítám výsledky…"));
}

/** The last run, and nothing else. */
async function load() {
  // no-store, and not as a precaution: /api/results is one path whose
  // contents change with every run, and the browser served a cached copy
  // of it live - the page showed a previous week's single company while
  // this week's five sat on disk. A results screen that can go stale
  // silently is worse than one that fails loudly.
  const response = await fetch(RESULTS, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return normalize(await response.json());
}

function describe(data) {
  const bits = [];
  if (data.generated) {
    const when = new Date(data.generated);
    if (!Number.isNaN(when.valueOf())) bits.push(`Běh z ${when.toLocaleDateString("cs-CZ")}`);
  }
  if (isNumber(data.qualified)) bits.push(`z <b>${data.qualified}</b> firem s událostí v okně`);
  bits.push(`vydáno <b>${data.rows.length}</b>`);
  if (data.withheld) {
    bits.push(`<b>${data.withheld}</b> vynecháno (bez prokázaného webu nebo kontaktu)`);
  }
  return bits.join(" · ");
}

(async function main() {
  try { setView(localStorage.getItem(VIEW_KEY) === "grid" ? "grid" : "deck"); } catch { setView("deck"); }

  for (const button of document.querySelectorAll("[data-set-view]")) {
    button.addEventListener("click", () => setView(button.dataset.setView));
  }
  for (const button of document.querySelectorAll("[data-scroll]")) {
    button.addEventListener("click", () => {
      focusCard(Math.min(cards.length - 1, Math.max(0, active + Number(button.dataset.scroll))));
    });
  }
  deck.addEventListener("keydown", onKey);
  dots.addEventListener("keydown", onDotKey);

  // A hand on the deck outranks the last button press. Bound to the
  // gestures that start a scroll rather than to `scroll` itself: the
  // programmatic scroll from focusCard() fires that event too and would
  // clear the pin it had just set.
  for (const gesture of ["wheel", "touchstart", "pointerdown"]) {
    deck.addEventListener(gesture, () => { pinned = null; }, { passive: true });
  }

  loading();

  let data;
  try {
    data = await load();
  } catch (error) {
    state("wk-state--error", "Výsledky nejsou dostupné",
          `Poslední běh se nepodařilo načíst (${error.message}). Spusťte python -m pipeline.run.`,
          "Data posledního běhu se nepodařilo načíst.");
    return;
  }

  // An empty week is a result, not a failure - and it is stated as one,
  // with no stand-in companies. The brief's own line: better to hand over
  // less than to lie.
  if (!data.rows.length) {
    state("", "Tento týden neprošla žádná firma",
          "V okně nebyla u kvalifikovaných firem žádná datovaná událost. Firmy zůstávají na čekací listině.",
          "Bez výsledků.");
    return;
  }

  delete root.dataset.empty;
  document.getElementById("wk-sub").innerHTML = describe(data);

  const fragment = document.createDocumentFragment();
  data.rows.forEach((item, index) => fragment.appendChild(renderCard(item, index, data.rows.length)));
  deck.textContent = "";
  deck.appendChild(fragment);

  cards = [...deck.querySelectorAll(".wk-card")];
  buildDots();
  active = -1;
  setActive(0);
  watch();
})();

/* the top bar ------------------------------------------------------------ */

// The run button in the navigation, driven by the same module the brief
// screen uses. Mounted here rather than inline in the page because this
// file is what the week is made of; run-control.js only needs to be told
// which nodes to write into and what to do when a run ends.
//
// onFinished reloads instead of navigating: the week already is this
// page, and re-reading /api/results is the whole difference between the
// week that was on screen and the one the run just wrote.
mountRunControl({
  button: document.getElementById("wk-run"),
  title: document.getElementById("wk-run-title"),
  sub: document.getElementById("wk-run-sub"),
  since: document.getElementById("since"),
  sinceValue: document.getElementById("since-value"),
  onFinished: () => { window.location.reload(); },
}) && fetch("/api/run")
  .then((r) => r.json())
  // Drawn on load so "Poslední běh před 3 h" is there before anybody
  // presses anything, and so a reload landing on a run already in
  // progress picks its polling back up.
  .then((state) => {
    const button = document.getElementById("wk-run");
    if (state.running) button.click();
    const since = document.getElementById("since");
    const value = document.getElementById("since-value");
    if (!state.last_run || !state.last_run.generated_at || !since || !value) return;
    since.hidden = false;
    value.textContent = new Date(state.last_run.generated_at).toLocaleString("cs-CZ");
  })
  .catch((error) => console.error("Could not read the run state", error));
