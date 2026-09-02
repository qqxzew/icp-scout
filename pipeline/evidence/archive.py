"""The evidence store: every page we read, kept with its URL and date.

This is not a cache. A cache exists to avoid fetching twice; this exists
because a week after a run the salesperson will ask "where did that come
from" and the page will have changed. The brief calls traceability a
function rather than documentation - so it is a table, not a habit.

Two stores, on purpose:

    snapshots/  the text itself, one file per distinct content
    archive.db  the index, plus everything derived from it

Content-addressed by SHA-256, which buys three things at once:

* the same page across weekly runs is stored once, not fifty times
* "did this change since last week" is a string comparison of hashes,
  so change detection needs no separate mechanism
* a quote can never be attributed to a document that was edited
  afterwards - a different text is a different hash

Text is stored **normalised**, through the one function verify.py will
use to look for quotes. Storing raw and normalising later is the same
bug sbirka.py already hit with non-breaking spaces: the quote is taken
from one shape of the text and searched for in another, and it silently
never matches.

The schema's load-bearing line is `claim.snapshot_id`. A claim about a
company that does not point at a snapshot cannot be inserted, so
"assertion without a source" is not a discipline anyone has to remember
- it is a foreign key.

Run:
    python -m pipeline.evidence.archive --init
    python -m pipeline.evidence.archive --stats
    python -m pipeline.evidence.archive --show 42
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data/archive.db")
SNAPSHOT_DIR = Path("data/snapshots")

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    -- The ICP this run used, serialised. Requirement 1 of the brief is
    -- "accept the salesperson's brief"; without this a run cannot be
    -- reproduced or explained a week later.
    icp_json    TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS snapshot (
    id          INTEGER PRIMARY KEY,
    ico         TEXT NOT NULL,
    source      TEXT NOT NULL,          -- website | ares | mpsv | sbirka | whois
    -- Which part of a site this page is: home | contact | career |
    -- production | about | references. Set while harvesting, because
    -- that is the only moment the link that led here is still known -
    -- a URL alone often cannot be classified after the fact.
    kind        TEXT,
    url         TEXT,
    fetched_at  TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    length      INTEGER NOT NULL,
    run_id      INTEGER REFERENCES run(id)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_ico    ON snapshot(ico);
CREATE INDEX IF NOT EXISTS idx_snapshot_sha    ON snapshot(sha256);
CREATE INDEX IF NOT EXISTS idx_snapshot_lookup ON snapshot(ico, source, url);

CREATE TABLE IF NOT EXISTS claim (
    id          INTEGER PRIMARY KEY,
    ico         TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- production_mode | contact | size | ...
    value       TEXT,
    -- fact: the quote was found in the snapshot
    -- inference: derived, quote is context rather than proof
    state       TEXT NOT NULL CHECK (state IN ('fact', 'inference')),
    quote       TEXT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
    run_id      INTEGER REFERENCES run(id),
    created_at  TEXT NOT NULL,
    -- The second layer's verdict: does this proven sentence actually
    -- prove what it was filed under (see llm/prompts/relevance.py).
    -- Deliberately a separate column rather than a third `state`: the
    -- state says how the claim is held (quoted or derived) and is
    -- decided by string containment alone, which is the one property of
    -- this schema worth protecting. NULL means nobody asked.
    relevance      TEXT CHECK (relevance IN ('supports', 'unrelated')),
    relevance_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_claim_ico ON claim(ico, kind);

CREATE TABLE IF NOT EXISTS discard (
    id          INTEGER PRIMARY KEY,
    ico         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    value       TEXT,
    -- What the model claimed as a quote. NOT NULL, unlike claim.quote -
    -- a discard exists *because* there was a quote and it did not match;
    -- an inference with no quote is never a discard, it is a claim.
    quote       TEXT NOT NULL,
    -- The snapshot the model was actually reading when it produced this.
    -- Kept even though the quote failed to match it, so a hallucination
    -- rate can be reported per source, not just as one global number.
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
    run_id      INTEGER REFERENCES run(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discard_ico ON discard(ico, kind);

CREATE TABLE IF NOT EXISTS delivered (
    ico          TEXT NOT NULL,
    run_id       INTEGER NOT NULL REFERENCES run(id),
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (ico, run_id)
);
"""

# One canonical normalisation, used when storing and when searching.
# Anything that changes this invalidates existing quotes, so it changes
# with the same care as a database migration.
_WHITESPACE = re.compile(r"\s+")

# The two verdicts the relevance judge may write on a claim. Defined
# here, next to the column that stores them, so verify.py and the agent
# that produces them read the same two strings instead of each spelling
# them out - a typo in one of the two would silently mean "nobody
# judged this", which is the safe-looking wrong answer.
SUPPORTS = "supports"
UNRELATED = "unrelated"


def normalize(text):
    """Collapse every run of whitespace, including the invisible ones.

    Non-breaking spaces inside a phrase are the classic failure: a label
    reads "Tržby z prodeje" on screen and is "Tržby\\xa0z\\xa0prodeje"
    in the bytes, so a search for the visible form finds nothing at all.
    """
    return _WHITESPACE.sub(" ", (text or "").replace("\xa0", " ")).strip()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Archive:
    """Snapshots on disk, index in SQLite."""

    def __init__(self, db_path=DB_PATH, snapshot_dir=SNAPSHOT_DIR):
        self.db_path = Path(db_path)
        self.snapshot_dir = Path(snapshot_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # check_same_thread=False only lifts Python's own guard; it does
        # not make one sqlite3.Connection safe for concurrent use. Found
        # the hard way: website.py and ares.py both call store() from a
        # ThreadPoolExecutor, and without serializing here that produced
        # "cannot commit - no transaction is active" and "bad parameter
        # or other API misuse" on 212 of 3299 companies in one run - two
        # threads' execute()/commit() calls interleaved on the same
        # connection. The lock below serializes every method that
        # touches self.db; WAL still lets a plain read via text_of()
        # avoid blocking on a concurrent write of a different snapshot.
        self.db.execute("PRAGMA journal_mode=WAL")
        # Off by default in SQLite, which makes every REFERENCES clause
        # decorative. Without this the promise above - that a claim
        # cannot exist without a snapshot - is merely a comment, and a
        # claim pointing at snapshot 99999 inserts happily.
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        # `kind` arrived after the first 14 731 snapshots were written;
        # CREATE TABLE IF NOT EXISTS will not add it to a table already
        # there, so it is added here and older rows keep NULL - which is
        # honest, we genuinely do not know what those pages were.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(snapshot)")}
        if "kind" not in columns:
            self.db.execute("ALTER TABLE snapshot ADD COLUMN kind TEXT")
        # Same story one table over: the relevance verdict arrived after
        # 700-odd claims were already stored. Those keep NULL, which is
        # the honest value - nobody judged them - and every reader treats
        # NULL as "still counts", so an old claim is not quietly demoted
        # by a column that did not exist when it was written.
        claim_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(claim)")}
        if "relevance" not in claim_columns:
            self.db.execute("ALTER TABLE claim ADD COLUMN relevance TEXT")
            self.db.execute("ALTER TABLE claim ADD COLUMN relevance_note TEXT")
        self.db.commit()
        # RLock, not Lock: store() calls latest() internally while
        # already holding the lock, and a plain Lock would deadlock a
        # thread against itself on that second acquire.
        self._lock = threading.RLock()

    # -- runs --------------------------------------------------------

    def start_run(self, icp=None, note=None):
        with self._lock:
            cursor = self.db.execute(
                "INSERT INTO run (started_at, icp_json, note) VALUES (?, ?, ?)",
                (now(), json.dumps(icp, ensure_ascii=False) if icp else None, note),
            )
            self.db.commit()
            return cursor.lastrowid

    def finish_run(self, run_id):
        with self._lock:
            self.db.execute("UPDATE run SET finished_at = ? WHERE id = ?", (now(), run_id))
            self.db.commit()

    # -- snapshots ---------------------------------------------------

    def path_for(self, sha):
        """Two-level fan-out: one directory per 256 files, not per 100k."""
        return self.snapshot_dir / sha[:2] / f"{sha}.txt"

    def store(self, ico, source, text, url=None, run_id=None, kind=None):
        """Keep one document and return (snapshot_id, changed).

        `changed` answers "is this different from the last time we looked
        at this exact url for this company" - which is the whole NOW
        signal for websites, obtained here for free.
        """
        text = normalize(text)
        sha = digest(text)

        with self._lock:
            previous = self.latest(ico, source, url)
            changed = previous is None or previous["sha256"] != sha

            path = self.path_for(sha)
            if not path.exists():      # identical content is written once
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            cursor = self.db.execute(
                "INSERT INTO snapshot (ico, source, kind, url, fetched_at, sha256, length, run_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(ico).zfill(8), source, kind, url, now(), sha, len(text), run_id),
            )
            self.db.commit()
            return cursor.lastrowid, changed

    def latest(self, ico, source=None, url=None):
        """The most recent snapshot row matching the filters, or None."""
        where = ["ico = ?"]
        args = [str(ico).zfill(8)]
        if source:
            where.append("source = ?")
            args.append(source)
        if url:
            where.append("url = ?")
            args.append(url)
        with self._lock:
            return self.db.execute(
                f"SELECT * FROM snapshot WHERE {' AND '.join(where)}"
                " ORDER BY fetched_at DESC, id DESC LIMIT 1", args
            ).fetchone()

    def text_of(self, snapshot_id):
        """The stored text of one snapshot, or None if the row is gone."""
        with self._lock:
            row = self.db.execute(
                "SELECT sha256 FROM snapshot WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if not row:
            return None
        path = self.path_for(row["sha256"])
        return path.read_text(encoding="utf-8") if path.exists() else None

    def documents(self, ico, source=None):
        """Latest snapshot per url for one company - what to read from."""
        with self._lock:
            rows = self.db.execute(
                "SELECT s.* FROM snapshot s JOIN ("
                "  SELECT url, MAX(id) AS id FROM snapshot"
                "  WHERE ico = ? AND (? IS NULL OR source = ?) GROUP BY url"
                ") last ON s.id = last.id",
                (str(ico).zfill(8), source, source),
            ).fetchall()
            return [(row, self.text_of(row["id"])) for row in rows]

    # -- claims ------------------------------------------------------

    def add_claim(self, ico, kind, value, state, snapshot_id, quote=None, run_id=None):
        """Record one assertion about a company.

        There is no way to call this without a snapshot: the argument is
        required and the column is a foreign key. That is deliberate -
        an unsourced claim should be impossible, not merely discouraged.

        Idempotent by content, same as snapshot storage: a claim with the
        same (ico, kind, value, state, quote, snapshot_id) is not
        inserted twice - the existing row's run_id is bumped to the
        current run instead. Without this, a cache hit in llm/client.py
        (same input, no new API call) still reaches this function and
        would otherwise duplicate the claim every time a run is resumed
        or repeated over already-processed companies - found live while
        building pain.py, when piping one command's output through two
        different filters silently ran the whole agent twice.
        """
        if state not in ("fact", "inference"):
            raise ValueError(f"state must be fact or inference, got {state!r}")
        ico = str(ico).zfill(8)
        with self._lock:
            # snapshot_id is deliberately NOT part of what makes a claim
            # the same claim. The same fact proven by a fresher copy of
            # the same page is one fact, not two - and re-fetching pages
            # is what this pipeline does every week, so including the
            # snapshot meant a company accumulated a duplicate of every
            # registry event on every run. Seen on ŠROUBY Krupka: the
            # same departure recorded twice, identical value and quote,
            # differing only in which snapshot backed it.
            existing = self.db.execute(
                "SELECT id, snapshot_id FROM claim WHERE ico = ? AND kind = ?"
                " AND value IS ? AND state = ? AND quote IS ?",
                (ico, kind, value, state, quote),
            ).fetchone()
            if existing is not None:
                # Point at the newer snapshot. The caller verified this
                # quote against it a moment ago, so it is at least as
                # good a proof and is the copy that still matches what
                # the page says today.
                self.db.execute(
                    "UPDATE claim SET snapshot_id = ?, run_id = COALESCE(?, run_id)"
                    " WHERE id = ?",
                    (snapshot_id, run_id, existing["id"]),
                )
                self.db.commit()
                return existing["id"]
            cursor = self.db.execute(
                "INSERT INTO claim (ico, kind, value, state, quote, snapshot_id, run_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ico, kind, value, state, quote, snapshot_id, run_id, now()),
            )
            self.db.commit()
            return cursor.lastrowid

    def claims(self, ico, kind=None):
        # s.source and s.kind travel with the row because not every
        # snapshot has a URL: vacancy text is assembled from MPSV's daily
        # JSON and archived as prose, so url is NULL and a card built on
        # url alone showed a verified quote with a blank source next to
        # it - the one thing a card must never do.
        with self._lock:
            return self.db.execute(
                "SELECT c.*, s.url, s.fetched_at, s.source, s.kind AS page_kind FROM claim c"
                " JOIN snapshot s ON s.id = c.snapshot_id"
                " WHERE c.ico = ? AND (? IS NULL OR c.kind = ?)"
                " ORDER BY c.created_at DESC",
                (str(ico).zfill(8), kind, kind),
            ).fetchall()

    def set_relevance(self, claim_id, verdict, note=None):
        """Record whether a proven claim actually proves what it was filed under.

        Written by llm/prompts/relevance.py, read by select.py and
        card.py. The verdict is checked against the two allowed values
        here rather than trusted from the caller, for the same reason
        add_claim() checks `state`: a third spelling would read as NULL
        to every consumer, which means "unjudged" - a wrong answer that
        looks exactly like a correct one.

        Survives a re-run by construction: add_claim() treats a claim
        with the same value and quote as the same claim and updates it
        rather than inserting a second, so the verdict stays attached to
        the statement it was made about.
        """
        if verdict not in (SUPPORTS, UNRELATED):
            raise ValueError(f"relevance must be {SUPPORTS} or {UNRELATED}, got {verdict!r}")
        with self._lock:
            self.db.execute(
                "UPDATE claim SET relevance = ?, relevance_note = ? WHERE id = ?",
                (verdict, note, claim_id),
            )
            self.db.commit()

    def add_discard(self, ico, kind, value, quote, snapshot_id, run_id=None):
        """Record one hallucination: a quote the model claimed but the
        archived page does not contain.

        This is not an error path to swallow. It is the one piece of
        evidence that the verifier is actually doing its job - "here is
        what the model got wrong, and here is how we caught it" is a
        stronger answer at defence than a silent drop, and it is the
        direct output of hypothesis D in the brief.

        Idempotent by content, for the same reason as add_claim() -
        a repeated run over a cache-hit response bumps run_id instead of
        inserting a second identical discard.
        """
        ico = str(ico).zfill(8)
        with self._lock:
            existing = self.db.execute(
                "SELECT id FROM discard WHERE ico = ? AND kind = ? AND value IS ?"
                " AND quote = ? AND snapshot_id = ?",
                (ico, kind, value, quote, snapshot_id),
            ).fetchone()
            if existing is not None:
                if run_id is not None:
                    self.db.execute("UPDATE discard SET run_id = ? WHERE id = ?",
                                    (run_id, existing["id"]))
                    self.db.commit()
                return existing["id"]
            cursor = self.db.execute(
                "INSERT INTO discard (ico, kind, value, quote, snapshot_id, run_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ico, kind, value, quote, snapshot_id, run_id, now()),
            )
            self.db.commit()
            return cursor.lastrowid

    # -- delivered: what the salesperson has already been handed --------

    def mark_delivered(self, ico, run_id):
        """Record that this company went out in this run.

        The table has existed since the first schema and stayed empty -
        nothing wrote to it, so requirement 8 of the brief ("see what is
        in what state, including what was already handed over") had a
        column and no content.

        INSERT OR IGNORE rather than a duplicate check, because the
        primary key is already (ico, run_id): handing the same company
        over twice inside one run is the same fact, not a second one.

        Note what this does NOT mean. Per the log (22.1) a company leaves
        the pool when the salesperson actually writes to it, not when it
        appears on a card - so this row says "was shown", and the
        except-list that governs re-offering is a separate, later thing.
        """
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO delivered (ico, run_id, delivered_at)"
                " VALUES (?, ?, ?)",
                (str(ico).zfill(8), run_id, now()),
            )
            self.db.commit()

    def delivered(self, ico=None, run_id=None):
        """What was handed over - filtered by company, by run, or neither."""
        if ico is not None:
            ico = str(ico).zfill(8)
        with self._lock:
            return self.db.execute(
                "SELECT d.*, r.started_at, r.note FROM delivered d"
                " JOIN run r ON r.id = d.run_id"
                " WHERE (? IS NULL OR d.ico = ?) AND (? IS NULL OR d.run_id = ?)"
                " ORDER BY d.delivered_at DESC",
                (ico, ico, run_id, run_id),
            ).fetchall()

    def discards(self, ico=None, kind=None):
        # Zero-padded exactly like claims() and every write path. Without
        # this, discards("207675") returned nothing while the row sat
        # there under "00207675" - a silent empty result, which on a card
        # reads as "the model got everything right" rather than "you
        # asked the wrong question".
        if ico is not None:
            ico = str(ico).zfill(8)
        with self._lock:
            return self.db.execute(
                "SELECT * FROM discard WHERE (? IS NULL OR ico = ?) AND (? IS NULL OR kind = ?)"
                " ORDER BY created_at DESC",
                (ico, ico, kind, kind),
            ).fetchall()

    # -- reporting ---------------------------------------------------

    def stats(self):
        with self._lock:
            one = lambda q: self.db.execute(q).fetchone()[0]
            return {
                "snapshots": one("SELECT COUNT(*) FROM snapshot"),
                "distinct_documents": one("SELECT COUNT(DISTINCT sha256) FROM snapshot"),
                "companies": one("SELECT COUNT(DISTINCT ico) FROM snapshot"),
                "claims": one("SELECT COUNT(*) FROM claim"),
                "discards": one("SELECT COUNT(*) FROM discard"),
                "runs": one("SELECT COUNT(*) FROM run"),
                "stored_mb": sum(
                    p.stat().st_size for p in self.snapshot_dir.rglob("*.txt")
                ) // 1024 // 1024,
            }

    def close(self):
        self.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The evidence store.")
    parser.add_argument("--init", action="store_true", help="create the database")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--show", type=int, metavar="SNAPSHOT_ID")
    parser.add_argument("--ico", help="list what is stored for one company")
    args = parser.parse_args()

    archive = Archive()

    if args.show:
        text = archive.text_of(args.show)
        print(text if text else f"no snapshot {args.show}", file=sys.stdout)
    elif args.ico:
        for row, text in archive.documents(args.ico):
            print(f"[{row['id']}] {row['source']:8} {row['fetched_at']} "
                  f"{row['length']:7} chars  {row['url']}")
    elif args.stats or args.init:
        for key, value in archive.stats().items():
            print(f"  {key:20} {value}")
