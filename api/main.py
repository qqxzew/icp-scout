"""The interface: the ICP screens and the four calls behind them.

One process serves both. A page cannot write to disk on its own, so
saving the brief has to reach a local server anyway - and the screens
already need one to read data/ui/*.json at all, which file:// forbids.
Given that, two servers would only add a port, a CORS list and a way for
half the app to be up.

Run:
    python -m uvicorn api.main:app --port 8000
    http://127.0.0.1:8000/
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline.evidence.archive import Archive
from pipeline.run import default_icp, with_defaults
from pipeline.scoring.card import build as build_card
from pipeline.scoring.card import for_web as card_for_web
from pipeline.scoring.card import load_turnover_cache
from pipeline.scoring.select import CONTACTS, WEBSITES, load_jsonl
from pipeline.signals.now import ARES_CANDIDATES, load_companies
from pipeline.sources.res_bulk import ICP_FORMA


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The one file pipeline/run.py reads (its ICP_FILE). Saving anywhere else
# means the interface accepts a brief the run never sees - which is what
# it used to do, writing icp/config.yaml that nothing read.
ICP_PATH = PROJECT_ROOT / "web" / "icp.json"
WEB_DIR = PROJECT_ROOT / "web"
UI_DATA_DIR = PROJECT_ROOT / "data" / "ui"
RESULTS_PATH = UI_DATA_DIR / "results" / "latest.json"
RUN_LOG = PROJECT_ROOT / "data" / "run_web.log"
ARCHIVE = Archive(
	db_path=PROJECT_ROOT / "data" / "archive.db",
	snapshot_dir=PROJECT_ROOT / "data" / "snapshots",
)

# Held between requests rather than re-read on each one - the same call
# select.py's own CLI makes, and re-reading a 9800-row candidate file and
# two jsonl caches on every card click would make the one interactive
# screen in this prototype the slow one. Turnover is the exception: its
# cache is read fresh per request (cheap, one small file) since card.py
# itself appends to it as new companies get looked up.
class Caches:
	"""The three files a card is built from, re-read when a run rewrites them.

	These used to be three module-level dicts filled once at import, which
	is correct for exactly as long as nobody runs the pipeline. A run
	rewrites all three, and from that moment the server answered every
	card click with "IČO není v seznamu kandidátů" until somebody
	restarted it - measured on the clean rebuild, where all five delivered
	companies 404'd because the process still held the candidate list as
	it looked eight hours earlier.

	Reloading on a timer would either be too slow to help or reload for
	nothing; reloading at the end of a run would miss runs started from
	the terminal, which count just as much. The file's own mtime is the
	one signal that is true whoever wrote it.
	"""

	SOURCES = (ARES_CANDIDATES, WEBSITES, CONTACTS)

	def __init__(self):
		self.lock = threading.Lock()
		self.stamp = None
		self.companies = {}
		self.websites = {}
		self.contacts = {}

	@staticmethod
	def stamp_of(paths):
		"""Size next to mtime: a rewrite inside one mtime tick still moves it."""
		marks = []
		for path in paths:
			try:
				info = Path(path).stat()
				marks.append((info.st_mtime_ns, info.st_size))
			except OSError:
				# Missing is a state, not an error - the file may not be
				# built yet, and it will come back on its own.
				marks.append(None)
		return tuple(marks)

	def current(self):
		stamp = self.stamp_of(self.SOURCES)
		with self.lock:
			if stamp != self.stamp:
				self.companies = {c["ico"]: c for c in load_companies(ARES_CANDIDATES)}
				self.websites = load_jsonl(WEBSITES)
				self.contacts = load_jsonl(CONTACTS)
				self.stamp = stamp
			return self.companies, self.websites, self.contacts


CACHES = Caches()

app = FastAPI()


class Origin(BaseModel):
	name: str
	lat: float
	lon: float


class Filters(BaseModel):
	nace: list[str] = Field(default_factory=list)
	sizes: list[str] = Field(default_factory=list)
	regions: list[str] = Field(default_factory=list)
	km: int | None = Field(default=None, ge=1)
	from_: str = Field(default="", alias="from")
	origin: Origin | None = None

	model_config = {"populate_by_name": True}


def unset(values):
	"""Empty in the interface means "vše", and that is not an empty list.

	res_bulk.matches() skips a criterion that is None and matches nothing
	against an empty list, so the two spellings are opposites. The screen
	that shows "vše" has to arrive at the pipeline as None.
	"""
	return values or None


@app.get("/api/icp")
def read_filters():
	"""What the next run would use: the saved brief, or ICP.

	The interface opens on this instead of on an empty form. A blank
	screen would be a lie about the prototype's state - the pipeline has
	an ICP either way, and hiding it would only invite retyping it by
	hand. An empty field in a saved brief is not "everything" either -
	see with_defaults() - so a screen the salesperson never opened keeps
	showing RTsoft's own answer for it, not a silent "vše".
	"""
	fallback = default_icp()
	if not ICP_PATH.is_file():
		return fallback

	saved = json.loads(ICP_PATH.read_text(encoding="utf-8"))
	return with_defaults(saved, fallback)


def location_of(filters):
	"""The saved geography, marked with whether anybody actually chose it.

	filters/brief.py excludes on a radius a person typed and only orders
	on the one that ships with RTsoft's ICP ("preferovaně", not "pouze").
	The screen cannot tell the two apart by itself: it opens pre-filled
	with the built-in 150 km around Plzeň, so a salesperson who edits the
	industry list and saves would post that radius back as if it were
	their own choice - and the brief's preference would silently become a
	hard geographic cut.

	So the comparison happens here: a location identical to the built-in
	one is still the built-in one, whoever pressed save. The moment any
	part of it differs, somebody decided, and it filters.
	"""
	chosen = {
		"from": filters.from_,
		"km": filters.km,
		"origin": filters.origin.model_dump() if filters.origin else None,
	}
	built_in = default_icp().get("location") or {}
	same = (chosen["km"] == built_in.get("km")
	        and (chosen["origin"] or {}).get("name") == (built_in.get("origin") or {}).get("name"))
	if same:
		chosen["from_default"] = True
	return chosen


@app.post("/api/icp")
def save_filters(filters: Filters):
	document = {
		# Named the way pipeline/sources/res_bulk.py names its criteria,
		# not the way the screens are labelled: the file is read by the
		# pipeline, and a rename between the two ends is one more place
		# for the brief to quietly stop being applied.
		"nace": unset(filters.nace),
		"katpo": unset(filters.sizes),
		# The interface offers no legal form, but the run filters on one.
		# Recorded anyway so the saved brief equals what actually ran.
		"forma": sorted(ICP_FORMA),
		"regions": unset(filters.regions),
		"location": location_of(filters),
	}

	ICP_PATH.parent.mkdir(parents=True, exist_ok=True)
	file_descriptor, temporary_name = tempfile.mkstemp(
		dir=ICP_PATH.parent,
		prefix=f"{ICP_PATH.name}.",
		suffix=".tmp",
		text=True,
	)
	try:
		with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as file:
			json.dump(document, file, ensure_ascii=False, indent=2)
			file.write("\n")
		os.replace(temporary_name, ICP_PATH)
	except Exception:
		os.unlink(temporary_name)
		raise

	return {"saved": True, "path": str(ICP_PATH.relative_to(PROJECT_ROOT))}


@app.get("/api/snapshot/{snapshot_id}", response_class=PlainTextResponse)
def get_snapshot(snapshot_id: int):
	text = ARCHIVE.text_of(snapshot_id)
	if text is None:
		raise HTTPException(status_code=404, detail="Snapshot not found")
	return text


@app.get("/api/results")
def get_results():
	if not RESULTS_PATH.is_file():
		raise HTTPException(status_code=404, detail="No completed run found")
	return FileResponse(RESULTS_PATH, media_type="application/json")


# The run is a subprocess, not a thread or an inline call. pipeline/run.py
# already is a command with a preflight, its own Archive connections and a
# habit of writing to stderr; calling run() inside the request would hold a
# worker for the length of a real run and take the interface down with it if
# a source raised. A separate process also means the log is a file somebody
# can read afterwards, which is the first thing asked when a run comes back
# with three companies instead of five.
#
# One at a time: two concurrent runs would write the same results file and
# the same archive rows, and there is exactly one person pressing the button.
RUN = {
	"process": None,
	"started_at": None,
	"finished_at": None,
	"returncode": None,
}
RUN_LOCK = threading.Lock()


def run_state():
	"""The status the interface polls, plus when the last finished run was.

	poll() is what turns "started" into "finished" - there is no callback
	from a subprocess, so the state advances when somebody asks. The button
	polls anyway, so nothing else has to.
	"""
	with RUN_LOCK:
		process = RUN["process"]
		if process is not None and process.poll() is not None:
			RUN["returncode"] = process.returncode
			RUN["finished_at"] = datetime.now().isoformat(timespec="seconds")
			RUN["process"] = None

		running = RUN["process"] is not None
		state = {
			"running": running,
			"started_at": RUN["started_at"],
			"finished_at": RUN["finished_at"],
			"returncode": RUN["returncode"],
		}

	# Read from the results file rather than from RUN: the timer has to
	# survive a server restart, and a run started from the terminal counts
	# just as much as one started from the button.
	state["last_run"] = None
	if RESULTS_PATH.is_file():
		try:
			document = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
			state["last_run"] = {
				"generated_at": document.get("generated_at"),
				"delivered": document.get("delivered"),
				"qualified": document.get("qualified"),
				"run_id": document.get("run_id"),
			}
		except (json.JSONDecodeError, OSError):
			# A results file that cannot be read is not a reason to refuse
			# to start a new run - which is exactly what the caller wants.
			pass
	return state


@app.get("/api/run")
def get_run():
	return run_state()


@app.post("/api/run")
def start_run():
	state = run_state()
	if state["running"]:
		# Not an error: the button was pressed twice, and the honest answer
		# is the state of the run that is already going.
		return state

	RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
	log = RUN_LOG.open("w", encoding="utf-8")
	log.write(f"# run started from the interface {datetime.now().isoformat(timespec='seconds')}\n")
	log.flush()

	with RUN_LOCK:
		RUN["process"] = subprocess.Popen(
			[sys.executable, "-m", "pipeline.run"],
			cwd=PROJECT_ROOT,
			stdout=log,
			stderr=subprocess.STDOUT,
			# PYTHONUNBUFFERED: a run that is still going has a log worth
			# tailing instead of an empty file.
			# PYTHONIOENCODING: the log file is opened as utf-8 here, and
			# without this the child encodes its own output as cp1252 on
			# Windows - the run's "·" separators arrived as mojibake.
			env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
		)
		RUN["started_at"] = datetime.now().isoformat(timespec="seconds")
		RUN["finished_at"] = None
		RUN["returncode"] = None

	return run_state()


@app.get("/api/run/log", response_class=PlainTextResponse)
def get_run_log():
	"""The tail of the current or last run, for when it ends badly.

	Nothing in the interface links here; it is for the person demonstrating
	the prototype, who otherwise has to go looking for the terminal.
	"""
	if not RUN_LOG.is_file():
		raise HTTPException(status_code=404, detail="No run has been started from the interface")
	return RUN_LOG.read_text(encoding="utf-8", errors="replace")


@app.get("/api/card/{ico}")
def get_card(ico: str):
	"""One company's dossier, in the shape web/card/ renders.

	fetch_turnover=False: sbirka.py is four sequential requests to
	justice.cz, and this endpoint is called from a page a person is
	looking at, not a batch run - a cache miss must not make the click
	hang. Enrichment happens at stage 06 of a real run (card.py --top),
	which fills the same cache this reads.
	"""
	ico = ico.strip().zfill(8)
	companies, websites, contacts = CACHES.current()
	if ico not in companies:
		raise HTTPException(status_code=404, detail="IČO není v seznamu kandidátů")
	card = build_card(ico, ARCHIVE, companies, websites, contacts,
	                   turnover_cache=load_turnover_cache(), fetch_turnover=False)
	return card_for_web(card)


# Mounted last, and only after every /api route above: a mount on "/"
# matches everything, and routes are tried in the order they were added.
#
# check_dir=False because data/ui/ is generated, not cloned. Refusing to
# start at all would hide a fixable step behind a stack trace, so the
# server comes up and says which command produces it.
if not UI_DATA_DIR.is_dir():
	print(f"api: {UI_DATA_DIR} is missing - the screens will stay empty.\n"
	      f"     build it: python -m pipeline.build_ui_data", file=sys.stderr)

app.mount("/data/ui", StaticFiles(directory=UI_DATA_DIR, check_dir=False), name="ui-data")
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")