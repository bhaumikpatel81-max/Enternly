"""
STEP A3 — SadTalker GPU microservice.

This is a standalone FastAPI service that runs ON the GPU instance,
separate from the main Enternly backend.

Workflow:
  1. Enternly backend POSTs {face image + audio file} to /render
  2. This service runs SadTalker to produce an MP4
  3. Uploads the MP4 to GCS
  4. Returns {"video_url": "gs://bucket/path.mp4"}

Deploy with Dockerfile.sadtalker (see README.md).

Requirements (installed in the Docker image):
  - SadTalker: https://github.com/OpenTalker/SadTalker
  - torch>=2.0 (CUDA 11.8)
  - google-cloud-storage
  - fastapi, uvicorn, python-multipart
"""
import os
import uuid
import tempfile
import subprocess

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="Enternly SadTalker Service")

GCS_BUCKET   = os.environ.get("GCS_BUCKET", "enternly-enteri-ai-media")
SADTALKER_DIR = os.environ.get("SADTALKER_DIR", "/sadtalker")


@app.get("/health")
def health():
    # Quick check that SadTalker inference script exists
    ok = os.path.isfile(os.path.join(SADTALKER_DIR, "inference.py"))
    return {"status": "ok" if ok else "sadtalker_missing", "gpu": _gpu_available()}


@app.post("/render")
async def render(face: UploadFile = File(...), audio: UploadFile = File(...)):
    """
    Accept face image + audio, run SadTalker, upload to GCS, return video URL.
    Falls back gracefully: if SadTalker errors, returns {"video_url": null}.
    """
    session = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as tmp:
        face_path  = os.path.join(tmp, f"face_{session}.png")
        audio_path = os.path.join(tmp, f"audio_{session}.wav")
        out_dir    = os.path.join(tmp, "output")
        os.makedirs(out_dir, exist_ok=True)

        with open(face_path, "wb")  as f: f.write(await face.read())
        with open(audio_path, "wb") as f: f.write(await audio.read())

        try:
            result = _run_sadtalker(face_path, audio_path, out_dir, session)
            if not result:
                return JSONResponse({"video_url": None, "reason": "SadTalker produced no output"})
            video_url = _upload_gcs(result, session)
            return {"video_url": video_url}
        except Exception as exc:
            return JSONResponse({"video_url": None, "reason": str(exc)})


def _run_sadtalker(face: str, audio: str, out_dir: str, sid: str) -> str | None:
    """Run SadTalker inference. Returns path to output MP4 or None."""
    cmd = [
        "python", os.path.join(SADTALKER_DIR, "inference.py"),
        "--source_image", face,
        "--driven_audio", audio,
        "--result_dir", out_dir,
        "--still",           # head stays mostly still (interview context)
        "--enhancer", "gfpgan",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=200)
    if proc.returncode != 0:
        raise RuntimeError(f"SadTalker failed: {proc.stderr[-500:]}")
    # SadTalker writes the output as *.mp4 in result_dir
    for f in os.listdir(out_dir):
        if f.endswith(".mp4"):
            return os.path.join(out_dir, f)
    return None


def _upload_gcs(local_path: str, sid: str) -> str:
    """Upload to GCS and return the public(ish) object URL."""
    from google.cloud import storage
    client = storage.Client()
    blob = client.bucket(GCS_BUCKET).blob(f"enteri-ai-avatars/{sid}.mp4")
    blob.upload_from_filename(local_path, content_type="video/mp4")
    # Return a signed URL or gs:// path — backend serves it to the client
    return f"gs://{GCS_BUCKET}/enteri-ai-avatars/{sid}.mp4"


def _gpu_available() -> bool:
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False
