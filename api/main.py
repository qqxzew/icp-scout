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
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline.evidence.archive import Archive
from pipeline.run import default_icp, with_defaults
from pipeline.sources.res_bulk import ICP_FORMA


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The one file pipeline/run.py reads (its ICP_FILE). Saving anywhere else
# means the interface accepts a brief the run never sees - which is what
# it used to do, writing icp/config.yaml that nothing read.
ICP_PATH = PROJECT_ROOT / "web" / "icp.json"
WEB_DIR = PROJECT_ROOT / "web"
UI_DATA_DIR = PROJECT_ROOT / "data" / "ui"
RESULTS_PATH = UI_DATA_DIR / "results" / "latest.json"
ARCHIVE = Archive(
	db_path=PROJECT_ROOT / "data" / "archive.db",
	snapshot_dir=PROJECT_ROOT / "data" / "snapshots",
)

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