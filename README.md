# icp-scout


![Python 3.14](https://img.shields.io/badge/Python%203.14-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-2088FF?style=for-the-badge&logo=githubactions&logoColor=white)

Takes a customer profile, walks the public Czech registers, and once a week hands back
a few companies **something actually happened to this week** — each with a dossier where
every claim carries its own state and its own source.

The whole tool rests on one sentence: **the model may propose a claim, but nothing
becomes a fact unless the code finds it verbatim in the stored text of the page.**
Anything else is labelled an inference, or discarded and counted.

Czech Republic only for now — the sources are Czech registers. Everything above them
(the archive, quote verification, signal windows, selection) does not depend on the
country.

**Running live at [icp-scout.fun](https://icp-scout.fun)** — the latest week, the
filters, and the history of everything that has been handed over. It is the same
instance the deploy below points at: what is in `main` is there a minute later.

Why it is built this way, in depth and with the code pointing at it:
[ARCHITECTURE.md](ARCHITECTURE.md). *Česká verze: [README.cs.md](README.cs.md).*

---

## What it is not

- **Not a CRM, and not a sender.** It sends nothing on its own. The last word is always
  a human's.
- **Not a replacement for company databases.** Those answer "which companies exist".
  This one answers "which of them moved, and what can I prove about it".
- **Not an LLM wrapper that chats about a company.** The model has no right to assert;
  it has the right to propose something the code then checks against the fetched text.
- **Not an endless list.** Five dossiers a week is the intent, not a ceiling. When
  fewer come out honestly, fewer are handed over — the window is never widened to fill
  a quota.

---

## What the output looks like

The shape of a dossier. Values tied to a specific company are replaced with
placeholders; the structure and the counters are real:

```
COMPANY Ltd.   (reg. no. ········)

  Seat              town · district · region                 ares.gov.cz/…
  Distance          85 km from the chosen point              RÚIAN
                    — note, an establishment sits 230 km away
  Headcount         100-199 employees                        ares.gov.cz/…-res
  Industry          Manufacture of other machinery           ares.gov.cz/…-res
  Turnover          123,456,000 CZK (2025)                   or.justice.cz/…
  Website           company.cz [proven]                      https://company.cz
  Certificate       ISO 9001                                 company.cz

  Director          ······ · director, since 2019-04-01      ares.gov.cz/…-vr
    ↳ channel       (none)
  Director          ······ · director, since 2023-11-15      ares.gov.cz/…-vr
    ↳ channel       ······@company.cz                        company.cz
  Contact from site ······ · director — per the website
    ↳ channel       ······@company.cz · +420 ·········       company.cz
  Company channel   sales@company.cz · +420 ·········        company.cz

  Why now           ······ (director) — left the statutory body     ares.gov.cz/…-vr

  Evidence          documentation, production, surface treatment…
                    "the verbatim sentence found in the archived page text"
                                                             https://company.cz/

  6 verified facts · 0 inferences · 2 discarded in verification · 3 not counted
```

**The last line is the whole point.** Six facts means six quotes that were found
verbatim in a page stored together with its URL and date. *Discarded* means the model
returned a quote that is not on the page — it went no further, but the dossier does not
keep quiet about it. *Not counted* are claims that passed verification and still do not
count: the second layer marked them as literally true but beside the question asked.

Three mechanics follow from that shape:

- **A conflict between sources is shown, not hidden.** When the register says a person
  left the statutory body and the website still lists them as a director, their name
  carries "per the website" — and the departure is the dated reason to call. If both
  roles looked the same on the page, they would look equally certain.
- **Distance is measured across every address a company has.** The seat is a postal
  address, an establishment is a place of business, and the register does not say which
  one is the shop floor. A company is admitted inside the radius on the nearest point —
  and the dossier is obliged to print the far one.
- **A missing value is printed as missing.** Where there is no turnover, the reason is
  printed ("the filing is a scan with no text layer"), not a blank and not an estimate.

The same thing lives in the browser. The interface is static files with no build step
and has four screens. The app opens on **the week** — five dossiers in the order the
selection ranked them. From there you reach one company's **dossier**, the **filters**
(the profile), and the **history**: everything that has ever gone out, grouped by run,
with how many times that company went out in total. A run is started with a button and
the page says how old the last one is.

History exists for the same reason as the counters on a dossier: without it you cannot
tell that the same company is being offered for the third time. And a company that has
since dropped out of the base — registers change, profiles change — stays in the
history as a registration number with no name. That is a state, not an error, and
leaving it out would be claiming it was never handed over.

---

## The core: the code checks the model

Hallucination is the main risk in this task. An invented claim about a company that
somebody pastes into an email is worse than no output at all — and that is exactly the
failure mode most "AI for sales" tools do nothing about.

Three states for one claim, and **the code decides between them, not the model**:

| state | condition | what the dossier shows |
|---|---|---|
| **fact** | the quote was found in the fetched page text | claim + quote + URL + date |
| **inference** | no quote, or the claim is derived from one | labelled as the model's inference |
| **discarded** | the model returned a quote that is not on the page | goes no further, only gets counted |

The model answers in a structured shape (`response_format: json_schema`,
`strict: true`): `{claim, quote}`. The code looks for that quote in the stored text —
plain substring search, both sides through the same normalisation. No "don't be
overconfident" in the prompt, because there is no way to check whether the model
complied. No second model as the first line of defence, because it hallucinates too.
**The prompt asks the model to behave; the architecture makes misbehaving impossible.**

Why exact matching and not fuzzy: a looser comparison would have to be tuned, and there
is nothing to tune it against. Exact matching after normalisation is the only version
that needs no calibration and cannot be defeated by paraphrase.

**Every claim is verified on its own, not the answer as a whole.** A single model
response routinely contains both — checkable data and uncheckable inference.

**What this cannot do, said out loud.** A match proves the sentence is on the page — not
that it answers the question that was asked. One run put "single-shift operation" on a
dossier as evidence of production scale, because that sentence really was there. So two
guards sit beside the verifier, and neither of them touches the fact / inference /
discard decision: `verify.states_absence()` refuses to count a quoteless "there is no
mention of X" as evidence of X, and the relevance judge (`llm/prompts/relevance.py`) may
mark a verified fact as beside the point. **It may subtract, never add** — a fact is
still only what survived the search in the text.

The load-bearing line of the schema is `claim.snapshot_id NOT NULL REFERENCES
snapshot(id)`. A claim about a company that does not point at a stored page snapshot
physically cannot be inserted — so "nothing without a source" is not a discipline
anyone has to remember, it is a foreign key.

### An archive, not a cache

A cache exists so you do not fetch twice. This exists because a week after a run
somebody will ask "where did that come from" and the page will have changed. Text is
stored content-addressed by SHA-256, which buys three things at once: the same page
across runs is stored once; "did this change since last week" is a comparison of two
hashes, so change detection needs no separate mechanism; and a quote can never be
pinned to a document that was rewritten afterwards — different text is a different hash.

Orders of magnitude from real operation: half a million snapshots, over 200 thousand
distinct documents, roughly a thousand stored claims, and just under two hundred
discards, which are remembered too.

---

## What a run looks like

The order is not accidental: **cheap and structured first, expensive and dirty last.**

```
1 icp       the profile from the interface; recorded on the run
2 refresh   only what actually moved — the register's change batches say
            which companies are affected, and only those are re-fetched
3 gate      the profile (industry, size, region), the negative filters, and
            finally NOW: with no dated event the company stops here
4 enrich    site, contacts, certificates — ONLY for those past the gate
5 agents    the LLM pass, again only over the survivors
6 select    ranking by what was actually proved
7 cards     rendering, and recording what was handed over
```

The expensive step is `enrich` — it crawls company websites. Over the whole base that is
hours; over ten companies it is minutes. That is why the gate comes **first**. It is not
an optimisation bolted on afterwards, it is the reason a weekly run fits into minutes
and the reason the model calls for one run cost pennies. (The cost is not estimated:
every call is appended to `data/llm_usage.jsonl` with its tokens and its price.)

**Change detection is pull-based, not push-based.** We do not walk the companies asking
whether anything happened — a batch of changes arrives from the register and is matched
against the base by registration number. There are a few thousand changes nationally in
a week and a few dozen of ours. The cost therefore does not grow with the size of the
base, the event arrives with a date from a state register, and companies the base does
not contain yet get found too.

---

## "Why now": every source keeps its own window

This is the finding that explained why, for months, only one signal appeared to work.
It was the only signal that worked — **three of the four sources publish more slowly
than the run repeats.** Asking them "what happened in the last seven days" is asking for
something they physically do not contain yet, and getting back a zero that says nothing.

| source | lag (measured) | window |
|---|---|---|
| commercial register | 2 days | the run's own, 7 days |
| job postings | 10 days | `VACANCY_WINDOW` = 24 days |
| subsidies | 35 days | `SUBSIDY_WINDOW` = 120 days |
| public tenders | same day | none — the bid deadline decides |

The width is derived, not chosen: `lag + run cadence`, taken twice so a signal cannot
fall between two runs. The lag is re-measured on every run and the tool warns as soon as
a constant stops covering the cadence. What is measured is the 1st percentile of record
age, not the freshest row: that one is one in tens of thousands and lies by a factor of
three.

How much such a signal can carry is calculable in advance. A backtest across 104 weeks:
a median of fifteen companies with an event per week, eight of which pass the delivery
condition. At least five came out in 89 of 104 weeks — the five hold, but with no
reserve, and that is an argument for more sources rather than a wider window.

---

## Who gets handed over and who does not

The hard condition: a company goes out only with a **proven domain** (`proven`, not
`probable`) and **at least one channel**. Geography is strict: the chosen radius with no
exceptions, measured across every address the company has.

The price is quantified and paid deliberately: a company without a proven domain is
never handed over. The reason is measured — of the guessed domains that turn out to be
live sites, **46 % belong to a different company**. Without that rule the dossier
carries another company's text and a stranger's contact; it happened once, and the ban
has been absolute since.

A company that fails the condition does not even get a line — its dossier would carry a
name, a registration number and one sentence from the register, which is precisely the
list the output is not supposed to be. The count of the withheld does stay in the
week's header.

Ranking is not a sum of scores, it is the **class of the reason**:

```
A subsidy with no tender started   (the money is there, the buying has not begun)
B change in management or ownership
C an open public tender
D a posting for a management/planning role
E some other event
```

Within that the order is: profile group → negative finding → class of reason →
corroboration by a second signal → distance → freshness within the class → number of
facts as the tie-break.

The best row of **each** class takes a slot before any class takes a second one. This is
not diversity for its own sake: there is nothing to calibrate weights against, and a
week of five identical reasons is one experiment run five times. The price is stated out
loud — a company in a reserved slot ranks below the one it displaced, and has to print
that.

---

## Sources

| source | what it gives | note |
|---|---|---|
| **RES** (bulk CSV) | the first cut: size, industry, district | the whole register in one file, filtered locally — zero HTTP requests |
| **ARES**, 4 GET endpoints | identity, seat, insolvency, size, statutory bodies and owners **with dates**, trades, establishments | a structured record with a date from a state register — the cleanest signal available |
| **ARES notifications** | daily change batches — who moved | a weekly update in minutes instead of hours; batch history ~30 days |
| **RÚIAN** | coordinates of an address point | distance to the company's nearest address |
| **company website** | operational signals, contacts | the address of a website is in no register — it is guessed and then **proven** (reg. no., VAT id, WHOIS, address) |
| **WHOIS CZ.NIC** (port 43) | domain owner | a tool of proof, not a source of contacts; the rate limit measured at 1 query/s |
| **MPSV** | job postings, daily JSON + an archive of increments | the growth signal |
| **subsidies** | monthly XLSX, projects with a signature date | careful: a subsidy is granted for a project, not to a company — most projects are about something else |
| **NEN** | public tenders | the status column separates "buying now" from "already bought" |
| **Collection of Deeds** | turnover from the annual accounts | **takes no part in selection**, it only fills the dossier — a readable statement exists for 16.5 % of companies |

A subsidy and a tender are two moments of one purchase, and both are needed:

| subsidy | tender | meaning |
|---|---|---|
| yes | **no** | the money is there, buying has not begun — the most interesting case |
| yes | open | they are buying, the spec is already written |
| yes | closed | they bought, too late |
| no | yes | they are buying with their own money |

The sign of the signal is ambiguous in both cases — an opportunity with a budget, or a
customer already taken by a competitor. It is not encoded; it is shown with a note and a
human decides.

### Negative filters

The category "formally a match, but will never buy" appears in no profile, and yet it
saves the most work. A company in insolvency satisfies every line of the brief and there
is nothing to sell it. It is thrown out **before** the expensive steps, while that is
still cheap.

### What was not used, and why

- **Keywords in job postings as evidence of an internal process** — measured on one of
  them: 1.84 % of records and precision near zero. A posting is written for applicants,
  not for us.
- **The public contracts register as a source of private-company purchases** — only
  public institutions are required to publish; a private company appears there at most
  as a counterparty.
- **Labour inspection** — no per-company public data exists, only aggregate annual
  reports.
- **Scraping behind a login** — inadmissible technically and under the services' terms.
- **Paid databases** — outside the intent of the project.
- **Anything requiring manual work per company** — it may be an excellent source, but it
  belongs to manual follow-up on companies already selected, not to the selection.

---

## The score that did not hold up

This section exists because a negative result is a result too, and elsewhere nobody
mentions them.

There used to be a weighted score meant to rank companies. **It no longer decides
anything**, and that is not a simplification, it is measured:

- the sum of eight terms behaved like one — a single component carried 62.6 % of the
  points and correlated **+0.81** with the number of pages fetched. The ranking was
  therefore largely deciding by whose website was biggest;
- the same five came out of 35 % of 500 random weight vectors, so "tuned" weights were
  determining nothing;
- against a hand-labelled control group nothing separated anything — neither the
  hand-built components (p = 0.72–1.00) nor a 1536-dimension embedding of the same sites
  (AUC 0.39).

The ceiling is in the source: **the public text of a website simply does not say it.**
So the score became what it can honestly be — the evidence a human reads on the dossier.

One thing did measure, though. **Corroboration by a second signal** — two different
events at once — was the only feature with real lift: **1.85**, i.e. 24 % against a 13 %
base rate. That is why it is a step in the ordering, and why it is counted by kind of
event rather than by family: that is how it was measured, and counted more coarsely that
level never fires at all.

---

## Where the ceiling is

This belongs in the README rather than in an issue tracker — these are properties of the
sources, not bugs to be fixed.

| what | the problem |
|---|---|
| A register event ≠ a change of management | the only signal faster than the run — and about a third of the finds are not what they look like. Measured on a year's sample: in 28 % of ownership changes the owner is a legal entity, i.e. a move inside a holding; 29 % of arrivals into a statutory body are people already present in that company's own record. Another 27 % were re-registrations, which `drop_reentries()` filters out. |
| The register only sees the top | a promotion inside a company never reaches the register. The most verifiable signal is also the least predictive: the median between a register event and a purchase comes out around 290 days. |
| Tenders are not in one place | NEN is only one of the certified contracting-authority profiles; the others are not crawled yet, so part of the buying is missed. |
| A group's domain ≠ a company's domain | some domains are proven through a parent company, or shared because of a generic name. A claim from there is about the group, not about that specific registration number. |
| A person vs. a general channel | a specific human is findable for roughly two fifths of companies; for a third there is only a front desk and a switchboard. A personal address **cannot be derived** from a name, only matched against a printed one — and when the register holds two people of the same name, nothing is matched and the dossier says why. |
| Contact accuracy across the whole base | verified only on the sample that was actually handed over. The weakest tier (a name and a number that merely sit next to each other on a page) is not reliable, and is labelled as such. |
| Calibration | there is nothing to tune against — a few dozen hand-labelled companies, and they showed no difference. The ranking is therefore explainable line by line, not optimised. |
| Feedback | none. The tool knows what it handed over and when — the history even shows it — but not how it went. So the ranking has nothing to learn from. |

One regularity showed up five times in this project (a historical record passed off as
current, missing deduplication, a `timeout` that does not bound the whole transfer,
glued-together domains, a DNS resolver refusing under load): **every step that attributes
something to a company has to be checked with the question "how many companies got the
same answer".** On a single company a defect like that is invisible by construction.

That goes double for exceptions. A timeout, a `socket.gaierror` or a 403 do not mean
"the company does not have one" — they mean the source did not answer. An unreachable
source therefore returns `None`, not an empty list; one such conflation cost the evidence
of two thousand companies.

And one observation about method: two automated experiments returned smooth numbers,
while two hours with the websites open found two defects, both of them spoiling the top
of the output. **Reading the dossiers with your own eyes is a tool, not a formality.**

---

## Quick start

Python 3.14, six pinned dependencies, the rest is the standard library.

```bash
pip install -r requirements.txt
```

The model key goes into `.env` (`OPENAI_API_KEY=sk-…`) or into the environment. The
`data/` directory is entirely in `.gitignore` — it is derived and re-downloadable — so a
fresh clone has no data at all. Start by asking what is missing:

```bash
python -m pipeline.run --check
```

It prints every prerequisite: what it is, which stage needs it, and whether it can
fetch it itself. Whatever it can, it builds:

```bash
python -m pipeline.run --bootstrap
```

The first build is by far the longest part of the whole operation — the register is
downloaded and the websites are crawled. The one thing the tool cannot obtain for you is
the API key: it names it and stops. A bootstrap that quietly half-works is worse than
one that says which step is yours — every later file is derived from the one before it,
so continuing without the first means producing a chain of empty files that look real.
For the same reason large files are not checked merely for existence: a download that
dies halfway passes every existence test and then quietly yields a truncated list.

That prerequisite list has a history of its own, because it was wrong in both directions
and quietly each time. The first clean build finished, reported success — and left a
machine where the interface could not be built at all, because the industry codebook was
missing from the list. A missing tenders file, in turn, broke nothing: it just meant a
class C reason could never appear in any week, and nothing said why. **Silence is a worse
failure than a crash**, so both are in the list now.

Then it is just:

```bash
python -m pipeline.run                               # the weekly run
python -m pipeline.run --stage gate --stage select   # selected stages only
python -m pipeline.run --window 14 --top 5
```

The interface — one process serves both the static files and `/api`:

```bash
python -m uvicorn api.main:app --port 8000
```

The data behind the screens is built by `--bootstrap` itself; after a register update
recompute it with `python -m pipeline.build_ui_data`.

Every source has its own CLI and runs standalone, which is also the fastest way to find
your way around the code:

```bash
python -m pipeline.sources.ares 29092540
python -m pipeline.evidence.archive --stats
python -m pipeline.scoring.card <reg-no> --no-fetch
```

### The profile is entered in the interface, not in the code

The profile is input, not a constant — which is why this repository can be public and
still usable on somebody else's data. It is saved to `web/icp.json`, which is **the same
file** `pipeline/run.py` reads; one place for both ends. The repository ships one example
profile so the screens do not start empty; you overwrite it in two steps and the next run
follows yours. An empty field does not mean "everything" — it means "nobody has decided
yet", and the default is used.

---

## Deployment

Push to `main` and a minute later the demo is running that commit.

```
push → GitHub Actions → git archive | ssh → receive.sh → rsync → build → restart → smoke test
```

The whole transfer is one pipe: `git archive` on the runner straight into
`deploy/receive.sh` over ssh. No registry, no checkout on the server — **the server
therefore needs no credentials for this repository**, and that is the entire reason it is
not a `git pull` on the far end.

Decisions worth explaining:

- **The deploy never starts a run.** A weekly run takes a long time and spends money on
  the model, so it stays a human decision rather than a side effect of pushing code.
- **And above all it must not kill one.** A run is a subprocess inside the container, so
  a restart would take it down. The script asks first whether one is going; if so, it
  refuses to restart and exits non-zero. Nothing is lost — the files are synced and the
  image is built, so re-running the deploy once the run finishes picks it up. A deploy
  that did not take effect must not show green.
- **The key can do only this.** In `authorized_keys` it carries
  `command="…/receive.sh"`, so it cannot open a shell or forward a port. On a machine
  that also serves somebody else's site, a plain deploy key in a secret is a root shell
  for anyone who can read a workflow log.
- **What is never overwritten:** `.env`, `data/` and `web/icp.json`. The first two are
  not in the repository at all; the third is — which is why it is excluded by name
  rather than by hoping. A saved profile is user input, not a build artefact.
- **Rsync with `--delete`,** so a file removed in git disappears from the server too.
  That has bitten twice: the script rsyncs over itself while running (safe only because
  rsync writes a temporary file and renames it), and the first time it deleted itself
  outright, because it existed on the server and not in git. Whatever this deploy needs
  must be in git.
- **The smoke test goes through Caddy**, not through the app's own port — that is the
  path a visitor takes. One dossier is checked too, because that endpoint once failed
  silently: it answered 404 for every company while every page kept returning 200.
  Finally the same question is asked from outside over the public address, because the
  tunnel or DNS can be down while every container is healthy.
- **`.gitattributes` pins LF** on everything the server executes. It is written on
  Windows, deployed to Linux, and the transfer is `git archive` — so whatever git stores
  is what bash executes on the far end, and a script with CRLF fails on its first line
  with a message that names no cause.

The machine's address and the user it logs in as are not in the repository — they are
secrets. The domain is not secret, the demo runs on it; but the deployment has it
hard-coded nowhere. It is a public hostname on the tunnel, so adding a second one or
renaming this one is an edit in a dashboard, not a commit.

The rest — the tunnel, Caddy, the container's memory ceiling — is described in
[DEPLOY.md](DEPLOY.md). The whole stack fits on a small server next to another running
application.

---

## Layout

```
pipeline/
  sources/        one source = one module behind a shared interface
    res_bulk.py       the first cut, from a local CSV
    ares.py           4 GET endpoints; fetch() apart, parse_*() pure functions
    ares_notifications.py  daily change batches: who moved
    coords.py         address code → coordinates
    website.py        reg. no. + name → the company's domain, with proof
    whois_cz.py       domain registry, owner
    contacts.py       channels to people we already know from the register
    mpsv.py           postings, daily JSON + an archive of increments
    dotace_eu.py      monthly XLSX (read with zipfile + re, not openpyxl)
    nen.py            public tenders
    sbirka.py         turnover from the annual accounts (dossier only)
    certificates.py   ISO and the like — issued by a third party, hence checkable
  evidence/       THE CORE — not a source, the check on all the others
    archive.py        SQLite + snapshots addressed by SHA-256
    verify.py         quote in the archive? → fact / inference / discard
  signals/
    now.py            dated "why now" events, each source with its own window
    mode.py           properties inferred from text
  llm/
    client.py         one door for every prompt; structured output, cache, cost
    prompts/          the individual jobs given to the model
  filters/
    brief.py          the profile from the interface, as a candidate filter
    negative.py       insolvency, liquidation
  scoring/
    select.py         the delivery condition + ranking + a slot quota per class
    card.py           the dossier a human reads
  run.py            one whole run, plus the preflight
api/main.py         the interface and the calls behind it
web/                static, no build step, four screens
  index.html          the week — what the app opens on
  brief/              the profile: industry, size, region
  history/            what has gone out, grouped by run
  card/               one company's dossier
  run-control.js      the run button and the age of the last one
ARCHITECTURE.md     why it is built this way; code comments point here
.github/workflows/  deploy: push to main → a running demo
deploy/             the other half of deployment: receive.sh, Caddyfile
data/               entirely in .gitignore (registers, archive, snapshots)
```

---

## Contributing

The most useful contribution is **a new source**. There will always be more modules than
there are today, the list is open, and adding one must not touch anything outside its
own file.

1. **A source decides nothing.** It only "goes and fetches". Who gets handed over is
   `scoring/`'s job.
2. **HTTP apart from parsing.** `fetch()` goes to the network, `parse_*()` are pure
   functions over a payload — debuggable against stored JSON with no network.
3. **Whatever is quoted must be in the archive.** The snapshot is stored before a claim
   is made from it. Without a `snapshot_id` a claim cannot be inserted.
4. **Its own CLI**: `python -m pipeline.sources.<name> <argument>` must print something
   meaningful on its own.
5. **An empty answer is not a fact.** An unreachable source returns `None`, not an empty
   list.
6. **Code in English** (identifiers, comments, docstrings). Comments explain **why** and
   the edge cases in the data, not what the line below them does.
7. **Standard library only**, until a dependency earns its place. XLSX is read here with
   `zipfile` and `re`, and that is fine.

**Measurements** are just as valuable. The `*_probe.py` scripts in the root are exactly
that: one-off questions of the kind "how much does this source actually contain". Their
output is in the repository too, not just the code — so the numbers in the tables above
can be checked without re-running a probe against data that has moved since. A number
that refutes one of them is a welcome pull request.

---

## Legal framing

**The company is profiled, not the person** — and that is not a phrasing, it is a split
in the data. Facts, signals, assessments and history hang off the **registration
number**. A person's name lives only in the contact block: name, function, link to the
register. Nothing is scored, noted or historised about a human being, and no sources are
joined on one.

The reason: legitimate interest as a legal basis does not cover advanced profiling that
joins data about a person across sources. A profile of a legal entity does not create
that problem.

So sole traders are excluded, addresses in a personal form are flagged, and the tool
**sends nothing on its own** — the data controller remains whoever uses it.

The sources are public registers and public websites, fetched at a rate the servers can
take. Nothing behind a login wall.

---

## Status and what is next

The whole weekly run works from profile to dossier, runs in production, and deploys on a
push to `main`. It is not a finished product: there is no user feedback, the ranking is
uncalibrated, and contacts are unverified across the whole base.

The nearest directions, ordered by how much they unlock:

- **more contracting-authority profiles** — part of the buying is currently missed
- **vendors' reference lists** as a negative filter: a fresh case means "already
  bought", an old one is a reason to call
- **state between runs**: the history shows what has gone out and how often. The other
  half is missing — what to do with a company that passed everything but happened to
  have no dated reason
- **contact verification across the whole base**, not only on what went out
- **another country**: a new set of modules in `sources/`, the rest should stay

---

## Licence

[MIT](LICENSE). The code may be used, modified and sold; the only condition is keeping
the attribution with it. No warranty — and for a tool that collects claims off other
people's websites that is worth repeating out loud: **what is verified is that the
sentence was on the page, not that it is true.**

The licence covers the code. The data it works with has its own regime: public registers
have their terms of use, so do company websites, and personal data is governed by the
section above rather than by this one.
