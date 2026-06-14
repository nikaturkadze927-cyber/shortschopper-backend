import os
import subprocess
import json
import sqlite3
import requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
import jwt
import yt_dlp
from google import genai
import imageio_ffmpeg

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

SECRET_KEY = os.environ.get("SECRET_KEY", "SUPER_SECRET_KEY_FOR_STARTUP_2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

LEMON_API_KEY = os.environ.get("LEMON_API_KEY", "")
LEMON_STORE_ID = os.environ.get("LEMON_STORE_ID", "406800")
LEMON_VARIANT_ID = os.environ.get("LEMON_VARIANT_ID", "1787422")
YOUR_DOMAIN = os.environ.get("YOUR_DOMAIN", "http://127.0.0.1:5500")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.environ.setdefault("GEMINI_API_KEY", "")
ai_client = genai.Client()
DB_FILE = "users.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            tier TEXT DEFAULT 'free',
            used_today INTEGER DEFAULT 0,
            last_reset TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def get_password_hash(password):
    return pwd_context.hash(password)


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="სესია ამოიწურა")
        return email
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="სესია ამოიწურა")


class UserRegister(BaseModel):
    email: EmailStr
    password: str


class LinkRequest(BaseModel):
    youtube_url: str
    burn_subtitles: bool = True


@app.post("/register")
async def register(user: UserRegister):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM users WHERE email = ?", (user.email,))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="ეს მეილი უკვე დაკავებულია")
        cursor.execute(
            "INSERT INTO users (email, password, last_reset) VALUES (?, ?, ?)",
            (user.email, get_password_hash(user.password), datetime.now(timezone.utc).date().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "წარმატება"}


@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT password, tier FROM users WHERE email = ?", (form_data.username,))
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row or not verify_password(form_data.password, row[0]):
        raise HTTPException(status_code=400, detail="არასწორი მონაცემები")

    return {
        "access_token": create_access_token({"sub": form_data.username, "tier": row[1]}),
        "token_type": "bearer",
        "tier": row[1],
    }


@app.post("/create-checkout-session")
async def create_checkout_session(current_user: str = Depends(get_current_user)):
    if not LEMON_API_KEY:
        raise HTTPException(status_code=500, detail="Lemon Squeezy API key არ არის კონფიგურირებული")

    url = "https://api.lemonsqueezy.com/v1/checkouts"
    headers = {
        "Authorization": f"Bearer {LEMON_API_KEY}",
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {"email": current_user},
                "product_options": {
                    "redirect_url": f"{YOUR_DOMAIN}/index.html?payment=success&email={current_user}"
                },
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(LEMON_STORE_ID)}},
                "variant": {"data": {"type": "variants", "id": str(LEMON_VARIANT_ID)}},
            },
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        res_data = response.json()
        checkout_url = res_data["data"]["attributes"]["url"]
        return {"url": checkout_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lemon Squeezy შეცდომა: {str(e)}")


@app.post("/activate-premium")
async def activate_premium(email: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="მომხმარებელი ვერ მოიძებნა")
    cursor.execute("UPDATE users SET tier = 'premium' WHERE email = ?", (email,))
    conn.commit()
    conn.close()
    return {"status": "პრემიუმი გააქტიურებულია!"}


@app.post("/cut-shorts")
async def cut_shorts(request: LinkRequest, current_user: str = Depends(get_current_user)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT tier, used_today, last_reset FROM users WHERE email = ?", (current_user,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="მომხმარებელი ვერ მოიძებნა")

    tier, used_today, last_reset = row
    today_str = datetime.now(timezone.utc).date().isoformat()

    if last_reset != today_str:
        used_today = 0
        cursor.execute(
            "UPDATE users SET used_today = 0, last_reset = ? WHERE email = ?",
            (today_str, current_user),
        )
        conn.commit()

    if tier == 'free' and used_today >= 1:
        conn.close()
        raise HTTPException(status_code=403, detail="Free ლიმიტი (1 ვიდეო დღეში) ამოწურულია. იყიდე პრემიუმი!")

    url = request.youtube_url
    safe_id = "".join(c for c in current_user.split('@')[0] if c.isalnum() or c in ('-', '_')) or "user"

    audio_file = f"audio_{safe_id}.mp3"
    video_file = f"video_{safe_id}.mp4"
    trimmed_video = f"clip_{safe_id}.mp4"
    trimmed_audio = f"clip_a_{safe_id}.mp3"
    srt_file = f"subs_{safe_id}.srt"
    output_file = f"shorts_{safe_id}.mp4"

    for f in os.listdir('.'):
        if f.startswith((audio_file.rsplit('.', 1)[0], video_file.rsplit('.', 1)[0],
                         trimmed_video, trimmed_audio, srt_file, output_file)):
            try:
                os.remove(f)
            except OSError:
                pass

    downloaded_v = None
    try:
        PROXY_URL = os.environ.get("YTDLP_PROXY", "")

        ydl_common = {
            'ffmpeg_location': FFMPEG_PATH,
            'nocheckcertificate': True,
            'socket_timeout': 15,
            'retries': 3,
            'http_chunk_size': 1048576,
            'extractor_args': {
                'youtube': {
                    'player_client': ['tvhtml5'],
                    'player_skip': ['webpage', 'configs']
                }
            }
        }
        if PROXY_URL:
            ydl_common['proxy'] = PROXY_URL

        # 1. აუდიო
        audio_opts = dict(ydl_common)
        audio_opts.update({
            'format': 'bestaudio/best',
            'outtmpl': audio_file,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        })
        print(f"🚀 იწყება აუდიოს გადმოწერა: {url}")
        with yt_dlp.YoutubeDL(audio_opts) as ydl:
            ydl.download([url])

        # 2. ვიდეო
        video_opts = dict(ydl_common)
        video_opts.update({
            'format': 'bestvideo[height<=1080]/best[height<=1080]',
            'outtmpl': video_file,
        })
        print(f"🚀 იწყება ვიდეოს გადმოწერა: {url}")
        with yt_dlp.YoutubeDL(video_opts) as ydl:
            ydl.download([url])

        video_base = video_file.rsplit('.', 1)[0]
        for f in os.listdir('.'):
            if f.startswith(video_base) and not f.endswith('.mp3'):
                downloaded_v = f
                break

        if not downloaded_v or not os.path.exists(audio_file):
            raise RuntimeError("ფაილები ვერ შეიქმნა ჩამოტვირთვისას")

    except Exception as e:
        conn.close()
        print(f"❌ yt-dlp ჩამოტვირთვის შეცდომა: {str(e)}")
        for f in [audio_file, video_file, downloaded_v]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        raise HTTPException(status_code=400, detail=f"ვიდეო ვერ ჩამოიტვირთა: {str(e)}")

    try:
        print("ფაილი იგზავნება Gemini-სთან...")
        uploaded_file = ai_client.files.upload(file=audio_file)
        prompt = (
            "Listen to this audio. Find 1 epic/viral segment (30-45s duration).\n"
            "CRITICAL: DO NOT take the introduction/beginning.\n"
            "CRITICAL: Create short subtitles (2-3 words per line).\n"
            "Return JSON only with this exact schema:\n"
            '{"start_time": number, "end_time": number, '
            '"subtitles": [{"start": number, "end": number, "text": "string"}]}'
        )
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[uploaded_file, prompt],
            config={"response_mime_type": "application/json"},
        )
        data = json.loads(response.text)
        start, end = float(data["start_time"]), float(data["end_time"])
        duration = end - start

        if duration <= 0:
            raise ValueError("AI-მ დააბრუნა არასწორი დროის მონაკვეთი")

        if request.burn_subtitles and "subtitles" in data and data["subtitles"]:
            def to_srt(secs):
                secs = max(0.0, secs)
                ms = int(round((secs - int(secs)) * 1000))
                return f"{int(secs // 3600):02d}:{int((secs % 3600) // 60):02d}:{int(secs % 60):02d},{ms:03d}"

            with open(srt_file, "w", encoding="utf-8") as f:
                for idx, sub in enumerate(data["subtitles"], start=1):
                    rel_s = max(0.0, sub["start"] - start)
                    rel_e = max(rel_s + 0.1, sub["end"] - start)
                    rel_e = min(rel_e, duration)
                    f.write(f"{idx}\n{to_srt(rel_s)} --> {to_srt(rel_e)}\n{sub['text'].strip()}\n\n")
        else:
            request.burn_subtitles = False

    except Exception as e:
        conn.close()
        print(f"❌ Gemini AI შეცდომა: {str(e)}")
        for f in [audio_file, downloaded_v]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        raise HTTPException(status_code=500, detail=f"AI დამუშავების შეცდომა: {str(e)}")

    try:
        ffmpeg_exe = FFMPEG_PATH
        print("იწყება FFmpeg ვიდეოს დამუშავება...")

        subprocess.run(
            [ffmpeg_exe, "-y", "-ss", str(start), "-i", downloaded_v, "-t", str(duration),
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-an", trimmed_video],
            check=True, capture_output=True,
        )
        subprocess.run(
            [ffmpeg_exe, "-y", "-ss", str(start), "-i", audio_file, "-t", str(duration),
             "-acodec", "libmp3lame", trimmed_audio],
            check=True, capture_output=True,
        )

        if request.burn_subtitles and os.path.exists(srt_file):
            abs_srt = os.path.abspath(srt_file).replace("\\", "/").replace(":", "\\:")
            v_filter = (
                f"crop=ih*9/16:ih,subtitles='{abs_srt}':force_style="
                "'FontName=Arial,FontSize=24,PrimaryColour=&HFFFFFF,"
                "OutlineColour=&H000000,Outline=4,Shadow=2,BorderStyle=1,Alignment=2,MarginV=120'"
            )
        else:
            v_filter = "crop=ih*9/16:ih"

        subprocess.run(
            [ffmpeg_exe, "-y", "-i", trimmed_video, "-i", trimmed_audio,
             "-vf", v_filter, "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "libx264", "-crf", "22", "-preset", "ultrafast",
             "-c:a", "aac", "-pix_fmt", "yuv420p", "-shortest", output_file],
            check=True, capture_output=True,
        )

        if not os.path.exists(output_file):
            raise RuntimeError("FFmpeg-მა ფაილი ვერ შექმნა")

    except subprocess.CalledProcessError as e:
        conn.close()
        err_msg = e.stderr.decode(errors="ignore")[-500:] if e.stderr else str(e)
        print(f"❌ FFmpeg ბრძანების შეცდომა: {err_msg}")
        for f in [audio_file, downloaded_v, trimmed_video, trimmed_audio, srt_file]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        raise HTTPException(status_code=500, detail=f"FFmpeg ჩავარდა: {err_msg}")
    except Exception as e:
        conn.close()
        print(f"❌ FFmpeg შეცდომა: {str(e)}")
        for f in [audio_file, downloaded_v, trimmed_video, trimmed_audio, srt_file]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        raise HTTPException(status_code=500, detail=f"FFmpeg ჩავარდა: {str(e)}")

    cursor.execute("UPDATE users SET used_today = used_today + 1 WHERE email = ?", (current_user,))
    conn.commit()
    conn.close()

    for f in [audio_file, downloaded_v, trimmed_video, trimmed_audio, srt_file]:
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    print("🎉 ვიდეო წარმატებით მომზადდა და იგზავნება მომხმარებელთან!")
    return FileResponse(path=output_file, filename="epic_shorts.mp4", media_type="video/mp4")
