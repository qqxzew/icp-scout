# icp-scout — architecture

Why the thing is built this way. Code comments point here by section
number instead of restating the reasoning at every call site.

---

## 1. What this is

A working prototype of a sales assistant. In: an ICP — the profile of an
ideal customer. Out: **five companies a week**, each with a prepared
brief a salesperson can act on — why this company, why now, and whom to
call.

The operative word is *prepared brief, not a list*. For every company it
must be visible not only **that** the assistant picked it, but **why**,
**what it concluded that from**, and **what to do next**. Traceability of
the output is treated as equal in weight to the output itself: the
salesperson has to be able to see quickly where the assistant was wrong.

**The assistant sends nothing on its own. The last word is always a
human's.**

Constraints it is built under:

- **No paid databases.** Open registers, company websites, job portals,
  news. Paid enrichment is a possible extension, not a premise.
- **No scraping behind a login wall** (LinkedIn and the like) — neither
  technically nor under those services' terms.
- **Hallucination is the main risk of the task.** An invented claim about
  a company, pasted by a salesperson into an e-mail, is worse than no
  output at all. Section 3 is the answer to that, and it is the part of
  the project worth reading first.
- A repeated weekly run means the agent has to remember what it already
  handed over, and what to do with companies that had no reason to call
  last time.

Sources are Czech: the RES bulk register, ARES, MPSV vacancies, EU
subsidies, the NEN procurement profile, Sbírka listin, company websites.

---

## 2. The brief: an ICP is input, not code

Stage 00 is a filter screen (`web/`), saved to `web/icp.json`, read by
`pipeline/run.py`. The task itself requires accepting a profile *from the
salesperson*, so the profile is data, not a constant — `run.py` only
ships a default so the screen does not open empty.

What actually filters, and what only enriches:

| Criterion | Role |
|---|---|
| Headcount band (`KATPO` from RES) | **filter** |
| NACE | **filter**, but only narrows the field — it does not tell made-to-order from serial production |
| Legal form (s.r.o. / a.s.) | **filter** — a sole trader gets no B2B relief under GDPR |
| Geography | **filter with a radius**, see below |
| Turnover | **not in selection.** Coverage measured at 16.5 % on 200 companies, and the headcount filter runs first, so turnover could only ever subtract candidates. It stays on the card |
| Industry, budget, process list | **not criteria** — either not published anywhere, or true of every manufacturer |

**NACE — why not "section C only".** The five example customers in the
profile sit in three different sections, so restricting to manufacturing
would contradict the brief. The set is 19 divisions: made-to-order
production (16, 18, 22, 23, 25, 26, 27, 28, 31, 32, 33) plus field
service (38, 41, 42, 43, 49, 77, 81, 95). The largest excluded code is
46 wholesale — trade does not distribute resources over time.

**Geography is a priority in the document ("preferovaně", not "pouze")
but a promise on the screen.** A radius drawn in the interface is a
statement about what will come back, so `filters/brief.py` enforces it
strictly — 150 ± 5 km, or the chosen regions, no exceptions — whoever set
the number.

**Distance is measured to the nearest of a company's addresses, not to
its registered seat.** A company has a `sídlo` and, if it registered any,
`provozovny`; the register does not say which one is the shop floor. Both
errors are expensive: by seat alone, one company entered a week's five
with "87 km from Prague" while all three of its sites were in Moravia
(nearest 234 km); by establishments alone, every company whose shop floor
is at its seat would drop out. So admission is generous and the card is
obliged to print the second number.

---

## 3. Evidence: three states of a fact, decided by code

**This is the core of the project.** Traceability is a *function*, not
documentation — it cannot be written in afterwards, because it changes
how the agent is built.

Not a confidence score (a number picked out of the air and impossible to
defend), but three states:

| State | Condition | What the salesperson sees |
|---|---|---|
| **Fact** | the quote was found in the downloaded page text | claim + URL + date + quote |
| **Inference** | there is no quote, or the claim is derived from one | marked as an inference, with the quote it rests on |
| **Discard** | the model returned a quote that is not on the page | goes no further |

An acceptable inference: "the production type is unknown, but plants like
this are usually small-batch." The inference is not lost, but it does not
pass itself off as a fact.

### The quote is checked by code, not by a critic

The model returns **claim + quote + URL**. Code looks for that quote in
the downloaded page text. Found → it passes. Not found → discarded.
Ordinary string comparison.

**The model is not checking itself — my code is checking the model.**

Rejected alternatives:

- *Telling the model in the prompt not to be overconfident.* There is no
  way to check whether it complied; you would end up verifying by hand,
  which is exactly the work the agent is supposed to remove. **A prompt
  asks for good behaviour; architecture makes bad behaviour impossible.**
- *A second LLM as critic in the first line.* Slower, more expensive, and
  it hallucinates too. Possible as a second layer, not as the mechanism.

Both kinds live in one model response — verifiable data and
non-verifiable inference — so **every claim is checked separately, never
the response as a whole.**

### An archive, not a cache

The downloaded text is not only needed during the run; it **is the
evidence**. A week later the salesperson asks "where does this come
from?" and the page has changed. So what is stored is a **snapshot + URL
+ date**.

`evidence/archive.py` is SQLite plus snapshots on disk addressed by
`sha256` of their content. The load-bearing line is
`claim.snapshot_id NOT NULL REFERENCES snapshot(id)`: a claim about a
company with no snapshot behind it cannot physically be inserted.

Text is stored normalised, by the same function the verifier uses —
otherwise a quote the model found would not be found in the archive.
Czech pages are full of non-breaking spaces, and that alone breaks naive
matching.

---

## 4. The pipeline

The order is not arbitrary: **cheap and structural first, expensive and
dirty last.** Negative filters and geography run *before* the steps that
go out to the internet, or a full search would cost a fortune.

```
00 Input        web/ — the ICP as filters in the interface, not in code
   ↓ size · NACE · radius
01 Selection    res_data.csv (bulk RES) → filtered locally, zero HTTP
   ↓ long list of IČO
02 Qualification ARES by IČO, four GET endpoints
   ↓ dropped before the expensive steps
03 Rejection    negative filters: insolvency, non-s.r.o./a.s., vendor
                reference lists
   ↓ qualified companies
04 Signals      MPSV vacancies, website, NEN tenders, EU subsidies
   ↓ claims + quotes
   ⟷ CROSS-CUTTING LAYER: evidence (archive + quote verifier)
   ↓ only the claims that survived
05 Scoring      three independent scores FIT / PAIN / NOW + delivery gate
   ↓ the week's five
05.5 Turnover   Sbírka listin for those five. Does NOT affect selection
   ↓ the five, with turnover wherever it is published at all
06 Output       five cards a week
```

**The evidence layer is not a pipeline stage.** It sits underneath every
stage that goes out to the internet.

---

## 5. Scoring and selection

Three independent scores, not one combined number. Fitness and timing are
two different things; a single score glues them together, and "great
company, wrong moment" is not the same as "wrong company".

| Score | Question | Built from |
|---|---|---|
| **FIT** | why this company | the brief as a hard filter, then NACE tier × production mode |
| **PAIN** | what we can prove | **does not rank** — it fills the card |
| **NOW** | why this week | a dated event: register, subsidy, tender, vacancy |

**PAIN no longer decides selection, and that is a measured result rather
than a simplification.** A sum of eight terms behaved like one: the
`facts` term carried 62.6 % of the points and correlated with the number
of pages downloaded at +0.81, and out of 500 random weightings, 35 % of
them produced the same five companies. Two experiments then showed no
best weights exist: against 35 companies known to have bought such a
system, neither our components (p = 0.72…1.00) nor a 1536-dimension
website embedding (AUC 0.392) separate buyers from non-buyers. **The
ceiling is in the source** — a company's public website does not say
whether it is buying a system.

**Selection order** (`scoring/select.py::ordering`):

```
FIT group → negative finding → reason class → corroboration
          → radius → freshness within class → number of facts (tie-break)

reason class:  A subsidy with no procurement started  (the best case)
               B change in management
               C open procurement
               D vacancy for a planning role
               E everything else, including "already bought"
```

**A quota per reason class** (`select.py::spread_classes`). The ordering
sorts by class strictly, so a week with five management changes handed
over five management changes and classes A, C, D, E were unreachable. The
best row of **each** class takes a slot before any class takes a second.
This is not diversity for its own sake: calibration is closed for lack of
labels, and a week of five identical reasons is one experiment run five
times. The price is stated out loud — a company on a reserved slot ranks
*below* the one it displaced, and is required to print that on its card.

**Delivery gate (hard).** A company reaches the salesperson only if it
has a **proven** domain (not merely probable) and **at least one channel**
— e-mail or phone. The cost is that roughly 11 % of the base can never be
delivered; the alternative was worse. One company entered a week's five
on a guessed domain that belonged to a private individual and dragged his
contact details along with it.

---

## 6. State between runs

Three fates for a company:

- **Failed qualification** — never re-checked.
- **Qualified, no reason to call** — goes to a waiting list. On the next
  run only the *signals* are re-checked: size, region and production mode
  do not change in a week. The base becomes an asset over time.
- **Handed over** — there is no feedback loop, because the agent sends
  nothing itself.

**Change detection: the event finds the company, not the other way
round.** The agent does not walk every company looking for events; a
change arrives as a daily register batch and is matched against the base
by IČO. Cost does not grow with the size of the base, the signal carries
a date from a state register, and it turns up companies not yet in the
base at all.

**Limit:** the register only covers ownership and statutory-body changes.
"The system is struggling" and "the company outgrew what one person can
hold in their head" never appear there, so two mechanisms are needed —
events arriving on their own, plus an active sweep of vacancies and news.

---

## 7. GDPR: the company is profiled, not the person

Not a form of words — a split in the data:

- every fact, signal, score and history is attached to an **IČO**;
- a person's name appears **only in the contact block**: name, function,
  link to the register;
- **no** scores, notes, history or cross-source joining about a person.

Legitimate interest does not cover advanced profiling that joins data
about a human being across sources. A profile of a legal entity raises no
such problem — GDPR does not apply to companies.

Consequences: sole traders are excluded; personal-format addresses
(`firstname.surname@`) are flagged, since those people need a way to
object; an e-mail address is **never constructed** from a name, only
matched against one printed on the page.

---

## 8. Repository layout

Each source is a separate module behind one interface — the list is open,
and adding a new one must not touch anything but its own file.
`evidence/` deliberately sits outside `sources/`: it is not a source of
data, it is what checks all the others.

```
icp-scout/
├── pipeline/
│   ├── sources/                 # one interface, many implementations
│   │   ├── res_bulk.py          # res_data.csv → the primary selection
│   │   ├── ares.py              # 4 GET endpoints by IČO
│   │   ├── coords.py            # RUIAN: address code → coordinates
│   │   ├── codebooks.py         # ČSÚ 579, legal forms
│   │   ├── sbirka.py            # turnover from filed statements
│   │   ├── website.py           # IČO → domain + proof it is theirs
│   │   ├── whois_cz.py          # CZ.NIC port 43: domain holder
│   │   ├── contacts.py          # people and channels from a contact page
│   │   ├── mpsv.py              # vacancies, daily JSON + delta archive
│   │   ├── dotace_eu.py         # monthly XLSX, EU subsidies with dates
│   │   ├── nen.py               # tenders: the fresh half of a buy signal
│   │   ├── certificates.py      # ISO and the like, issued by third parties
│   │   └── ares_notifications.py# daily register change batches
│   ├── evidence/                # THE CORE. Not a source — a check on sources
│   │   ├── archive.py           # SQLite + sha256 snapshots: text, URL, date
│   │   └── verify.py            # quote in the archive? → fact / inference / discard
│   ├── signals/
│   │   ├── mode.py              # production mode: made-to-order / serial / mixed
│   │   └── now.py               # dated "why now" events
│   ├── llm/
│   │   ├── client.py            # the model over an API; nothing is trained
│   │   └── prompts/             # structured output: claim + quote + URL
│   ├── filters/
│   │   ├── brief.py             # the ICP from the interface as a filter
│   │   └── negative.py          # insolvency, liquidation, dead establishments
│   ├── scoring/
│   │   ├── select.py            # delivery gate + the week's five
│   │   ├── card.py              # the brief a salesperson reads
│   │   └── audit.py             # does one run obey every rule it claims?
│   ├── build_ui_data.py         # precomputed codebooks for the interface
│   └── run.py                   # one whole run
├── api/main.py                  # serves web/ and /api from one process
├── web/                         # static: the ICP screen, week, card, history
├── deploy/                      # restricted deploy key, Caddy, env template
└── data/                        # gitignored: registers and the evidence archive
```

Run it: `python -m uvicorn api.main:app --port 8000`, then
`http://127.0.0.1:8000/`. One process serves both the static files and
`/api`, so there is no CORS and no host baked into `app.js`. The
interface will not work after a fresh clone until
`python -m pipeline.build_ui_data` has been run — `data/` is not in git.

---

## 9. Working rules

- **Code is English throughout**: identifiers, comments, docstrings.
  Czech words only as ARES field keys and in output strings.
- **Simple and explicit, easy to debug.** Sources decide nothing — they
  fetch and hand back; scoring decides. HTTP is kept separate from
  parsing, so every `parse_*` is a pure function reproducible on saved
  JSON.
- Comments explain **why**, and record edge cases in the data. They do
  not restate the code.
- **Only stdlib and a minimum of dependencies** until a need is proven.
- **Honest accounting of what was verified.** Mark claims: checked
  myself · second-hand · not checked. Never present unverified as
  verified — that is precisely the hallucination the whole architecture
  is built against.
- **Any step that attributes something to a company must be checked with
  the question "how many companies got the same answer?"** This pattern
  has appeared five times — a historical register record read as the
  current one, link dedup, a hung `timeout`, a domain collision across 19
  companies, a DNS resolver failing under load. On a single company such
  a defect is invisible by construction.

---

## 10. Known holes

Stated plainly, because they belong in the write-up.

| What | The problem |
|---|---|
| Manual re-entry of production data | Not detectable automatically at all. Searching MPSV vacancies for "Excel": 1.84 % of records, precision near zero. |
| Know-how held in one person's head | Named as the strongest recurring trigger — and the worst-evidenced. Only indirect, through a role appearing that did not exist before. |
| Growth vs. churn | Indistinguishable from outside. High turnover looks like growth. |
| A register event is not a change of management | The only signal that arrives faster than the run repeats — and about a third of its findings are not what they look like. Measured over a year (1569 events): 28 % of ownership events have a legal entity as the owner (movement inside a holding), 29 % of director arrivals are people already present in that company's record, and a further 27 % are re-registrations. |
| The register covers 2 triggers out of 9 | A production manager promoted from inside never appears there. Median gap between event and purchase: 293 days. |
| Tenders and subsidies cut both ways | Either a hot lead with a budget, or a customer a competitor has already taken. Resolved for tenders by the status column; for subsidies, shown with a flag and left to the human. |
| NEN is not the only certified profile | E-ZAK, Tenderarena, Vortal. Sweeping only NEN catches part of the procurements. |
| Production mode: made-to-order vs. serial | Checked by hand on three companies — **0 of 3 gave a clean answer**. One says "to order" but publishes a catalogue; one says "unit and serial" on the same page; one says nothing. Website wording is marketing. |
| A company's website is published nowhere | Not in RES (25 columns), not in ARES (no contact field at all). It has to be guessed and then proved. **Of the guessed domains that turned out to be live sites, 46 % belong to a different company.** 11.2 % are never found. |
| Person vs. switchboard | 42 % have a named person, 34 % only `info@` and a front desk. |
| A group's domain is not the company's | 121 companies are proved through a parent owner; a claim from there is about the group, not that IČO. |
| PAIN does not measure pain | Measured twice; see section 5. The ceiling is in the source. |
| Manual checking found what the measurements did not | Two automated experiments produced flat numbers; two hours with the actual websites open found two defects, both spoiling the top of the output. Reading the cards by eye is a tool, not a formality. |
