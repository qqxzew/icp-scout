"use strict";

// Renders one company's dossier from GET /api/card/{ico} - the JSON shape
// pipeline/scoring/card.py::for_web() produces. Nothing here decides what
// counts as a fact, which quote is worth showing, or how a link is built:
// those are calls the pipeline already made once, and this file's only
// job is to walk the rows it returns and turn them into the same markup
// for any IČO, not to re-derive anything.

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function sourceLink(url, label) {
  if (!url) return el("span", "dossier-source-empty");
  const a = el("a", "dossier-source");
  a.href = url;
  a.target = "_blank";
  a.rel = "noreferrer";
  a.setAttribute("aria-label", `Zdroj: ${label || url}`);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#icon-external");
  svg.appendChild(use);
  a.appendChild(svg);
  return a;
}

function subIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "dossier-sub-icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#icon-sub");
  svg.appendChild(use);
  return svg;
}

// One row: a label (or, for a sub-row, a connector icon plus a label),
// a value with an optional note or pill, and a source link pinned to the
// right - or an empty spacer when there is nothing to link, so every
// row's link column still lines up.
function buildRow(row) {
  const wrap = el("div", row.sub ? "dossier-row dossier-row--sub" : "dossier-row");

  const label = el("span", row.sub ? "dossier-label dossier-label--sub" : "dossier-label");
  if (row.sub) label.appendChild(subIcon());
  label.appendChild(document.createTextNode(row.label || ""));
  wrap.appendChild(label);

  const main = el("span", "dossier-main");
  if (row.value) {
    main.appendChild(valueText(row));
    if (row.note) {
      const note = el("span", "dossier-note", row.note);
      main.appendChild(note);
    }
    if (row.pill) {
      const pillClass = row.pill === "proven" ? "dossier-pill dossier-pill--proven" : "dossier-pill";
      main.appendChild(el("span", pillClass, row.pill));
    }
  } else {
    main.classList.add("dossier-empty");
    main.textContent = row.empty_note || "—";
  }
  wrap.appendChild(main);

  wrap.appendChild(sourceLink(row.source, row.label));
  return wrap;
}

// The value itself, tinted when the pipeline flagged it as the model's
// reading rather than something a register states or a quote proves.
// The tint sits on the text, not on the row, so it reads as a marker pen
// over the words in question and not as a highlighted line. The reason is
// carried in `title` - `inferred_note` is written per case in card.py,
// because "no register carries this" and "no verbatim quote backs this"
// are different admissions and should not collapse into one tooltip.
function valueText(item) {
  if (!item.inferred) return document.createTextNode(item.value);
  const mark = el("span", "dossier-mark", item.value);
  if (item.inferred_note) mark.title = item.inferred_note;
  return mark;
}

function buildClaim(claim) {
  const wrap = el("div", "dossier-claim");
  const row = el("div", "dossier-row");

  // The claim's Czech label goes in the label column like every other
  // row's does, now that no badge is competing for that slot - so the
  // evidence lines up with the registry lines above it instead of
  // starting at its own indent.
  row.appendChild(el("span", "dossier-label", claim.label));

  const main = el("span", "dossier-main");
  main.appendChild(valueText(claim));
  row.appendChild(main);
  row.appendChild(sourceLink(claim.source, claim.label));
  wrap.appendChild(row);

  if (claim.quote) {
    wrap.appendChild(el("p", "dossier-quote", claim.quote));
  }
  return wrap;
}

function renderCard(root, data) {
  root.textContent = "";
  root.classList.remove("dossier--status");

  const head = el("div", "dossier-head");
  head.appendChild(el("h1", "dossier-name", data.name || "(bez názvu)"));
  head.appendChild(el("span", "dossier-ico", `IČO ${data.ico}`));
  root.appendChild(head);

  const rows = el("div", "dossier-rows");
  data.rows.forEach((row) => rows.appendChild(buildRow(row)));
  root.appendChild(rows);

  data.sections.forEach((section) => {
    const wrap = el("div", "dossier-section");
    wrap.appendChild(el("h2", "dossier-section-title", section.title));
    if (section.rows) {
      section.rows.forEach((row) => wrap.appendChild(buildRow(row)));
    }
    if (section.claims) {
      section.claims.forEach((claim) => wrap.appendChild(buildClaim(claim)));
    }
    root.appendChild(wrap);
  });

  const foot = el("footer", "dossier-foot");
  const f = data.footer;
  foot.appendChild(el("span", null, `${f.facts} ${f.facts_label}`));
  foot.appendChild(el("span", null, `${f.inferences} ${f.inferences_label}`));
  foot.appendChild(el("span", null, `${f.discarded} zahozeno při ověření`));
  if (f.discounted) {
    foot.appendChild(el("span", null, `${f.discounted} nezapočteno (mimo signál)`));
  }
  root.appendChild(foot);
}

function renderStatus(root, text, isError) {
  root.textContent = "";
  root.classList.add("dossier--status");
  root.appendChild(el("p", isError ? "dossier-status dossier-status--error" : "dossier-status", text));
}

function renderPicker(root) {
  root.textContent = "";
  root.classList.add("dossier--status");
  root.appendChild(el("p", "dossier-status", "Zadejte IČO firmy, jejíž kartu chcete otevřít."));

  const form = el("form", "dossier-picker");
  const input = el("input");
  input.type = "text";
  input.name = "ico";
  input.placeholder = "např. 47541717";
  input.autocomplete = "off";
  const button = el("button", null, "Otevřít");
  button.type = "submit";

  form.appendChild(input);
  form.appendChild(button);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) return;
    location.search = `?ico=${encodeURIComponent(value)}`;
  });
  root.appendChild(form);
}

(function main() {
  const root = document.getElementById("dossier");
  const ico = new URLSearchParams(location.search).get("ico");

  if (!ico) {
    renderPicker(root);
    return;
  }

  renderStatus(root, "Načítání karty…");
  fetch(`/api/card/${encodeURIComponent(ico.trim())}`)
    .then(async (response) => {
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${response.status}`);
      }
      return response.json();
    })
    .then((data) => renderCard(root, data))
    .catch((error) => renderStatus(root, `Kartu se nepodařilo načíst: ${error.message}`, true));
})();
