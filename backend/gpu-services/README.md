# NexAI GPU Avatar Services — STEP A3

Two open-source lip-sync options. Both take: **face image + audio → MP4 video**.

## Prerequisites (GCP)

| Resource | Recommended |
|---|---|
| Machine type | n1-standard-4 (4 vCPU, 15 GB RAM) |
| GPU | NVIDIA Tesla T4 (1x) |
| OS image | `c2-deeplearning-pytorch-gpu-v20241003-debian-11` |
| Disk | 100 GB SSD |
| Cost (preemptible) | ~$0.35 / hour |
| Cost (on-demand) | ~$0.95 / hour |

**Render time per question** (10–15 s audio):
- SadTalker: ~60–90 s on T4
- Wav2Lip: ~30–50 s on T4

## SadTalker

```bash
# Build
docker build -f Dockerfile.sadtalker -t enternly-sadtalker .

# Run (GPU required)
docker run --gpus all -p 8090:8090 \
  -e GCS_BUCKET=your-bucket-name \
  -v /path/to/avatars:/avatars \
  enternly-sadtalker
```

Set in backend environment:
```
AVATAR_PROVIDER=sadtalker
SADTALKER_SERVICE_URL=http://<gpu-instance-ip>:8090
AVATAR_FACE_DIR=/app/frontend/assets/avatars
```

## Wav2Lip

Same pattern — use `Dockerfile.wav2lip` and set `WAV2LIP_SERVICE_URL`.

## Face images

Store AI-generated faces in `frontend/assets/avatars/`:
- `nexai-female.png` — AI-generated female face
- `nexai-male.png` — AI-generated male face

Generate from:
- https://thispersondoesnotexist.com (JPEG, free, no attribution needed)
- Stable Diffusion with a photorealistic model (fully offline)
- StyleGAN2/3 (research licence — confirm with legal before production)

**Important:** Do not use real people's photos. Use only AI-generated faces.

## Fallback

Both providers **automatically fall back to the orb** if:
- Service URL is not set
- GPU container is not running
- Render fails for any reason

The orb visual always works. GPU rendering is progressive enhancement only.
