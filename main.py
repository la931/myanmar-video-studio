import os, uuid, shutil, subprocess, json, asyncio
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from google import genai
from google.cloud import storage
from google.oauth2 import service_account

ROOT = Path(os.getenv("WORK_DIR", "/tmp/myanmar-studio")); ROOT.mkdir(parents=True, exist_ok=True)
GEMINI = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ASSEMBLYAI = os.getenv("ASSEMBLYAI_API_KEY", "")
ELEVENLABS = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL_ID", "eleven_v3")
BUCKET = os.getenv("GCS_BUCKET", "")
GCS_JSON = os.getenv("GCS_CREDENTIALS_JSON", "")
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "2048"))
app = FastAPI(title="Myanmar Video Studio API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",")], allow_methods=["*"], allow_headers=["*"])

class SplitBody(BaseModel): sections: int = Field(default=2, ge=1, le=2)
class TranscribeBody(BaseModel): job_id: str; language_hint: Optional[str] = None
class TranslateBody(BaseModel): text: str = Field(min_length=1, max_length=30000); target_language: str = "Burmese"
class VoiceBody(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    output_format: str = Field(default="mp3", pattern="^(mp3|wav)$")
    voice_id: Optional[str] = None

def need(value, name):
    if not value: raise HTTPException(503, f"{name} is not configured in Render Environment Variables")
def jd(job_id):
    if not job_id or "/" in job_id or ".." in job_id: raise HTTPException(400, "Invalid job_id")
    p = ROOT / job_id
    if not p.is_dir(): raise HTTPException(404, "Job not found; temporary job may have expired")
    return p
def gc():
    if GCS_JSON:
        info = json.loads(GCS_JSON)
        return storage.Client(project=info.get("project_id"), credentials=service_account.Credentials.from_service_account_info(info))
    return storage.Client()
def putgcs(path, key):
    if not BUCKET: return None
    gc().bucket(BUCKET).blob(key).upload_from_filename(str(path))
    return f"gs://{BUCKET}/{key}"
def cmd(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode: raise HTTPException(500, p.stderr[-1500:])
    return p.stdout.strip()

@app.get("/")
@app.get("/health")
def health():
    return {"status":"ok", "ffmpeg":bool(shutil.which("ffmpeg")), "gemini_configured":bool(GEMINI), "assemblyai_configured":bool(ASSEMBLYAI), "elevenlabs_configured":bool(ELEVENLABS), "gcs_configured":bool(BUCKET)}

@app.post("/api/video/upload")
async def upload(video: UploadFile = File(...)):
    ext = Path(video.filename or "video.mp4").suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}: raise HTTPException(400, "Unsupported video format")
    jid = uuid.uuid4().hex; d = ROOT / jid; d.mkdir(); target = d / ("source" + ext); size = 0
    try:
        with target.open("wb") as f:
            while chunk := await video.read(1024*1024):
                size += len(chunk)
                if size > MAX_MB*1024*1024: raise HTTPException(413, f"Video exceeds {MAX_MB} MB limit")
                f.write(chunk)
        dur = float(cmd(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(target)]))
        return {"job_id":jid,"filename":video.filename,"size_bytes":size,"duration_seconds":dur,"storage_uri":putgcs(target,f"{jid}/source{ext}")}
    except Exception:
        shutil.rmtree(d, ignore_errors=True); raise

@app.post("/api/video/{job_id}/split")
def split(job_id: str, body: SplitBody):
    d = jd(job_id); srcs = list(d.glob("source.*"))
    if not srcs: raise HTTPException(404, "Source video not found")
    src = srcs[0]; dur = float(cmd(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(src)]))
    seg = dur/body.sections; out=[]
    for i in range(body.sections):
        start=i*seg; length=dur-start if i==body.sections-1 else seg; dest=d/f"section_{i+1}.mp4"
        cmd(["ffmpeg","-y","-ss",str(start),"-i",str(src),"-t",str(length),"-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-movflags","+faststart",str(dest)])
        out.append({"section":i+1,"filename":dest.name,"start_seconds":start,"duration_seconds":length,"storage_uri":putgcs(dest,f"{job_id}/{dest.name}")})
    return {"job_id":job_id,"sections":out}

async def assemblyai_transcribe(mp3_path: Path, language_code: str):
    need(ASSEMBLYAI, "ASSEMBLYAI_API_KEY")
    headers={"authorization":ASSEMBLYAI}
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as c:
        with mp3_path.open("rb") as f:
            up=await c.post("https://api.assemblyai.com/v2/upload", headers={**headers,"content-type":"application/octet-stream"}, content=f.read())
        if not up.is_success: raise HTTPException(502, f"AssemblyAI upload failed: {up.text[:1000]}")
        audio_url=up.json().get("upload_url")
        payload={"audio_url":audio_url,"speech_model":"universal-2","language_code":language_code,"punctuate":True,"format_text":True}
        tr=await c.post("https://api.assemblyai.com/v2/transcript", headers=headers, json=payload)
        if not tr.is_success: raise HTTPException(502, f"AssemblyAI transcription request failed: {tr.text[:1000]}")
        tid=tr.json()["id"]
        for _ in range(300):
            await asyncio.sleep(3)
            r=await c.get(f"https://api.assemblyai.com/v2/transcript/{tid}",headers=headers)
            if not r.is_success: raise HTTPException(502, f"AssemblyAI status failed: {r.text[:1000]}")
            data=r.json()
            if data.get("status")=="completed": return data
            if data.get("status")=="error": raise HTTPException(502, f"AssemblyAI transcription error: {data.get('error')}")
        raise HTTPException(504,"AssemblyAI transcription timed out; retry this section")

@app.post("/api/video/transcribe")
async def transcribe(body: TranscribeBody):
    d=jd(body.job_id); paths=sorted(d.glob("section_*.mp4"))
    if not paths: raise HTTPException(400,"Split video first")
    language="my" if (body.language_hint or "").lower() in ("myanmar","burmese","မြန်မာ","my","auto") else ((body.language_hint or "my").lower())
    out=[]
    for i,p in enumerate(paths,1):
        audio=d/f"section_{i}.mp3"
        cmd(["ffmpeg","-y","-i",str(p),"-vn","-ac","1","-ar","16000","-b:a","64k",str(audio)])
        data=await assemblyai_transcribe(audio,language)
        words=data.get("words") or []
        lines=[]
        for w in words:
            start=max(0,int(w.get("start",0)//1000)); end=max(start+1,int((w.get("end",0)+999)//1000))
            lines.append({"text":w.get("text",""),"start_ms":w.get("start",0),"end_ms":w.get("end",0)})
        item={"section":i,"text":data.get("text", ""),"words":lines,"language_code":data.get("language_code")}
        out.append(item)
        (d/f"section_{i}.json").write_text(json.dumps(item,ensure_ascii=False),encoding="utf-8")
        putgcs(d/f"section_{i}.json",f"{body.job_id}/section_{i}.json")
    return {"job_id":body.job_id,"transcriptions":out,"copy_all_text":"\n\n".join(f"===== SECTION {x['section']} =====\n{x['text']}" for x in out)}

@app.post("/api/translate")
def translate(body: TranslateBody):
    need(GEMINI,"GEMINI_API_KEY")
    client=genai.Client(api_key=GEMINI)
    prompt=f"Translate the following text into natural, fluent {body.target_language}. Preserve meaning, names, dialogue tone and paragraph order. Do not summarize or add explanations. Return only the translation.\n\nTEXT:\n{body.text}"
    try:
        res=client.models.generate_content(model=MODEL,contents=prompt)
        return {"translated_text":res.text or ""}
    except Exception as e: raise HTTPException(502,f"Gemini translation failed: {e}")

@app.post("/api/voice/quote")
def voice_quote(body: VoiceBody):
    return {"provider":"ElevenLabs","model_id":ELEVENLABS_MODEL,"characters":len(body.text),"note":"Final cost depends on your ElevenLabs plan and model."}

@app.post("/api/voice/jobs")
async def create_voice(body: VoiceBody):
    need(ELEVENLABS,"ELEVENLABS_API_KEY")
    voice=body.voice_id or ELEVENLABS_VOICE_ID
    if not voice: raise HTTPException(400,"Provide voice_id or set ELEVENLABS_VOICE_ID in Render")
    params={"output_format":"mp3_44100_128"}
    payload={"text":body.text,"model_id":ELEVENLABS_MODEL,"language_code":"my","voice_settings":{"stability":0.5}}
    async with httpx.AsyncClient(timeout=180) as c:
        r=await c.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",params=params,headers={"xi-api-key":ELEVENLABS,"accept":"audio/mpeg","content-type":"application/json"},json=payload)
    if not r.is_success: raise HTTPException(502,f"ElevenLabs TTS failed: {r.text[:1200]}")
    jid=uuid.uuid4().hex; d=ROOT/jid; d.mkdir(exist_ok=True); ext=body.output_format
    mp3_path=d/"voice_source.mp3"; mp3_path.write_bytes(r.content)
    dest=d/f"voice.{ext}"
    if ext == "mp3":
        mp3_path.replace(dest)
    else:
        cmd(["ffmpeg","-y","-i",str(mp3_path),"-acodec","pcm_s16le","-ar","44100",str(dest)])
        mp3_path.unlink(missing_ok=True)
    uri=putgcs(dest,f"{jid}/voice.{ext}")
    meta={"job_id":jid,"status":"completed","filename":dest.name,"format":ext,"storage_uri":uri}
    (d/"voice.json").write_text(json.dumps(meta),encoding="utf-8")
    return meta

@app.get("/api/voice/jobs/{job_id}")
def voice_status(job_id: str):
    d=jd(job_id); meta=d/"voice.json"
    if not meta.exists(): raise HTTPException(404,"Voice job not found")
    return json.loads(meta.read_text(encoding="utf-8"))

@app.get("/api/voice/jobs/{job_id}/audio")
def voice_audio(job_id: str, format: str="mp3"):
    if format not in ("mp3","wav"): raise HTTPException(400,"format must be mp3 or wav")
    d=jd(job_id); path=d/f"voice.{format}"
    if path.exists(): data=path.read_bytes()
    elif BUCKET:
        blob=gc().bucket(BUCKET).blob(f"{job_id}/voice.{format}")
        if not blob.exists(): raise HTTPException(404,"Audio file not found")
        data=blob.download_as_bytes()
    else: raise HTTPException(404,"Audio file not found")
    return Response(data,media_type="audio/mpeg" if format=="mp3" else "audio/wav",headers={"Content-Disposition":f'attachment; filename="myanmar_voice_{job_id}.{format}"'})

@app.get("/api/storage/{job_id}/files")
def storage_files(job_id: str):
    need(BUCKET,"GCS_BUCKET")
    blobs=list(gc().list_blobs(BUCKET,prefix=f"{job_id}/"))
    return {"files":[{"name":b.name,"size":b.size,"gs_uri":f"gs://{BUCKET}/{b.name}"} for b in blobs]}
