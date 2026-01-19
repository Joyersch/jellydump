import os
import uuid
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, Field, field_validator
from dotenv import load_dotenv


load_dotenv()
BASE_DATA_PATH = os.getenv("BASE_DATA_PATH")
BASE_DATA_DIR = Path(BASE_DATA_PATH)

app = FastAPI(title="Jellydump")

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

class RequestPayload(BaseModel):
    url: HttpUrl
    imdbid: str = Field(..., description="IMDb ID, e.g. 'tt0111161'")
    name: str = Field(..., description="Base folder name")
    season: int = Field(..., ge=1, description="Season number (>=1)")

    @field_validator("imdbid")
    @classmethod
    def validate_imdbid(cls, v: str) -> str:
        if not v.startswith("tt") or len(v) < 7:
            raise ValueError("Invalid IMDb ID (expected format like 'tt0111161').")
        return v

    @field_validator("season")
    @classmethod
    def validate_season(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Season must be >= 1.")
        return v

def build_output_template(name: str, season: int) -> str:
    season_str = f"{season:02d}"
    episode_placeholder = "%(autonumber)02d"      # 01, 02, …
    template = f"{name} S{season_str}E{episode_placeholder}.%(ext)s"
    return template

def count_media_files(folder: Path, suffix: str = ".mp4") -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.iterdir() if p.is_file() and p.suffix.lower() == suffix.lower())

def _yt_dlp_progress_hook(job_id: str, season_dir: Path):
    def hook(d: dict):
        if d.get("status") not in ("downloading", "finished"):
            return

        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        percent = int((downloaded / total) * 100) if total else 0

        title = d.get("info_dict", {}).get("title", "")

        speed_raw = d.get("speed")
        if speed_raw:
            if speed_raw > 1024**3:
                speed = f"{speed_raw / 1024**3:.2f} GiB/s"
            elif speed_raw > 1024**2:
                speed = f"{speed_raw / 1024**2:.2f} MiB/s"
            elif speed_raw > 1024:
                speed = f"{speed_raw / 1024:.2f} KiB/s"
            else:
                speed = f"{speed_raw:.0f} B/s"
        else:
            speed = ""

        episode_no = count_media_files(season_dir, suffix=".mp4")

        with jobs_lock:
            job_meta = jobs.get(job_id)
            if job_meta:
                job_meta.update({
                    "progress_percent": percent,
                    "current_title": title,
                    "speed": speed,
                    "current_episode": episode_no,
                })
    return hook

def _run_download(job_id: str, payload: RequestPayload) -> None:
    from yt_dlp import YoutubeDL

    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["started_at"] = datetime.now().isoformat()
        jobs[job_id].update({
            "progress_percent": 0,
            "current_title": "",
            "speed": "",
        })

    try:
        safe_name = payload.name.strip()
        main_folder_name = f"{safe_name} [imdbid-{payload.imdbid}]"
        main_dir = BASE_DATA_DIR / main_folder_name
        main_dir.mkdir(parents=True, exist_ok=True)

        season_str = f"{payload.season:02d}"
        season_folder_name = f"Season {season_str}"
        season_dir = main_dir / season_folder_name
        season_dir.mkdir(parents=False, exist_ok=False)   # error if exists

        output_template = build_output_template(safe_name, payload.season)
        ydl_opts = {
            "outtmpl": str(season_dir / output_template),
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
            "merge_output_format": "mp4",
            "ignoreerrors": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                },
                {
                    "key": "FFmpegMetadata",
                },
            ],
            "nopart": True,
            "progress_hooks": [_yt_dlp_progress_hook(job_id, season_dir)],
        }

        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([str(payload.url)])

        with jobs_lock:
            jobs[job_id].update({
                "status": "finished",
                "finished_at": datetime.utcnow().isoformat(),
                "result_path": str(season_dir),
                "message": "Download completed successfully",
                "progress_percent": 100,
                "current_title": "",
                "speed": "",
            })

    except FileExistsError:
        with jobs_lock:
            jobs[job_id].update({
                "status": "failed",
                "finished_at": datetime.utcnow().isoformat(),
                "error": "Season folder already exists",
            })
    except Exception as exc:
        with jobs_lock:
            jobs[job_id].update({
                "status": "failed",
                "finished_at": datetime.utcnow().isoformat(),
                "error": str(exc),
            })

@app.post("/pull")
async def pull(request: RequestPayload, background_tasks: BackgroundTasks):
    with jobs_lock:
        active = any(meta["status"] in ("pending", "running") for meta in jobs.values())
        if active:
            raise HTTPException(
                status_code=409,
                detail="Another download is already in progress. Please wait or cancel it."
            )

        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
            "payload": request.dict(),
        }

    background_tasks.add_task(_run_download, job_id, request)

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Download started.",
    }

@app.get("/status/{job_id}")
async def status(job_id: str):
    with jobs_lock:
        meta = jobs.get(job_id)

    if not meta:
        raise HTTPException(status_code=404, detail="Job ID not found")

    resp = {
        "job_id": job_id,
        "status": meta["status"],
        "created_at": meta.get("created_at"),
    }

    if "progress_percent" in meta:
        resp["progress_percent"] = meta["progress_percent"]

    if "current_title" in meta and meta["current_title"]:
        resp["current_title"] = meta["current_title"]

    if "speed" in meta and meta["speed"]:
        resp["speed"] = meta["speed"]

    if "current_episode" in meta and meta["current_episode"] is not None:
        resp["current_episode"] = meta["current_episode"]

    if meta["status"] in ("pending", "running"):
        resp["message"] = "Job is still processing."

    if meta["status"] == "finished":
        resp.update({
            "finished_at": meta.get("finished_at"),
            "result_path": meta.get("result_path"),
            "message": meta.get("message"),
        })

    if meta["status"] == "failed":
        resp.update({
            "finished_at": meta.get("finished_at"),
            "error": meta.get("error"),
        })

    return resp

app.mount("/static", StaticFiles(directory="static"), name="static")
@app.get("/", response_class=FileResponse)
async def ui():
    return FileResponse(os.path.join("static", "index.html"))