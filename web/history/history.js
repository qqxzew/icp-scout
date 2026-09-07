"use strict";

// What the assistant has already handed over, by run, newest first.
//
// The same three layers the week keeps apart:
//
//   normalize()  one shape out of whatever the endpoint returns
//   view()       every "what if this field is missing" answer, in one place
//   render()     markup only - it never decides anything
//
// The split earns itself on exactly one field here. A delivered company
// can have no name, because it left the candidate list after it went out,
// and a renderer that also did the deciding would print "null" next to an
// IČO a salesperson already called. Here the missing name can only ever
// come out as a written reason for its absence, because render() has
// nothing to fall back to.

const HISTORY = "/api/history";
const DOSSIER = "../card/index.html?ico=";

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

/** Czech counted forms: 1 / 2-4 / 5+. The same three lines the week
 *  keeps, for the same reason - "3 firem" in a header reads as machine
 *  output on a page whose subject is careful record-keeping. */
function plural(n, one, few, many) {
  if (n === 1) return one;
  if (n >= 2 && n <= 4) return few;
  return many;
}

/** Czech "how long ago", days only. The archive stamps a delivery to the
 *  second, but the unit the question is asked in is weeks ("co jsme
 *  poslali minulý týden"), and an hour would be precision nobody wants. */
function ago(days) {
  if (!isNumber(days)) return "";
  if (days <= 0) return "dnes";
  if (days === 1) return "včera";
  return `před ${days} dny`;
}

function daysSince(iso) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  return Math.max(0, Math.round((Date.now() - then) / 86400000));
}

/** Date and time in Czech, or null. Null rather than a fallback string:
 *  every timestamp on this page comes from the archive, which writes them
 *  with NOT NULL, so an unparseable one means the shape changed and the
 *  page should say less, not guess more. */
function moment(iso) {
  const when = new Date(iso);
  if (Number.isNaN(when.valueOf())) return null;
  return {
    date: when.toLocaleDateString("cs-CZ", { day: "numeric", month: "long", year: "numeric" }),
    time: when.toLocaleTimeString("cs-CZ", { hour: "2-digit", minute: "2-digit" }),
  };
}

// ---------------------------------------------------------------------------
// normalize
// ---------------------------------------------------------------------------

/** Accepts what /api/history returns ({runs, deliveries, companies}) and
 *  a bare array of runs. Unknown keys are ignored rather than fatal: this
 *  page reads the archive's output and should degrade to fewer fields,
 *  never to a blank screen, when that output gains or loses one.
 */
function normalize(payload) {
  const runs = Array.isArray(payload) ? payload : payload?.runs || [];
  return {
    // Two counts, not one, and they are different questions. A company
    // handed over in three runs is one company and three deliveries, and
    // the whole point of this page is that the second number is the one
    // the salesperson lived through.
    deliveries: isNumber(payload?.deliveries) ? payload.deliveries : null,
    companies: isNumber(payload?.companies) ? payload.companies : null,
    runs: runs.map(runView),
  };
}

function runView(run) {
  const rows = (run.companies || []).map(rowView);
  // The delivery stamp, not the run's start: this page answers "what did
  // we hand over and when", and a run that started at 23:50 handed its
  // five over on the next date. The start still shows in the meta line,
  // because that is the number the log and the results file are filed
  // under.
  const handed = moment(rows[0]?.deliveredAt || run.started_at);
  const started = moment(run.started_at);
  return {
    id: run.run_id ?? null,
    date: handed ? handed.date : "Datum předání neznámé",
    age: ago(daysSince(rows[0]?.deliveredAt || run.started_at)),
    meta: [
      run.run_id != null ? `běh #${run.run_id}` : null,
      started ? `spuštěn ${started.date} ${started.time}` : null,
      `${rows.length} ${plural(rows.length, "firma", "firmy", "firem")}`,
    ].filter(Boolean).join(" · "),
    rows,
  };
}

/** One row's worth of decisions. */
function rowView(row) {
  const ico = String(row.ico || "").padStart(8, "0");
  const marks = [];
  // Said on every occurrence rather than folded into one row with a
  // counter: the fact worth seeing is that this company came up again in
  // a later week, and that is only visible if both weeks still list it.
  if (isNumber(row.times_delivered) && row.times_delivered > 1) {
    marks.push({
      text: `předáno ${row.times_delivered}× celkem`,
      title: "Tato firma byla vydána i v jiném běhu — v seznamu zůstává u každého z nich.",
    });
  }
  // Why the name is missing, said once, next to the row that is missing
  // it. The register moves and the brief changes, so a company can drop
  // out of the pool after it was handed over; that is a state, not an
  // error, and it is not the same state as "we never knew the name".
  if (row.in_candidates === false) {
    marks.push({
      text: "mimo aktuální seznam kandidátů",
      warn: true,
      title: "IČO už není v kandidátech posledního běhu — mohl se změnit rejstřík nebo zadání.",
    });
  }
  return {
    ico,
    name: row.name || null,
    where: [row.city, row.region].filter(Boolean).join(", "),
    deliveredAt: row.delivered_at || null,
    marks,
  };
}

// ---------------------------------------------------------------------------
// render
// ---------------------------------------------------------------------------

const runTemplate = document.getElementById("hs-run-tpl");
const rowTemplate = document.getElementById("hs-row-tpl");
const list = document.getElementById("hs-list");
const sub = document.getElementById("hs-sub");

function renderRun(run) {
  const node = runTemplate.content.firstElementChild.cloneNode(true);
  const find = (name) => node.querySelector(`[data-f="${name}"]`);

  find("date").textContent = run.date;
  find("age").textContent = run.age;
  find("meta").textContent = run.meta;

  const rows = find("rows");
  for (const row of run.rows) rows.appendChild(renderRow(row));
  return node;
}

function renderRow(row) {
  const node = rowTemplate.content.firstElementChild.cloneNode(true);
  const find = (name) => node.querySelector(`[data-f="${name}"]`);

  const name = find("name");
  name.textContent = row.name || "název neznámý";
  if (!row.name) name.dataset.empty = "1";

  find("where").textContent = row.where;
  find("ico").textContent = `IČO ${row.ico}`;

  const open = find("open");
  // The dossier is built from the IČO alone, so it opens even for a
  // company whose name this page could not fill in - which is the row
  // most worth being able to check.
  open.href = DOSSIER + encodeURIComponent(row.ico);
  open.setAttribute("aria-label", `Otevřít podklad: ${row.name || `IČO ${row.ico}`}`);

  const marks = find("marks");
  for (const mark of row.marks) {
    const item = el("li", mark.warn ? "hs-mark hs-mark--warn" : "hs-mark", mark.text);
    if (mark.title) item.title = mark.title;
    marks.appendChild(item);
  }
  return node;
}

/** The screen with no rows on it: a message, and the subtitle rewritten
 *  too - left alone it keeps saying "Načítání…" under a state that has
 *  already been reported. */
function state(className, title, detail, summary) {
  list.textContent = "";
  const box = el("div", `hs-state ${className}`);
  box.appendChild(el("b", null, title));
  box.appendChild(document.createTextNode(detail));
  list.appendChild(box);
  sub.textContent = summary;
}

function describe(data) {
  const bits = [];
  // "předání" is neuter and does not decline across 1 / 2-4 / 5+, so it
  // is the one count here that needs no plural().
  if (isNumber(data.deliveries)) bits.push(`<b>${data.deliveries}</b> předání`);
  if (isNumber(data.companies)) {
    bits.push(`<b>${data.companies}</b> ${plural(data.companies, "firma", "firmy", "firem")}`);
  }
  bits.push(`<b>${data.runs.length}</b> ${plural(data.runs.length, "běh", "běhy", "běhů")}`);
  return bits.join(" · ");
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

(async function main() {
  list.appendChild(el("p", "hs-state", "Načítám historii…"));

  let data;
  try {
    // no-store for the same reason /api/results is fetched that way: a
    // run appends to this list, and a cached copy would show the week
    // before last as if it were everything there is.
    const response = await fetch(HISTORY, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    data = normalize(await response.json());
  } catch (error) {
    state("hs-state--error", "Historii se nepodařilo načíst",
          `Server neodpověděl (${error.message}).`,
          "Historii se nepodařilo načíst.");
    return;
  }

  // Nothing handed over yet is a result, not a failure - and on a fresh
  // installation it is the correct one. Said in a sentence rather than
  // drawn as an empty table with headings over nothing.
  if (!data.runs.length) {
    state("", "Zatím nebyla předána žádná firma",
          "Historie se naplní po prvním dokončeném běhu — každá vydaná firma se sem zapíše i s datem.",
          "Zatím bez záznamů.");
    return;
  }

  sub.innerHTML = describe(data);

  const fragment = document.createDocumentFragment();
  for (const run of data.runs) fragment.appendChild(renderRun(run));
  list.textContent = "";
  list.appendChild(fragment);
})();
