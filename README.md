# Myanmar Video Studio Backend (Render)

FastAPI + FFmpeg backend for video upload/splitting, AssemblyAI Burmese speech-to-text, Gemini translation, ElevenLabs Burmese text-to-speech, and optional Google Cloud Storage.

## Providers
- AssemblyAI Universal-2: prerecorded transcription; Burmese language code `my`.
- Gemini: translation endpoint `/api/translate`.
- ElevenLabs: Burmese TTS endpoint `/api/voice/jobs` using `eleven_v3`. Use an ElevenLabs voice that supports Burmese; pass its voice ID or set `ELEVENLABS_VOICE_ID`.

## Deploy
1. Push this folder to a GitHub repository.
2. In Render choose **New > Blueprint** and select the repository.
3. Add Render environment variables: `ASSEMBLYAI_API_KEY`, `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID` (recommended). Configure `CORS_ORIGINS` with your frontend origin.
4. Deploy and test `GET /health`.

Optional durable storage: create a Google Cloud Storage bucket and set `GCS_BUCKET` and `GCS_CREDENTIALS_JSON` to a service-account JSON string. Render local disk is ephemeral unless a persistent disk is attached. Temporary processing files are stored under `/tmp`.

## API
- `GET /health`
- `POST /api/video/upload` multipart `video`
- `POST /api/video/{job_id}/split` JSON `{ "sections": 1 }` or `{ "sections": 2 }`
- `POST /api/video/transcribe` JSON `{ "job_id": "...", "language_hint": "my" }` (AssemblyAI; returns text and word timestamps)
- `POST /api/translate` JSON `{ "text": "...", "target_language": "Burmese" }`
- `POST /api/voice/quote` JSON `{ "text": "...", "output_format": "mp3" }`
- `POST /api/voice/jobs` JSON `{ "text": "...", "output_format": "mp3", "voice_id": "optional" }`
- `GET /api/voice/jobs/{job_id}`
- `GET /api/voice/jobs/{job_id}/audio?format=mp3`
- `GET /api/storage/{job_id}/files`

## Important production notes
This is a deployable starter backend, not a fully hardened public service. Add authentication, rate limiting, request quotas, file-type validation, malware scanning, and durable job metadata before public use. Large uploads are constrained by Render plan/proxy limits, available disk, RAM, and request timeouts; `MAX_UPLOAD_MB` is not an unlimited-upload guarantee. Store API keys only in Render environment variables, never in Gemini Canvas/browser code.
