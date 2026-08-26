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
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_ico ON claim(ico, kind);

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
        """
        if state not in ("fact", "inference"):
            raise ValueError(f"state must be fact or inference, got {state!r}")
        with self._lock:
            cursor = self.db.execute(
                "INSERT INTO claim (ico, kind, value, state, quote, snapshot_id, run_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(ico).zfill(8), kind, value, state, quote, snapshot_id, run_id, now()),
            )
            self.db.commit()
            return cursor.lastrowid

    def claims(self, ico, kind=None):
        with self._lock:
            return self.db.execute(
                "SELECT c.*, s.url, s.fetched_at FROM claim c"
                " JOIN snapshot s ON s.id = c.snapshot_id"
                " WHERE c.ico = ? AND (? IS NULL OR c.kind = ?)"
                " ORDER BY c.created_at DESC",
                (str(ico).zfill(8), kind, kind),
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
