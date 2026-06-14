import os
import subprocess
import json
import sqlite3
import requests  # <-- Stripe-ის ნაცვლად ახლა requests-ს ვიყენებთ
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
import jwt
import yt_dlp
from google import genai

SECRET_KEY = "SUPER_SECRET_KEY_FOR_STARTUP_2026"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

# ⚠️ ჩასვი შენი Lemon Squeezy-ს მონაცემები (სატესტო რეჟიმშიც მუშაობს)
LEMON_API_KEY = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiI5NGQ1OWNlZi1kYmI4LTRlYTUtYjE3OC1kMjU0MGZjZDY5MTkiLCJqdGkiOiI3OTM3YzZmNWJhNWYwYjg4YTA5Zjg5OTY4OGFjY2ZlOTU1MjNjZWZjODY4ZTU2ZTE4YzQyZDkwZjFiMmQyNmIwZjgwZTMwZmVkZmIxNTU2MyIsImlhdCI6MTc4MTQwMDgxNi4wNjQyMDEsIm5iZiI6MTc4MTQwMDgxNi4wNjQyMDQsImV4cCI6MTc5NzIwNjQwMC4wMjc2NTUsInN1YiI6IjczODc1NTciLCJzY29wZXMiOltdfQ.wSXuRLP9G1dE9xfjESfiP7z_mobhHy6YnCDy0MU5ZKxd9MDyfy_hspuAPIlcNKRjjWdxSf-rEn-z6XFObbvR3eTnnNztOpgtAJkTabIWLC6TQPMOh_izHRpyQtGVfjcWJK3aClAeyYPxN2rhZgjjFMJ01bG_DNXAFgMjPUJlc4azRdy4vhit2e6BXJyEN2d9NKiugvnCdUJpWRhenl45oz2fkDVZGdTeMHK4InqZiCJGn4U0T5O5HV-bDXtxv6n9AEJ4k-PFFmOPdoAIFMGbAjodUg1HnlVZXuoOnyfF6N0exYYE5SzSDaT440K0RcVZCvkAQUX9yZNeuQdopdJzh-F-YcFjfFRHULS0LQq7Zx-AYACHW_IWETuamoTCei7Sc7jLHbHECXlTFVeiX7gUIG7oY6pqyZ2xcAhoE0veyH89bD_73_yX-RPLXWVl0g4tuwL14oQA6LiYP88q-v9ukG4Z5wnsaH11e-zTMJuUM0dH8fRE_35OT_hevcuk2dfONcpIN7_S-ljlUsJfmhl8dUJw4iKl-6gEstqcI9BbW5WPrToNXABJ7wUrbubFXHfCvPkne9DJfLMV-PkppaVoUeQptW2mib4w4Ni7rk1AYuRLrxarFGZAHWaWwXSFQ7DBDRpv5z1WpHHFYelrB-zUlBckae660oQcImFjQQ1wse8"
LEMON_VARIANT_ID = "1787422" # პროდუქტის/ტარიფის ID Lemon Squeezy-დან
YOUR_DOMAIN = "http://127.0.0.1:5500"

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

os.environ["GEMINI_API_KEY"] = "AQ.Ab8RN6KdTjZUua66bWpXb3hAi0qhbX20VaUJ5YGkulOcgJn5Mg"
FFMPEG_DIR = r"C:\ffmpeg-master-latest-win64-gpl-shared\bin"
os.environ["PATH"] += os.pathsep + FFMPEG_DIR
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
            used_today INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_password_hash(password): return pwd_context.hash(password)
def verify_password(plain_password, hashed_password): return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="სესია ამოიწურა")

class UserRegister(BaseModel):
    email: EmailStr
    password: str

class LinkRequest(BaseModel):
    youtube_url: str
    burn_subtitles: bool = True

# --- ენდპოინტები ---

@app.post("/register")
async def register(user: UserRegister):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email = ?", (user.email,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="ეს მეილი უკვე დაკავებულია")
    cursor.execute("INSERT INTO users (email, password) VALUES (?, ?)", (user.email, get_password_hash(user.password)))
    conn.commit()
    conn.close()
    return {"status": "წარმატება"}

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT password, tier FROM users WHERE email = ?", (form_data.username,))
    row = cursor.fetchone()
    conn.close()
    if not row or not verify_password(form_data.password, row[0]):
        raise HTTPException(status_code=400, detail="არასწორი მონაცემები")
    return {"access_token": create_access_token({"sub": form_data.username, "tier": row[1]}), "token_type": "bearer", "tier": row[1]}


# 🍋 Lemon Squeezy Checkout ლინკის გენერაცია
@app.post("/create-checkout-session")
async def create_checkout_session(current_user: str = Depends(get_current_user)):
    url = "https://api.lemonsqueezy.com/v1/checkouts"
    headers = {
        "Authorization": f"Bearer {LEMON_API_KEY}",
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json"
    }
    
    # Lemon Squeezy-ს სპეციფიკური JSON სტრუქტურა
    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {
                    "email": current_user  # იუზერის მეილი ავტომატურად რომ ჩაეწეროს გადახდისას
                },
                "product_options": {
                    "redirect_url": f"{YOUR_DOMAIN}/index.html?payment=success&email={current_user}"
                }
            },
            "relationships": {
                "store": {
                    "data": {
                        "type": "stores",
                        "id": "406800"  # Lemon Squeezy-ს მაღაზიის ID
                    }
                },
                "variant": {
                    "data": {
                        "type": "variants",
                        "id": str(LEMON_VARIANT_ID)
                    }
                }
            }
        }
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        res_data = response.json()
        checkout_url = res_data["data"]["attributes"]["url"]
        return {"url": checkout_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lemon Squeezy შეცდომა: {str(e)}")


@app.post("/activate-premium")
async def activate_premium(email: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET tier = 'premium' WHERE email = ?", (email,))
    conn.commit()
    conn.close()
    return {"status": "პრემიუმი გააქტიურებულია!"}


@app.post("/cut-shorts")
async def cut_shorts(request: LinkRequest, current_user: str = Depends(get_current_user)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT tier, used_today FROM users WHERE email = ?", (current_user,))
    tier, used_today = cursor.fetchone()
    
    if tier == 'free' and used_today >= 1:
        conn.close()
        raise HTTPException(status_code=403, detail="Free ლიმიტი (1 ვიდეო დღეში) ამოწურულია. იყიდე პრემიუმი!")
    
    url = request.youtube_url
    audio_file = f"audio_{current_user.split('@')[0]}.mp3"
    video_file = f"video_{current_user.split('@')[0]}.mp4"
    trimmed_video = f"clip_{current_user.split('@')[0]}.mp4"
    trimmed_audio = f"clip_a_{current_user.split('@')[0]}.mp3"
    srt_file = f"subs_{current_user.split('@')[0]}.srt"
    output_file = f"shorts_{current_user.split('@')[0]}.mp4"
    
    for f in [audio_file, video_file, trimmed_video, trimmed_audio, srt_file, output_file]:
        if os.path.exists(f): 
            try: os.remove(f)
            except: pass
            
    try:
        with yt_dlp.YoutubeDL({'format': 'bestaudio', 'outtmpl': audio_file, 'ffmpeg_location': FFMPEG_DIR, 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}]}) as ydl:
            ydl.download([url])
        with yt_dlp.YoutubeDL({'format': 'bestvideo[height=1080]', 'outtmpl': video_file, 'ffmpeg_location': FFMPEG_DIR}) as ydl:
            ydl.download([url])
            
        downloaded_v = None
        for f in os.listdir('.'):
            if f.startswith(video_file.split('.')[0]) and not f.endswith('.mp3'):
                downloaded_v = f
                break
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail="ვიდეო ვერ ჩამოიტვირთა")

    try:
        uploaded_file = ai_client.files.upload(file=audio_file)
        prompt = (
            "Listen to this audio. Find 1 epic/viral segment (30-45s duration).\n"
            "⚠️ CRITICAL: DO NOT take the introduction/beginning.\n"
            "⚠️ CRITICAL: Create short subtitles (2-3 words per line).\n"
            "Return JSON only:\n"
            "{'start_time': X, 'end_time': Y, 'subtitles': [{'start': S, 'end': E, 'text': 'T'}]}"
        )
        response = ai_client.models.generate_content(model="gemini-2.5-flash", contents=[uploaded_file, prompt], config={"response_mime_type": "application/json"})
        data = json.loads(response.text)
        start, end = float(data["start_time"]), float(data["end_time"])
        duration = end - start
        
        if request.burn_subtitles and "subtitles" in data:
            def to_srt(secs):
                return f"{int(secs//3600):02d}:{int((secs%3600)//60):02d}:{int(secs%60):02d},{int((secs-int(secs))*1000):03d}"
            with open(srt_file, "w", encoding="utf-8") as f:
                for idx, sub in enumerate(data["subtitles"], start=1):
                    rel_s = max(0.0, sub["start"] - start)
                    rel_e = sub["end"] - start
                    f.write(f"{idx}\n{to_srt(rel_s)} --> {to_srt(rel_e)}\n{sub['text'].strip()}\n\n")
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail="AI ჩავარდა")

    try:
        ffmpeg_exe = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
        subprocess.run([ffmpeg_exe, "-y", "-ss", str(start), "-i", downloaded_v, "-t", str(duration), "-c", "copy", trimmed_video], check=True)
        subprocess.run([ffmpeg_exe, "-y", "-ss", str(start), "-i", audio_file, "-t", str(duration), "-acodec", "libmp3lame", trimmed_audio], check=True)
        
        abs_srt = os.path.abspath(srt_file).replace("\\", "/").replace(":", "\\:")
        v_filter = f"crop=ih*9/16:ih,subtitles='{abs_srt}':force_style='FontName=Segoe UI Black,FontSize=24,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=4,Shadow=2,BorderStyle=1,Alignment=2,MarginV=120'" if request.burn_subtitles else "crop=ih*9/16:ih"
        
        subprocess.run([ffmpeg_exe, "-y", "-i", trimmed_video, "-i", trimmed_audio, "-vf", v_filter, "-c:v", "libx264", "-crf", "22", "-preset", "ultrafast", "-c:a", "aac", "-pix_fmt", "yuv420p", output_file], check=True)
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail="FFmpeg ჩავარდა")

    cursor.execute("UPDATE users SET used_today = used_today + 1 WHERE email = ?", (current_user,))
    conn.commit()
    conn.close()

    for f in [audio_file, downloaded_v, trimmed_video, trimmed_audio, srt_file]:
        if os.path.exists(f): os.remove(f)

    return FileResponse(path=output_file, filename="epic_shorts.mp4", media_type="video/mp4")