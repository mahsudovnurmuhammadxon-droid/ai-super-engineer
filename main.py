import os
import io
import json
import base64
import asyncio
import contextlib
import traceback
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
from psycopg2.extras import RealDictCursor
import litellm
from duckduckgo_search import DDGS
import pypdf
import docx
import openpyxl

app = FastAPI(title="Super-Agent AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

if GEMINI_API_KEY:
    os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
if OPENROUTER_API_KEY:
    os.environ["OPENROUTER_API_KEY"] = OPENROUTER_API_KEY

MODELS = [
    "gemini/gemini-2.5-flash",
    "gemini/gemini-2.0-flash",
    "openrouter/google/gemma-3-27b-it:free",
    "openrouter/openrouter/free"
]

SYSTEM_PROMPT = """Sen «Super-Agent» (Shaxsiy Super-Ong) — foydalanuvchining 24/7 ishlaydigan aqlli shaxsiy AI yordamchisisiz.
Asosiy vazifalaring:
1. Foydalanuvchi bilan tabiiy, ravon va samimiy o'zbek tilida muloqot qilish.
2. Logistika (yuk tashish, marshrutlar, hisob-kitoblar, jadvallar, hujjatlar), dasturlash, tahlil va kundalik rejalashtirishda professional yordam berish.
3. Agar foydalanuvchi hisob-kitob, ma'lumot tahlili yoki kod yozishni so'rasa, aniq va tushunarli yechim taqdim etish.
4. Qisqa, lo'nda, foydali va amaliy javob berish."""


def get_db_connection():
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        print("DB ulanish xatosi:", e)
        return None


def init_db():
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id SERIAL PRIMARY KEY,
                    role VARCHAR(20) NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS long_term_memory (
                    id SERIAL PRIMARY KEY,
                    fact TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        conn.commit()
    except Exception as e:
        print("DB jadval yaratish xatosi:", e)
    finally:
        conn.close()


init_db()


def save_message(role: str, content: str):
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_history (role, content) VALUES (%s, %s)",
                (role, content)
            )
        conn.commit()
    except Exception as e:
        print("Xabar saqlash xatosi:", e)
    finally:
        conn.close()


def get_recent_messages(limit: int = 16):
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, content FROM chat_history ORDER BY id DESC LIMIT %s",
                (limit,)
            )
            rows = cur.fetchall()
            return list(reversed(rows))
    except Exception as e:
        print("Tarixni o'qish xatosi:", e)
        return []
    finally:
        conn.close()


def web_search(query: str, max_results: int = 4) -> str:
    try:
        results_text = []
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            for r in results:
                title = r.get("title", "")
                body = r.get("body", "")
                href = r.get("href", "")
                results_text.append(f"- {title}: {body} ({href})")
        return "\n".join(results_text) if results_text else ""
    except Exception as e:
        print("Qidiruv xatosi:", e)
        return ""


def parse_uploaded_file(filename: str, file_bytes: bytes) -> dict:
    ext = filename.lower().split(".")[-1] if "." in filename else ""
    if ext in ["png", "jpg", "jpeg", "webp"]:
        b64 = base64.b64encode(file_bytes).decode("utf-8")
        mime = "image/png" if ext == "png" else "image/jpeg"
        return {"type": "image", "data": f"data:{mime};base64,{b64}", "name": filename}
    
    extracted_text = ""
    try:
        if ext == "pdf":
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            for page in reader.pages:
                extracted_text += (page.extract_text() or "") + "\n"
        elif ext in ["docx", "doc"]:
            doc = docx.Document(io.BytesIO(file_bytes))
            extracted_text = "\n".join([p.text for p in doc.paragraphs])
        elif ext in ["xlsx", "xls"]:
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                extracted_text += f"\n--- Varaq: {sheet} ---\n"
                for row in ws.iter_rows(values_only=True):
                    row_vals = [str(cell) if cell is not None else "" for cell in row]
                    if any(row_vals):
                        extracted_text += " | ".join(row_vals) + "\n"
        else:
            extracted_text = file_bytes.decode("utf-8", errors="ignore")
    except Exception as e:
        extracted_text = f"Faylni o'qishda xatolik: {str(e)}"
        
    return {"type": "text", "data": extracted_text[:15000], "name": filename}


@app.get("/api/history")
async def api_history():
    return {"messages": get_recent_messages(30)}


@app.post("/api/clear")
async def api_clear():
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chat_history")
            conn.commit()
        finally:
            conn.close()
    return {"status": "ok"}


@app.post("/api/run_python")
async def api_run_python(code: str = Form(...)):
    output_buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(output_buffer):
            exec_globals = {}
            exec(code, exec_globals)
        result = output_buffer.getvalue()
        return {"status": "ok", "output": result if result else "Kod muvaffaqiyatli bajarildi (natija chop etilmadi)."}
    except Exception:
        return {"status": "error", "output": traceback.format_exc()}


@app.post("/api/chat")
async def api_chat(
    message: str = Form(""),
    use_search: str = Form("false"),
    file: Optional[UploadFile] = File(None)
):
    file_info = None
    if file and file.filename:
        raw_bytes = await file.read()
        file_info = parse_uploaded_file(file.filename, raw_bytes)

    search_context = ""
    if use_search.lower() == "true" and message.strip():
        search_res = web_search(message)
        if search_res:
            search_context = f"\n\n[Internet qidiruv natijalari]:\n{search_res}"

    user_display_text = message.strip()
    if file_info:
        user_display_text += f" [Fayl: {file_info['name']}]"

    if user_display_text:
        save_message("user", user_display_text)

    history = get_recent_messages(12)
    messages_payload = [{"role": "system", "content": SYSTEM_PROMPT}]

    for msg in history[:-1]:
        messages_payload.append({"role": msg["role"], "content": msg["content"]})

    final_prompt = message.strip() + search_context
    if file_info:
        if file_info["type"] == "text":
            final_prompt += f"\n\n[Yuklangan fayl ({file_info['name']}) mazmuni]:\n{file_info['data']}"
            messages_payload.append({"role": "user", "content": final_prompt})
        elif file_info["type"] == "image":
            messages_payload.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": final_prompt or "Ushbu rasmni tahlil qilib ber."},
                    {"type": "image_url", "image_url": {"url": file_info["data"]}}
                ]
            })
    else:
        messages_payload.append({"role": "user", "content": final_prompt})

    async def response_generator():
        full_reply = ""
        success = False

        for model_name in MODELS:
            try:
                response = await litellm.acompletion(
                    model=model_name,
                    messages=messages_payload,
                    stream=True,
                    temperature=0.7
                )
                async for chunk in response:
                    if hasattr(chunk, "choices") and len(chunk.choices) > 0:
                        delta = chunk.choices[0].delta
                        content = getattr(delta, "content", None)
                        if content:
                            full_reply += content
                            yield f"data: {json.dumps({'text': content})}\n\n"
                success = True
                break
            except Exception as e:
                print(f"Model {model_name} xatosi:", str(e))
                continue

        if not success:
            err_msg = "Kechirasiz, barcha AI modellarida vaqtinchalik uzilish yuz berdi. API kalitlarni tekshiring."
            full_reply = err_msg
            yield f"data: {json.dumps({'text': err_msg})}\n\n"

        if full_reply.strip():
            save_message("assistant", full_reply)
        yield "data: [DONE]\n\n"

    return StreamingResponse(response_generator(), media_type="text/event-stream")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
