"""
Lithophane STL Generator API
Wraps main.py as a small HTTP service:

  POST /jobs         multipart image + params -> {job_id}
  GET  /jobs/{id}     -> {status: pending|running|done|error, ...}
  GET  /jobs/{id}/download -> zip of generated STL files
  GET  /health        -> quick liveness check

Generation runs as a background subprocess since a single job can take
several minutes at production resolution - the webshop backend should
poll GET /jobs/{id} (or we add a webhook callback later) rather than
waiting on the POST.
"""
import os
import shutil
import subprocess
import uuid
import zipfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(os.environ.get("DATA_DIR", "/data"))
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

REPO_SRC = Path(__file__).parent  # .../image-to-stl/src

app = FastAPI(title="Lithophane STL Generator")

# in-memory job registry; fine for a single-instance sidecar.
# survives only as long as the container - acceptable since the webshop
# is expected to download the result promptly after "done".
_jobs: dict[str, dict] = {}


class Mode(str, Enum):
    mono = "mono"
    color = "color"


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    error = "error"


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: str
    finished_at: Optional[str] = None
    error: Optional[str] = None
    files: Optional[list[str]] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/jobs", response_model=JobResponse)
async def create_job(
    background_tasks_marker: bool = False,  # placeholder, FastAPI BackgroundTasks used below
    image: UploadFile = File(...),
    mode: Mode = Form(Mode.color),
    width_mm: float = Form(80.0),
    resolution_mm: float = Form(0.4),
    filament_label: str = Form("bambu"),
):
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True)

    input_path = job_dir / f"input{Path(image.filename or 'image.png').suffix or '.png'}"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(image.file, f)

    _jobs[job_id] = {
        "status": JobStatus.pending,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "error": None,
        "files": None,
        "dir": str(job_dir),
        "input": str(input_path),
        "mode": mode,
        "width_mm": width_mm,
        "resolution_mm": resolution_mm,
        "filament_label": filament_label,
    }

    import threading
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()

    return _to_response(job_id)


def _run_job(job_id: str):
    job = _jobs[job_id]
    job["status"] = JobStatus.running
    out_dir = Path(job["dir"]) / "output"

    cmd = [
        "python", str(REPO_SRC / "main.py"),
        "--input", job["input"],
        "--width", str(job["width_mm"]),
        "--resolution", str(job["resolution_mm"]),
        "--filament-label", job["filament_label"],
        "--no-clear",
        "--stl-output", str(out_dir),
    ]
    # NOTE: main.py has no native mono-only mode - it always runs the full
    # CMY solver. For "mono" jobs we currently just discard the C/M/Y
    # results below and keep only the white layers. This means mono jobs
    # pay the same runtime cost as color jobs for now - worth optimizing
    # later (e.g. a light-weight code path that skips the solver
    # entirely when only grayscale is requested).

    try:
        result = subprocess.run(
            cmd, cwd=str(REPO_SRC.parent), capture_output=True, text=True, timeout=1800
        )
        if result.returncode != 0:
            job["status"] = JobStatus.error
            job["error"] = result.stderr[-4000:]
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            return

        stl_files = sorted(out_dir.glob("*.stl"))
        if job["mode"] == Mode.mono:
            stl_files = [f for f in stl_files if f.name.startswith("white_")]
        if not stl_files:
            job["status"] = JobStatus.error
            job["error"] = "Generator lief durch, aber keine STL-Dateien gefunden."
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            return

        zip_path = Path(job["dir"]) / "result.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in stl_files:
                zf.write(f, arcname=f.name)

        job["status"] = JobStatus.done
        job["files"] = [f.name for f in stl_files]
        job["zip"] = str(zip_path)
        job["finished_at"] = datetime.now(timezone.utc).isoformat()

    except subprocess.TimeoutExpired:
        job["status"] = JobStatus.error
        job["error"] = "Zeitlimit (30 Min) überschritten."
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:  # noqa: BLE001
        job["status"] = JobStatus.error
        job["error"] = str(exc)
        job["finished_at"] = datetime.now(timezone.utc).isoformat()


def _to_response(job_id: str) -> JobResponse:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    return JobResponse(
        job_id=job_id,
        status=job["status"],
        created_at=job["created_at"],
        finished_at=job["finished_at"],
        error=job["error"],
        files=job["files"],
    )


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str):
    return _to_response(job_id)


@app.get("/jobs/{job_id}/download")
def download_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if job["status"] != JobStatus.done:
        raise HTTPException(status_code=409, detail=f"Job ist noch '{job['status']}'")
    return FileResponse(job["zip"], media_type="application/zip", filename=f"lithophane-{job_id}.zip")
