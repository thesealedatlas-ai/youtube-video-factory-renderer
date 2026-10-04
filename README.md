# youtube-video-factory-renderer

Free renderer for The Sealed Atlas n8n video factory.

- `render/render.py` voices each beat with Piper (local, free) and builds a 1080p MP4 with FFmpeg (slow pan/zoom on free stills, timed captions, crossfades).
- `.github/workflows/render.yml` is started by n8n (workflow_dispatch), renders the job JSON in `jobs/`, uploads the MP4 as a temporary artifact (14 days) and calls n8n back.

No paid APIs. Nothing is uploaded to YouTube from here.
