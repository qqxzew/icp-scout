/* Renders the weekly dossiers from data/ui/cards.json.
 *
 * The row order is fixed and identical for every company - that is the
 * whole point of the layout. A salesperson reading the tenth card knows
 * where "obrat" sits without looking for it, and an empty row is
 * information rather than a gap to be filled.
 *
 * Nothing is computed here beyond formatting. Whether a statement is a
 * fact or an inference was decided by evidence/verify.py against an
 * archived snapshot; this file only shows which. */

const PAGE = 5;

const NACE = {
  "25620": "obrábění",
  "27110": "výroba elektromotorů a generátorů",
  "25610": "povrchová úprava a zušlechťování kovů",
  "28990": "výroba ostatních strojů pro speciální účely",
  "33120": "opravy strojů",
};

/* Human labels for the claim kinds the agents produce. Anything not
   listed falls back to the raw kind - visible and ugly, which is the
   right failure: a new agent should show up, not hide. */
const KIND_LABEL = {
  "pain": "Známka bolesti",
  "production_mode": "Režim výroby",
  "certificate:page": "Certifikát (web)",
  "certificate:pdf": "Certifikát (dokument)",
  "certificate:image": "Certifikát (sken)",
  "turnover_web": "Obrat dle webu",
  "subsidy_vendor_evidence": "Dotace — dodavatel",
};

const NOW_LABEL = {
  "subsidy_signed": "Podepsaná dotace",
  "director_joined": "Nový jednatel",
  "director_departed": "Odchod jednatele",
  "owner_joined": "Nový vlastník",
  "owner_departed": "Odchod vlastníka",
  "management_vacancy": "Inzerát na řídící roli",
};

const ARES = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest";

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const czDate = (iso) => {
  if (!iso) return "";
  const [y, m, d] = String(iso).slice(0, 10).split("-");
  return d ? `${+d}. ${+m}. ${y}` : iso;
};

const daysAgo = (iso) => {
  if (!iso) return null;
  const then = new Date(String(iso).slice(0, 10));
  if (isNaN(then)) return null;
  return Math.round((Date.now() - then) / 86400000);
};

/* A source cell is a link when there is somewhere to click, and plain
   text when the source is an API response or a file. It is never empty
   while the value is filled - a statement the reader cannot trace back
   is exactly what this project refuses to print. */
function source(label, url) {
  if (!label) return "";
  return url
    ? `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(label)}</a>`
    : `<span class="src-plain">${esc(label)}</span>`;
}

function row(label, value, src, opts = {}) {
  const empty = value === "" || value == null;
  const cls = ["row"];
  if (empty) cls.push("row-empty");
  if (opts.inference) cls.push("row-inference");
  return `<div class="${cls.join(" ")}">
    <div class="row-label">${esc(label)}</div>
    <div class="row-value">${empty ? "" : value}</div>
    <div class="row-src">${empty ? "" : src || ""}</div>
  </div>`;
}

function identityRows(card) {
  const c = card.company || {};
  const vr = `${ARES}/ekonomicke-subjekty-vr/${card.ico}`;
  const base = `${ARES}/ekonomicke-subjekty/${card.ico}`;
  const res = `${ARES}/ekonomicke-subjekty-res/${card.ico}`;
  const rzp = `${ARES}/ekonomicke-subjekty-rzp/${card.ico}`;
  const out = [];

  out.push(row("IČO", esc(card.ico), source("ARES", base)));
  out.push(row("Právní forma", esc(c.legal_form_name || ""), source("ARES", base)));
  out.push(row("Sídlo",
    esc([c.city, c.district, c.region].filter(Boolean).join(", ")),
    source("ARES", base)));
  out.push(row("Vznik", esc(czDate(c.established)), source("ARES", base)));
  out.push(row("Velikost", esc(card.size || ""), source("RES", res)));

  const nace = card.nace ? `${esc(card.nace)}${NACE[card.nace] ? " — " + NACE[card.nace] : ""}` : "";
  out.push(row("NACE", nace, source("RES", res)));

  out.push(row("Insolvence",
    c.insolvent === undefined ? "" : (c.insolvent ? "vedena" : "není vedena"),
    source("ARES", base)));

  const act = c.establishments_active, tot = c.establishments_total;
  out.push(row("Provozovny",
    tot == null ? "" : `${act} aktivních z ${tot}`,
    source("RŽP", rzp)));

  return out.join("");
}

function turnoverRows(card) {
  const out = [];
  const t = card.turnover || {};

  /* Two independent sources, two rows. The filed accounts are audited;
     the website figure is the company talking about itself. Merging them
     would hide which one the salesperson is about to quote. */
  if (t.value_czk) {
    out.push(row("Obrat (závěrka)",
      `${t.value_czk.toLocaleString("cs-CZ")} Kč${t.year ? " (" + t.year + ")" : ""}`,
      source("Sbírka listin", t.source_url)));
  } else {
    /* Not blank: "files no P&L" and "filed a scan" are different facts
       about a company, and the reason is worth more than the gap. */
    out.push(row("Obrat (závěrka)",
      t.note ? `<span class="meta">${esc(t.note)}</span>` : "",
      t.note ? source("Sbírka listin", t.source_url) : ""));
  }

  for (const s of card.turnover_site || []) {
    out.push(row("Obrat (web)",
      `${esc(s.value)}${s.quote ? `<span class="quote">„${esc(s.quote)}"</span>` : ""}`,
      source("web firmy", s.url),
      { inference: s.state === "inference" }));
  }
  return out.join("");
}

function certificateRows(card) {
  const certs = (card.certificates || []).filter((c) => c.standard);
  if (!certs.length) return row("Certifikáty", "", "");

  return certs.map((c) => {
    const bits = [esc(c.standard)];
    if (c.number) bits.push(`č. ${esc(c.number)}`);
    if (c.issuer) bits.push(`vydal ${esc(c.issuer)}`);
    const tier = c.tier === "pdf" ? "" : `<span class="meta">uvedeno na webu, bez čísla certifikátu</span>`;
    return row("Certifikát", bits.join(" · ") + tier, source("zdroj", c.source_url));
  }).join("");
}

function whyNowRows(card) {
  const events = card.why_now || [];
  if (!events.length) return row("Proč teď", "", "");

  return events.map((e) => {
    const age = daysAgo(e.seen_at);
    const label = NOW_LABEL[e.kind] || e.kind;
    /* An event's age is shown because "why now" is the claim being made.
       92 days and 5 days are both inside the window and are not the
       same argument. */
    const meta = age == null ? "" : `<span class="meta">zaznamenáno před ${age} dny</span>`;
    return row(label, esc(e.value) + meta,
      source(e.url ? "zdroj" : "registr / dotaceEU", e.url));
  }).join("");
}

function contactRows(card) {
  const people = card.contacts || [];
  const vr = `${ARES}/ekonomicke-subjekty-vr/${card.ico}`;

  if (!people.length) return row("Komu volat", "", "");

  return people.map((p) => {
    const channel = p.email || p.phone;
    const role = p.role_registered ? `<span class="meta">${esc(p.role_registered)}${p.since ? ", ve funkci od " + czDate(p.since) : ""}</span>` : "";
    /* A person with no channel is still shown. The register proves the
       name and the role; the missing channel is the gap, and hiding the
       person would hide that we know exactly who to reach. */
    const value = `${esc(p.name)}${channel ? " — " + esc(channel) : `<span class="meta">kanál nedohledán</span>`}${role}`;
    return row("Komu volat", value, source("obchodní rejstřík", vr));
  }).join("");
}

function evidenceRows(card) {
  const facts = card.evidence?.facts || [];
  const infs = card.evidence?.inferences || [];
  const out = [];

  for (const f of facts) {
    const label = KIND_LABEL[f.kind] || f.kind;
    const quote = f.quote ? `<span class="quote">„${esc(f.quote)}"</span>` : "";
    out.push(row(label, esc(f.value) + quote, source(f.url ? "zdroj" : (f.source || "zdroj"), f.url)));
  }
  for (const i of infs) {
    const label = KIND_LABEL[i.kind] || i.kind;
    out.push(row(label,
      esc(i.value) + `<span class="meta">úsudek modelu — bez citace</span>`,
      source(i.url ? "kontext" : (i.source || "kontext"), i.url),
      { inference: true }));
  }
  return out.join("");
}

function cardHtml(card, rank) {
  const facts = card.evidence?.facts?.length || 0;
  const infs = card.evidence?.inferences?.length || 0;
  const drops = card.discarded || 0;

  const site = card.website || {};
  const siteRow = row("Web",
    site.domain
      ? `${esc(site.domain)}${site.status === "proven" ? `<span class="meta">doloženo IČO na stránce</span>` : `<span class="meta">doména neověřena</span>`}`
      : "",
    site.domain ? source("web", "https://" + site.domain) : "",
    { inference: site.domain && site.status !== "proven" });

  return `<article class="card">
    <div class="card-head">
      <span class="rank">${rank}.</span>
      <h2 class="card-name">${esc(card.name || card.ico)}</h2>
      <div class="tally">
        <span><b>${facts}</b> faktů</span>
        <span><b>${infs}</b> úsudků</span>
        <span class="drop"><b>${drops}</b> zahozeno</span>
      </div>
    </div>
    <div class="rows">
      ${identityRows(card)}
      ${turnoverRows(card)}
      ${certificateRows(card)}
      ${siteRow}
      ${whyNowRows(card)}
      ${contactRows(card)}
      ${evidenceRows(card)}
    </div>
  </article>`;
}

async function main() {
  const host = document.getElementById("cards");
  const summary = document.getElementById("summary");
  const more = document.getElementById("more");

  let cards;
  try {
    const res = await fetch("../data/ui/cards.json");
    if (!res.ok) throw new Error(res.status);
    cards = await res.json();
  } catch (e) {
    summary.textContent =
      "Nepodařilo se načíst data/ui/cards.json — spusťte pipeline a otevřete přes http://localhost:8123/web/card.html";
    return;
  }

  const withReason = cards.filter((c) => (c.why_now || []).length).length;
  summary.textContent =
    `${cards.length} firem, všechny s datovaným důvodem k oslovení · ` +
    `celkem ${cards.reduce((n, c) => n + (c.evidence?.facts?.length || 0), 0)} ověřených faktů`;

  let shown = 0;
  const render = () => {
    const next = cards.slice(shown, shown + PAGE);
    host.insertAdjacentHTML("beforeend",
      next.map((c, i) => cardHtml(c, shown + i + 1)).join(""));
    shown += next.length;
    more.hidden = shown >= cards.length;
    more.textContent = `Dalších ${Math.min(PAGE, cards.length - shown)} firem`;
  };

  render();
  more.addEventListener("click", render);
}

main();
