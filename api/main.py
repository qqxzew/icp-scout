import json
import os
import tempfile
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from pipeline.evidence.archive import Archive


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICP_PATH = PROJECT_ROOT / "icp" / "config.yaml"
RESULTS_PATH = PROJECT_ROOT / "data" / "results.json"
ARCHIVE = Archive(
	db_path=PROJECT_ROOT / "data" / "archive.db",
	snapshot_dir=PROJECT_ROOT / "data" / "snapshots",
)

app = FastAPI()
app.add_middleware(
	CORSMiddleware,
	allow_origins=["http://localhost:8123", "http://127.0.0.1:8123"],
	allow_methods=["POST", "GET"],
	allow_headers=["Content-Type"],
)


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


@app.post("/api/icp")
def save_filters(filters: Filters):
	document = {
		"nace": filters.nace,
		"sizes": filters.sizes,
		"regions": filters.regions,
		"location": {
			"from": filters.from_,
			"km": filters.km,
			"origin": filters.origin.model_dump() if filters.origin else None,
		},
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
			yaml.safe_dump(document, file, allow_unicode=True, sort_keys=False)
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

	try:
		with RESULTS_PATH.open(encoding="utf-8") as file:
			document = json.load(file)
	except (OSError, json.JSONDecodeError) as error:
		raise HTTPException(status_code=500, detail="Results file is invalid") from error

	if not isinstance(document, dict) or not isinstance(document.get("results"), list):
		raise HTTPException(status_code=500, detail="Results file has invalid format")

	return {
		"run_id": document.get("run_id"),
		"finished_at": document.get("finished_at"),
		"results": document["results"][:5],
	}