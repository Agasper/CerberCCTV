# CerberCCTV

Home video surveillance in two parts: an **agent** on a Raspberry Pi reads the
RTSP stream of an IP camera (Hikvision or any other RTSP-capable camera),
detects motion and assembles clips, while an **admin panel** on an external
server receives those clips, stores them in S3-compatible storage and serves a
web UI — a browsable list of recordings with a player, live view and settings.
Everything runs in Docker.

The key property: **no static IP or port forwarding needed at home**. The agent
talks to the admin panel using outbound HTTPS/WSS connections only, so it works
behind any NAT. The agent uploads video to the admin panel, and the panel
writes it to S3 itself — storage credentials never reach the Raspberry Pi.

```
┌────── home (NAT, no static IP) ────────┐   ┌──── DO App Platform ─────┐
│ Camera ──RTSP──> Agent (Docker)        │   │  Admin FastAPI (:8080,   │
│   main stream -> ffmpeg ring buffer    │   │  TLS by the platform)    │
│   substream   -> OpenCV motion         │   │    │            │        │
│   clip ──HTTPS (chunked)────────────────────>  upload API   S3       │
│   heartbeat/config/commands ──WSS───────────>  agent API             │
│   live fMP4 ──WSS───────────────────────────>  WS relay ──MSE──> browser
└────────────────────────────────────────┘   │       Postgres (external)│
                                             └──────────────────────────┘
```

## How it works

**Recording without transcoding.** ffmpeg on the agent continuously copies the
camera's main stream into a ring buffer of 2-second segments (`-c copy`, almost
no CPU). The buffer lives in tmpfs — the Raspberry Pi's SD card is not worn out.

**Motion detection.** OpenCV reads the camera's low-resolution substream
(MOG2 background subtraction → contours → moving area as % of the frame).
Sensitivity, minimum area, pre/post-roll, cooldown between events and maximum
clip length are configured in the admin panel and applied by the agent on the
fly, without restarts. You can also draw **detection zones** — polygons over a
camera snapshot; motion outside the zones is ignored (the area threshold is
computed against the zone area).

**A motion event.** On trigger the agent takes the pre-roll segments from the
buffer, waits for the motion to end (plus post-roll), concatenates everything
into an mp4 (faststart, plays in the browser) and puts it into a local queue
(outbox).

**Delivery.** The clip is uploaded to the admin panel in 8 MiB chunks with
`Content-Range` — an interrupted upload resumes where it stopped. The panel
streams every chunk straight into an S3 multipart upload without relying on its
own disk (which is ephemeral on App Platform). While the panel is unreachable,
clips accumulate in the outbox and are delivered later.

**Live view.** When you press the button, the agent receives a WebSocket
command, starts ffmpeg (stream copy again) and pushes fMP4 fragments over WSS
to the panel, which fans them out to viewers; the browser plays them via an MSE
player. Latency is 1–3 seconds. The stream shuts down by itself once all
viewers leave (and on a timeout, as a safety net).

**Resilience.** Both sides survive each other's restarts: the agent caches its
config locally and keeps clips in the outbox, the panel persists multipart
upload state in Postgres. ffmpeg and the detector restart on stream loss and
when the camera freezes.

## Repository layout

```
common/   pydantic models of the agent<->admin API contract
agent/    Raspberry Pi agent (capture, motion, outbox, live)
admin/    FastAPI admin panel (UI, clip ingest, S3, live relay, retention)
.do/      App Spec for deploying to DigitalOcean App Platform
docker-compose.dev.yml  local e2e environment with a fake camera and MinIO
```

## Quick start: local environment (no hardware, no cloud)

Docker is all you need. Everything comes up at once: Postgres, MinIO (local
S3), a fake RTSP camera (MediaMTX + ffmpeg with a moving test pattern), the
admin panel and the agent:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

A minute or two later:

- admin panel: http://localhost:8090 — login `admin`, password `admin123`;
- the dashboard shows a "camera" snapshot and the agent status;
- the test pattern moves constantly, so events are produced back to back
  (each lasts up to `max_clip_s`, 120 s by default) — recordings appear on
  the Events page and play back from MinIO;
- MinIO console: http://localhost:9001 (`cerber` / `cerber-secret`).

## Production deployment

### 1. Admin panel — DigitalOcean App Platform

This assumes you already have Postgres (a DO managed database or your own) and
an S3-compatible storage (DO Spaces, Backblaze, MinIO on a VPS, etc.).

1. Fork/push the repository to GitHub (the spec references
   `Agasper/CerberCCTV`, adjust if needed).
2. Create the app from the spec:
   ```bash
   doctl apps create --spec .do/app.yaml
   ```
   or via the UI (Create App → import the repository). The platform builds
   `admin/Dockerfile` and gives you a domain like
   `https://<app>.ondigitalocean.app` with TLS.
3. Set the environment variables (secrets):
   - `DATABASE_URL` — the Postgres connection string (for DO managed databases
     it comes with `?sslmode=require`; the parameter is handled correctly);
   - `SECRET_KEY` — any long random string (session cookie signing);
   - `ADMIN_PASSWORD` — the initial login password (used once, when the
     database is empty);
   - `AGENT_TOKEN` — optional: you can instead issue a token on the Settings
     page after logging in.
4. Migrations run automatically on container start (alembic in the
   entrypoint).
5. Open the admin panel → Settings → fill in the S3 section (endpoint, bucket,
   keys; for DO Spaces the endpoint looks like
   `https://fra1.digitaloceanspaces.com`) and press "Test connection".

### 2. Agent — Raspberry Pi

You need a Raspberry Pi with a **64-bit** OS (for aarch64 opencv wheels) and
Docker.

```bash
git clone https://github.com/Agasper/CerberCCTV.git
cd CerberCCTV/agent
cp .env.example .env        # fill in ADMIN_URL and AGENT_TOKEN
docker compose up -d --build
```

Everything else is configured from the admin panel, the Camera section:

- main stream: `rtsp://admin:PASSWORD@192.168.0.10:554/Streaming/Channels/101`
- substream: `rtsp://admin:PASSWORD@192.168.0.10:554/Streaming/Channels/102`

The camera credentials are stored only in the admin panel's database; they
appear neither in the repository nor on the Pi's disk.

### 3. Camera setup (Hikvision)

- Enable the substream with a small resolution, e.g. 640×360 @ 5–10 fps — the
  detector analyses it; the smaller it is, the easier on the Pi.
- Use the **H.264** video codec. H.265 will be recorded and uploaded fine, but
  live view and in-browser playback depend on HEVC support in the specific
  browser/hardware; H.264 works everywhere.
- Keep the main stream's I-frame interval (GOP) at 1–2 seconds: it affects
  segment-cut precision and live latency.

## Security

- Admin panel: session-based auth (bcrypt, signed cookies), password change in
  settings. Agent: Bearer token; only its sha256 hash is stored, one-click
  reissue.
- App Platform terminates TLS: both the agent and the browser talk
  HTTPS/WSS.
- S3 keys live only in the admin panel's database. Recordings are served via
  short-lived presigned URLs.

## Limitations and notes

- One agent = one camera. The database schema is designed for multiple agents,
  but the UI and the live relay currently assume a single one.
- Live view on iPhone: Safari supports MSE starting with iOS 17.1
  (ManagedMediaSource); on older iOS live view won't work (recordings will).
  Not verified on a real device.
- Audio: transcoded to AAC both while recording into the ring buffer and in
  live view (IP cameras usually output G.711, which neither MP4 nor browsers
  can play; the transcode costs ~1-2% CPU), so clips and live carry sound with
  any camera. The live player starts muted — browsers block autoplay with
  sound — unmute it in the player controls.
- The exact request body size limit of the App Platform ingress is not
  documented — that's why uploads go in 8 MiB chunks.
- Verified end-to-end in the local dev environment (fake camera + MinIO +
  Postgres in Docker) and against a real Hikvision camera (H.264 1080p/25fps,
  both streams). The App Platform deployment follows the documentation but has
  not been exercised on live infrastructure by the author — if something
  doesn't add up, start with the agent's `docker logs` and the app's Runtime
  Logs in DO.

## License

MIT — see [LICENSE](LICENSE).
