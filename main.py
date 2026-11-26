import io
import json
import os
import re
import subprocess
import tempfile
import traceback
from typing import Any, AsyncGenerator, Dict, List, Optional

import docx
import litellm
import openpyxl
import psycopg2
from psycopg2.extras import RealDictCursor
from duckduckgo_search import DDGS
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pypdf import PdfReader
import uvicorn

app = FastAPI(title="Super-Agent")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

SYSTEM_PROMPT = (
    "Sen aqlli, samimiy va tabiiy o'zbek tilida gaplashadigan shaxsiy yordamchi — 'Super-Agent'san. "
    "O'zingni 'Men' deb tanishtir va robotona shablonlardan qoch. "
    "Javoblarni **qalin matn** va tartibli/tartibsiz ro'yxatlar bilan tiniq, tushunarli va foydali qilib tuz. "
    "Rasmlardagi ko'rinmaydigan yoki xira tafsilotlarni hech qachon o'ylab topma (gallyutsinatsiya qilma); faqat aniq ko'rinadigan narsalarni ayt. "
    "Agar foydalanuvchi hisoblash yoki kod yozishni so'rasa va Python ishlatish foydali bo'lsa, kodni [PY]...[/PY] blokida yozishing mumkin."
)

SEARCH_KEYWORDS = {
    "bugun",
    "hozir",
    "narx",
    "kurs",
    "dollar",
    "ob-havo",
    "ob havo",
    "yangilik",
    "yangiliklar",
    "2025",
    "2026",
    "natija",
    "natijalar",
    "sayt",
}


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db() -> None:
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mind_chat (
                        id SERIAL PRIMARY KEY,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL
                    )
                    """
                )
        conn.close()
    except Exception as exc:
        print(f"DB init error: {exc}")


def save_message(role: str, content: str) -> None:
    if not DATABASE_URL or not content.strip():
        return
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mind_chat (role, content) VALUES (%s, %s)",
                    (role, content),
                )
        conn.close()
    except Exception as exc:
        print(f"DB save error: {exc}")


def fetch_recent_history(limit: int = 30) -> List[Dict[str, str]]:
    if not DATABASE_URL:
        return []
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, role, content FROM mind_chat ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        conn.close()
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as exc:
        print(f"DB fetch error: {exc}")
        return []


def clear_history() -> None:
    if not DATABASE_URL:
        return
    conn = get_db_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mind_chat")
    conn.close()


def retrieve_rag_memory(query: str, max_items: int = 5) -> str:
    if not DATABASE_URL or not query.strip():
        return ""
    tokens = [
        w.lower()
        for w in re.findall(r"[a-zA-Zа-яА-ЯёЁўғқҳЎҒҚҲ0-9_-]{3,}", query)
    ]
    if not tokens:
        return ""
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 200"
                )
                rows = cur.fetchall()
        conn.close()

        scored: List[tuple[int, str, str]] = []
        for row in rows:
            text = row["content"] or ""
            low = text.lower()
            score = sum(1 for tok in set(tokens) if tok in low)
            if score > 0:
                snippet = text.strip().replace("\n", " ")
                if len(snippet) > 350:
                    snippet = snippet[:350] + "..."
                scored.append((score, row["role"], snippet))

        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[:max_items]
        if not top:
            return ""
        lines = ["Uzoq muddatli xotiradan tegishli eski suhbatlar (RAG):"]
        for _, role, snippet in top:
            who = "Foydalanuvchi" if role == "user" else "Super-Agent"
            lines.append(f"- [{who}]: {snippet}")
        return "\n".join(lines)
    except Exception as exc:
        print(f"RAG retrieval error: {exc}")
        return ""


def should_search_web(query: str) -> bool:
    low = query.lower()
    for kw in SEARCH_KEYWORDS:
        if kw in low:
            return True
    return False


def perform_web_search(query: str, max_results: int = 4) -> str:
    try:
        items: List[Dict[str, Any]] = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                items.append(r)
                if len(items) >= max_results:
                    break
        if not items:
            return ""
        formatted = ["Jonli veb-qidiruv natijalari:"]
        for idx, item in enumerate(items, start=1):
            title = item.get("title", "")
            href = item.get("href", "")
            body = item.get("body", "")
            formatted.append(f"{idx}. **{title}** ({href})\n   {body}")
        return "\n".join(formatted)
    except Exception as exc:
        print(f"Web search error: {exc}")
        return ""


async def extract_uploaded_file_text(upload_file: Optional[UploadFile]) -> str:
    if upload_file is None or not upload_file.filename:
        return ""
    filename = upload_file.filename
    ext = os.path.splitext(filename)[1].lower()
    raw_bytes = await upload_file.read()
    if not raw_bytes:
        return ""

    extracted = ""
    try:
        if ext == ".pdf":
            reader = PdfReader(io.BytesIO(raw_bytes))
            parts: List[str] = []
            for i, page in enumerate(reader.pages):
                txt = page.extract_text() or ""
                if txt.strip():
                    parts.append(f"--- Sahifa {i + 1} ---\n{txt}")
            extracted = "\n".join(parts)
        elif ext == ".docx":
            doc = docx.Document(io.BytesIO(raw_bytes))
            parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    row_vals = [c.text.strip() for c in row.cells if c.text.strip()]
                    if row_vals:
                        parts.append(" | ".join(row_vals))
            extracted = "\n".join(parts)
        elif ext in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
            parts = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                parts.append(f"--- Varag'a: {sheet_name} ---")
                for row in ws.iter_rows(values_only=True):
                    if any(cell is not None for cell in row):
                        row_str = " | ".join(
                            "" if cell is None else str(cell) for cell in row
                        )
                        parts.append(row_str)
            extracted = "\n".join(parts)
        else:
            extracted = raw_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        extracted = f"[Faylni o'qishda xatolik ({filename}): {exc}]"

    if len(extracted) > 12000:
        extracted = extracted[:12000] + "\n...[Matn 12,000 belgida qisqartirildi]..."

    return f"\n\n[Yuklangan fayl: {filename}]\n{extracted}"


def run_python_code(code: str) -> str:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(code)
            tmp_path = tmp.name

        proc = subprocess.run(
            ["python3", tmp_path],
            capture_output=True,
            text=True,
            timeout=6,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        out_parts = []
        if stdout:
            out_parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            out_parts.append(f"STDERR:\n{stderr}")
        if not out_parts:
            out_parts.append("(Kod muvaffaqiyatli bajarildi, chiqish bo'sh)")
        return "\n".join(out_parts)
    except subprocess.TimeoutExpired:
        return "STDERR: Vaqt tugadi (6 soniyalik cheklov oshib ketdi)."
    except Exception as exc:
        return f"STDERR: Bajarish xatosi: {exc}\n{traceback.format_exc()}"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def build_messages_payload(
    user_message: str,
    image_data_url: str,
    file_context: str,
    rag_context: str,
    web_context: str,
    history: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    extra_blocks: List[str] = []
    if rag_context:
        extra_blocks.append(rag_context)
    if web_context:
        extra_blocks.append(web_context)
    if extra_blocks:
        messages.append({"role": "system", "content": "\n\n".join(extra_blocks)})

    for past in history[-16:]:
        role = past.get("role", "user")
        content = past.get("content", "")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})

    combined_text = (user_message or "").strip()
    if file_context:
        combined_text = (combined_text + file_context).strip()
    if not combined_text and image_data_url:
        combined_text = "Ushbu rasmni tahlil qilib, nimalar ko'rinayotganini aytib ber."

    if image_data_url and image_data_url.startswith("data:image/"):
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": combined_text},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        )
    else:
        messages.append({"role": "user", "content": combined_text})

    return messages


async def stream_llm_with_fallbacks(
    messages: List[Dict[str, Any]],
) -> AsyncGenerator[str, None]:
    attempts = [
        ("gemini/gemini-2.5-flash", GEMINI_API_KEY),
        ("gemini/gemini-2.0-flash", GEMINI_API_KEY),
        ("openrouter/openrouter/free", OPENROUTER_API_KEY),
        ("openrouter/google/gemma-3-27b-it:free", OPENROUTER_API_KEY),
    ]

    last_error: Optional[Exception] = None

    for model_name, api_key in attempts:
        if not api_key:
            continue
        try:
            response_stream = await litellm.acompletion(
                model=model_name,
                messages=messages,
                api_key=api_key,
                stream=True,
            )
            emitted_any = False
            async for chunk in response_stream:
                delta = ""
                try:
                    delta = chunk.choices[0].delta.content or ""
                except Exception:
                    delta = ""
                if delta:
                    emitted_any = True
                    yield delta
            if emitted_any:
                return
        except Exception as exc:
            last_error = exc
            print(f"Model {model_name} failed: {exc}")
            continue

    raise RuntimeError(
        f"Barcha modellar muvaffaqiyatsiz tugadi. Oxirgi xato: {last_error}"
    )


async def explain_python_output(
    original_prompt: str, code_block: str, exec_output: str
) -> str:
    api_key = GEMINI_API_KEY or OPENROUTER_API_KEY
    model = "gemini/gemini-2.5-flash" if GEMINI_API_KEY else "openrouter/openrouter/free"
    if not api_key:
        return f"\n\n**Kod natijasi:**\n```\n{exec_output}\n```"

    prompt_msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Foydalanuvchi so'rovi: {original_prompt}\n\n"
                f"Bajarilgan Python kodi:\n```python\n{code_block}\n```\n\n"
                f"Dastur natijasi:\n{exec_output}\n\n"
                "Ushbu natijani o'zbek tilida qisqa, aniq va tushunarli qilib tushuntirib ber."
            ),
        },
    ]
    try:
        r2 = await litellm.acompletion(
            model=model,
            messages=prompt_msgs,
            api_key=api_key,
            stream=False,
        )
        explanation = r2.choices[0].message.content or ""
        return (
            f"\n\n**Python kod natijasi:**\n```\n{exec_output}\n```\n\n"
            f"**Tushuntirish:**\n{explanation}"
        )
    except Exception as exc:
        return (
            f"\n\n**Python kod natijasi:**\n```\n{exec_output}\n```\n\n"
            f"*(Tushuntirish olishda xatolik: {exc})*"
        )


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/api/history")
def api_history():
    try:
        messages = fetch_recent_history(limit=30)
        return {"messages": messages}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/clear")
def api_clear():
    try:
        clear_history()
        return {"status": "cleared"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/chat")
async def api_chat(
    message: str = Form(""),
    image: str = Form(""),
    file: Optional[UploadFile] = File(None),
):
    clean_message = (message or "").strip()
    clean_image = (image or "").strip()
    file_context = await extract_uploaded_file_text(file)

    if not clean_message and not clean_image and not file_context:
        raise HTTPException(status_code=400, detail="Bo'sh xabar yuborib bo'lmaydi.")

    user_stored_text = clean_message
    if file and file.filename:
        user_stored_text = (
            f"{user_stored_text}\n[Fayl: {file.filename}]".strip()
        )
    if clean_image:
        user_stored_text = f"{user_stored_text}\n[Rasm biriktirildi]".strip()

    save_message("user", user_stored_text)

    rag_context = retrieve_rag_memory(clean_message or file_context)
    web_context = (
        perform_web_search(clean_message) if should_search_web(clean_message) else ""
    )
    history = fetch_recent_history(limit=30)

    messages_payload = build_messages_payload(
        user_message=clean_message,
        image_data_url=clean_image,
        file_context=file_context,
        rag_context=rag_context,
        web_context=web_context,
        history=history,
    )

    async def event_generator() -> AsyncGenerator[str, None]:
        full_assistant_text = ""
        try:
            async for delta in stream_llm_with_fallbacks(messages_payload):
                full_assistant_text += delta
                payload = json.dumps({"t": delta}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

            py_matches = re.findall(r"\[PY\](.*?)\[/PY\]", full_assistant_text, flags=re.DOTALL)
            if py_matches:
                code_to_run = py_matches[0].strip()
                exec_result = run_python_code(code_to_run)
                appended_section = await explain_python_output(
                    clean_message or "Python kodni bajarish",
                    code_to_run,
                    exec_result,
                )
                full_assistant_text += appended_section
                payload = json.dumps({"t": appended_section}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

            save_message("assistant", full_assistant_text)
            yield "data: [DONE]\n\n"
        except Exception as exc:
            err_msg = f"\n\n**Xatolik yuz berdi:** {exc}"
            full_assistant_text += err_msg
            save_message("assistant", full_assistant_text)
            payload = json.dumps({"t": err_msg}, ensure_ascii=False)
            yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>Super-Agent</title>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    :root {
      --bg-surface: #0b0f17;
      --bg-elevated: rgba(24, 30, 44, 0.78);
      --bg-card: rgba(20, 27, 42, 0.72);
      --border-subtle: rgba(148, 163, 184, 0.16);
      --text-primary: #f1f5f9;
      --text-secondary: #94a3b8;
      --accent-blue: #4285f4;
      --accent-purple: #9b72cb;
      --accent-rose: #d96570;
      --accent-gold: #f4b400;
      --danger: #ef4444;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }

    html, body {
      height: 100%;
      width: 100%;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Helvetica, Arial, sans-serif;
      color: var(--text-primary);
      background: radial-gradient(circle at 50% 100%, #0f1d3a 0%, #05070c 55%, #000 100%);
    }

    .app-shell {
      display: flex;
      flex-direction: column;
      height: 100dvh;
      width: 100%;
      max-width: 900px;
      margin: 0 auto;
      position: relative;
    }

    /* Top Header */
    .top-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px;
      background: rgba(5, 8, 16, 0.65);
      backdrop-filter: blur(14px);
      border-bottom: 1px solid var(--border-subtle);
      z-index: 20;
      flex-shrink: 0;
    }

    .header-left {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .icon-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-primary);
      width: 38px;
      height: 38px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 18px;
      transition: background 0.18s ease, border-color 0.18s ease;
    }

    .icon-btn:hover {
      background: rgba(255, 255, 255, 0.08);
      border-color: var(--border-subtle);
    }

    .brand-title {
      font-size: 17px;
      font-weight: 600;
      letter-spacing: 0.2px;
      display: flex;
      align-items: center;
      gap: 4px;
      color: #f8fafc;
    }

    .header-right {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .new-chat-pill {
      background: rgba(66, 133, 244, 0.16);
      color: #dbeafe;
      border: 1px solid rgba(66, 133, 244, 0.35);
      border-radius: 999px;
      padding: 7px 14px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.18s ease, transform 0.12s ease;
    }

    .new-chat-pill:hover {
      background: rgba(66, 133, 244, 0.28);
    }

    .new-chat-pill:active {
      transform: scale(0.97);
    }

    .avatar-circle {
      width: 34px;
      height: 34px;
      border-radius: 50%;
      background: linear-gradient(135deg, var(--accent-blue), var(--accent-purple));
      color: #ffffff;
      font-weight: 700;
      font-size: 14px;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 2px 10px rgba(66, 133, 244, 0.35);
      user-select: none;
    }

    /* Slide-out Left Drawer */
    .drawer-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.62);
      backdrop-filter: blur(4px);
      z-index: 40;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.22s ease;
    }

    .drawer-backdrop.open {
      opacity: 1;
      pointer-events: auto;
    }

    .side-drawer {
      position: fixed;
      top: 0;
      left: 0;
      bottom: 0;
      width: 280px;
      max-width: 82vw;
      background: #090e1a;
      border-right: 1px solid var(--border-subtle);
      z-index: 50;
      padding: 20px 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      transform: translateX(-100%);
      transition: transform 0.24s cubic-bezier(0.22, 1, 0.36, 1);
    }

    .side-drawer.open {
      transform: translateX(0);
    }

    .drawer-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border-subtle);
      font-weight: 600;
      font-size: 15px;
    }

    .drawer-action-btn {
      width: 100%;
      text-align: left;
      padding: 12px 14px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border-subtle);
      color: var(--text-primary);
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 10px;
      transition: background 0.16s ease;
    }

    .drawer-action-btn:hover {
      background: rgba(255, 255, 255, 0.1);
    }

    .drawer-action-btn.danger {
      color: #fca5a5;
      border-color: rgba(239, 68, 68, 0.25);
      background: rgba(239, 68, 68, 0.08);
    }

    .drawer-action-btn.danger:hover {
      background: rgba(239, 68, 68, 0.18);
    }

    /* Chat Area */
    .chat-container {
      flex: 1;
      overflow-y: auto;
      padding: 20px 16px 28px;
      display: flex;
      flex-direction: column;
      gap: 18px;
      scroll-behavior: smooth;
    }

    .empty-state {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      text-align: center;
      gap: 16px;
      padding: 40px 16px;
      user-select: none;
    }

    .gemini-star {
      width: 68px;
      height: 68px;
      animation: floatStar 3.6s ease-in-out infinite;
      filter: drop-shadow(0 0 22px rgba(108, 138, 255, 0.35));
    }

    @keyframes floatStar {
      0%, 100% {
        transform: translateY(0px) rotate(0deg) scale(1);
      }
      50% {
        transform: translateY(-7px) rotate(8deg) scale(1.04);
      }
    }

    .empty-label {
      font-size: 15px;
      color: var(--text-secondary);
      font-weight: 400;
    }

    /* Messages */
    .message-row {
      display: flex;
      flex-direction: column;
      max-width: 100%;
      animation: fadeIn 0.2s ease;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .message-row.user {
      align-items: flex-end;
    }

    .message-row.assistant {
      align-items: flex-start;
    }

    .bubble-user {
      max-width: 84%;
      background: linear-gradient(135deg, rgba(66, 133, 244, 0.28), rgba(155, 114, 203, 0.24));
      border: 1px solid rgba(129, 168, 248, 0.32);
      color: #f8fafc;
      padding: 12px 16px;
      border-radius: 20px 20px 6px 20px;
      font-size: 15px;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .bubble-assistant {
      max-width: 100%;
      width: 100%;
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 18px;
      padding: 14px 16px;
      font-size: 15px;
      line-height: 1.62;
      color: var(--text-primary);
      word-break: break-word;
    }

    .markdown-body p {
      margin-bottom: 10px;
    }
    .markdown-body p:last-child {
      margin-bottom: 0;
    }
    .markdown-body ul, .markdown-body ol {
      margin: 8px 0 12px 22px;
    }
    .markdown-body li {
      margin-bottom: 5px;
    }
    .markdown-body strong {
      color: #ffffff;
      font-weight: 600;
    }
    .markdown-body code {
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid rgba(148, 163, 184, 0.2);
      padding: 2px 6px;
      border-radius: 6px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.9em;
      color: #93c5fd;
    }
    .markdown-body pre {
      background: #050811;
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 12px;
      padding: 12px;
      overflow-x: auto;
      margin: 10px 0;
    }
    .markdown-body pre code {
      background: transparent;
      border: none;
      padding: 0;
      color: #e2e8f0;
      font-size: 13px;
    }

    .assistant-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 8px;
      padding-left: 4px;
    }

    .action-chip {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border-subtle);
      color: var(--text-secondary);
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      transition: background 0.15s ease, color 0.15s ease;
    }

    .action-chip:hover {
      background: rgba(255, 255, 255, 0.12);
      color: var(--text-primary);
    }

    /* Attachment Preview Strip */
    .attachment-bar {
      display: none;
      align-items: center;
      gap: 10px;
      padding: 8px 14px;
      margin: 0 12px 6px;
      background: rgba(18, 25, 40, 0.9);
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      font-size: 13px;
      color: #e2e8f0;
      flex-shrink: 0;
    }

    .attachment-bar.active {
      display: flex;
    }

    .attachment-thumb {
      width: 40px;
      height: 40px;
      border-radius: 8px;
      object-fit: cover;
      border: 1px solid var(--border-subtle);
    }

    .attachment-remove {
      margin-left: auto;
      background: rgba(239, 68, 68, 0.18);
      color: #fca5a5;
      border: none;
      border-radius: 8px;
      padding: 4px 8px;
      font-size: 12px;
      cursor: pointer;
    }

    /* Popup Menu for + button */
    .plus-menu {
      position: absolute;
      bottom: 76px;
      left: 16px;
      background: #0d1424;
      border: 1px solid var(--border-subtle);
      border-radius: 16px;
      padding: 8px;
      display: none;
      flex-direction: column;
      gap: 4px;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.65);
      z-index: 35;
      min-width: 235px;
    }

    .plus-menu.open {
      display: flex;
    }

    .plus-menu-item {
      background: transparent;
      border: none;
      color: var(--text-primary);
      padding: 10px 12px;
      border-radius: 10px;
      text-align: left;
      font-size: 13.5px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 10px;
      transition: background 0.15s ease;
    }

    .plus-menu-item:hover {
      background: rgba(255, 255, 255, 0.08);
    }

    /* Bottom Capsule Dock */
    .dock-wrapper {
      padding: 6px 12px 14px;
      background: transparent;
      flex-shrink: 0;
      z-index: 20;
    }

    .capsule-dock {
      display: flex;
      align-items: flex-end;
      gap: 6px;
      background: var(--bg-elevated);
      backdrop-filter: blur(18px);
      border: 1px solid var(--border-subtle);
      border-radius: 26px;
      padding: 6px 8px;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.5);
    }

    .dock-btn {
      width: 38px;
      height: 38px;
      border-radius: 50%;
      border: none;
      background: transparent;
      color: var(--text-primary);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 17px;
      flex-shrink: 0;
      transition: background 0.16s ease, transform 0.12s ease, color 0.16s ease;
    }

    .dock-btn:hover {
      background: rgba(255, 255, 255, 0.08);
    }

    .dock-btn:active {
      transform: scale(0.95);
    }

    .dock-btn.voice-live {
      background: var(--danger);
      color: #ffffff;
      box-shadow: 0 0 16px rgba(239, 68, 68, 0.55);
      animation: pulseRed 1.4s infinite;
    }

    @keyframes pulseRed {
      0%, 100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.55); }
      50% { box-shadow: 0 0 0 8px rgba(239, 68, 68, 0); }
    }

    .dock-btn.send-active {
      background: #4285f4;
      color: #ffffff;
    }

    .dock-btn.send-active:hover {
      background: #3374e0;
    }

    #promptInput {
      flex: 1;
      background: transparent;
      border: none;
      outline: none;
      color: var(--text-primary);
      font-family: inherit;
      font-size: 15px;
      line-height: 1.45;
      max-height: 140px;
      min-height: 38px;
      padding: 8px 6px;
      resize: none;
      overflow-y: auto;
    }

    #promptInput::placeholder {
      color: var(--text-secondary);
    }

    .wave-icon {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      height: 18px;
    }

    .wave-icon span {
      display: block;
      width: 3px;
      border-radius: 999px;
      background: currentColor;
    }

    .wave-icon span:nth-child(1) { height: 7px; }
    .wave-icon span:nth-child(2) { height: 15px; }
    .wave-icon span:nth-child(3) { height: 10px; }

    .hidden-input {
      display: none;
    }
  </style>
</head>
<body>
  <div class="drawer-backdrop" id="drawerBackdrop"></div>

  <aside class="side-drawer" id="sideDrawer">
    <div class="drawer-header">
      <span>Super-Agent</span>
      <button class="icon-btn" id="closeDrawerBtn" aria-label="Закрыть меню">✕</button>
    </div>
    <button class="drawer-action-btn" id="drawerNewChatBtn">
      <span>＋ Новый чат</span>
    </button>
    <button class="drawer-action-btn danger" id="drawerClearHistoryBtn">
      <span>🗑️ Очистить историю</span>
    </button>
  </aside>

  <div class="app-shell">
    <header class="top-header">
      <div class="header-left">
        <button class="icon-btn" id="openDrawerBtn" aria-label="Меню">☰</button>
        <div class="brand-title">Super-Agent ▾</div>
      </div>
      <div class="header-right">
        <button class="new-chat-pill" id="headerNewChatBtn">Новый чат</button>
        <div class="avatar-circle" title="Профиль">N</div>
      </div>
    </header>

    <main class="chat-container" id="chatContainer">
      <div class="empty-state" id="emptyState">
        <svg class="gemini-star" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="geminiGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#4285f4" />
              <stop offset="38%" stop-color="#9b72cb" />
              <stop offset="72%" stop-color="#d96570" />
              <stop offset="100%" stop-color="#f4b400" />
            </linearGradient>
          </defs>
          <path
            fill="url(#geminiGrad)"
            d="M32 2 C34.5 18.5 45.5 29.5 62 32 C45.5 34.5 34.5 45.5 32 62 C29.5 45.5 18.5 34.5 2 32 C18.5 29.5 29.5 18.5 32 2 Z"
          />
        </svg>
        <div class="empty-label">Чем я могу помочь вам сегодня?</div>
      </div>
    </main>

    <div class="attachment-bar" id="attachmentBar">
      <img id="attachmentThumb" class="attachment-thumb" alt="Превью" style="display:none;" />
      <span id="attachmentLabel"></span>
      <button type="button" class="attachment-remove" id="removeAttachmentBtn">✕</button>
    </div>

    <div class="plus-menu" id="plusMenu">
      <button type="button" class="plus-menu-item" id="pickPhotoBtn">
        <span>📷 Загрузить фото</span>
      </button>
      <button type="button" class="plus-menu-item" id="pickFileBtn">
        <span>📎 Загрузить файл (PDF/Word/Excel)</span>
      </button>
    </div>

    <input type="file" id="photoInput" class="hidden-input" accept="image/*" />
    <input type="file" id="fileInput" class="hidden-input" accept=".pdf,.doc,.docx,.xlsx,.xlsm,.txt,.md,.py,.js,.ts,.json,.csv,.html,.css" />

    <div class="dock-wrapper">
      <div class="capsule-dock">
        <button type="button" class="dock-btn" id="plusBtn" aria-label="Добавить">＋</button>
        <textarea
          id="promptInput"
          rows="1"
          placeholder="Спросить ..."
          autocomplete="off"
        ></textarea>
        <button type="button" class="dock-btn" id="micBtn" title="Голосовой ввод (uz-UZ)" aria-label="Микрофон">🎙️</button>
        <button type="button" class="dock-btn" id="actionBtn" title="Голосовой режим / Отправить" aria-label="Действие">
          <span class="wave-icon" id="waveIcon">
            <span></span><span></span><span></span>
          </span>
        </button>
      </div>
    </div>
  </div>

  <script>
    const chatContainer = document.getElementById("chatContainer");
    const emptyState = document.getElementById("emptyState");
    const promptInput = document.getElementById("promptInput");
    const actionBtn = document.getElementById("actionBtn");
    const micBtn = document.getElementById("micBtn");
    const plusBtn = document.getElementById("plusBtn");
    const plusMenu = document.getElementById("plusMenu");
    const photoInput = document.getElementById("photoInput");
    const fileInput = document.getElementById("fileInput");
    const pickPhotoBtn = document.getElementById("pickPhotoBtn");
    const pickFileBtn = document.getElementById("pickFileBtn");
    const attachmentBar = document.getElementById("attachmentBar");
    const attachmentThumb = document.getElementById("attachmentThumb");
    const attachmentLabel = document.getElementById("attachmentLabel");
    const removeAttachmentBtn = document.getElementById("removeAttachmentBtn");

    const openDrawerBtn = document.getElementById("openDrawerBtn");
    const closeDrawerBtn = document.getElementById("closeDrawerBtn");
    const sideDrawer = document.getElementById("sideDrawer");
    const drawerBackdrop = document.getElementById("drawerBackdrop");
    const headerNewChatBtn = document.getElementById("headerNewChatBtn");
    const drawerNewChatBtn = document.getElementById("drawerNewChatBtn");
    const drawerClearHistoryBtn = document.getElementById("drawerClearHistoryBtn");

    let attachedImageDataUrl = "";
    let attachedFileObj = null;
    let isStreaming = false;

    // Live voice conversation state
    let liveVoiceMode = false;
    let recognition = null;
    let isRecognizing = false;

    function openDrawer() {
      sideDrawer.classList.add("open");
      drawerBackdrop.classList.add("open");
    }

    function closeDrawer() {
      sideDrawer.classList.remove("open");
      drawerBackdrop.classList.remove("open");
    }

    openDrawerBtn.addEventListener("click", openDrawer);
    closeDrawerBtn.addEventListener("click", closeDrawer);
    drawerBackdrop.addEventListener("click", closeDrawer);

    function startNewViewChat() {
      chatContainer.innerHTML = "";
      chatContainer.appendChild(emptyState);
      emptyState.style.display = "flex";
      clearAttachment();
      promptInput.value = "";
      autoResizeTextarea();
      updateActionButton();
      closeDrawer();
    }

    headerNewChatBtn.addEventListener("click", startNewViewChat);
    drawerNewChatBtn.addEventListener("click", startNewViewChat);

    drawerClearHistoryBtn.addEventListener("click", async () => {
      closeDrawer();
      try {
        const res = await fetch("/api/clear", { method: "POST" });
        if (res.ok) {
          startNewViewChat();
        }
      } catch (e) {
        console.error("Failed to clear history:", e);
      }
    });

    function autoResizeTextarea() {
      promptInput.style.height = "auto";
      promptInput.style.height = Math.min(promptInput.scrollHeight, 140) + "px";
    }

    function hasComposerContent() {
      return (
        promptInput.value.trim().length > 0 ||
        Boolean(attachedImageDataUrl) ||
        Boolean(attachedFileObj)
      );
    }

    function updateActionButton() {
      if (hasComposerContent()) {
        actionBtn.classList.remove("voice-live");
        actionBtn.classList.add("send-active");
        actionBtn.innerHTML = "↑";
      } else {
        actionBtn.classList.remove("send-active");
        if (liveVoiceMode) {
          actionBtn.classList.add("voice-live");
        } else {
          actionBtn.classList.remove("voice-live");
        }
        actionBtn.innerHTML = '<span class="wave-icon"><span></span><span></span><span></span></span>';
      }
    }

    promptInput.addEventListener("input", () => {
      autoResizeTextarea();
      updateActionButton();
    });

    promptInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (!isStreaming && hasComposerContent()) {
          sendMessage();
        }
      }
    });

    // + Popup Menu
    plusBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      plusMenu.classList.toggle("open");
    });

    document.addEventListener("click", (e) => {
      if (!plusMenu.contains(e.target) && e.target !== plusBtn) {
        plusMenu.classList.remove("open");
      }
    });

    pickPhotoBtn.addEventListener("click", () => {
      plusMenu.classList.remove("open");
      photoInput.click();
    });

    pickFileBtn.addEventListener("click", () => {
      plusMenu.classList.remove("open");
      fileInput.click();
    });

    function clearAttachment() {
      attachedImageDataUrl = "";
      attachedFileObj = null;
      photoInput.value = "";
      fileInput.value = "";
      attachmentThumb.style.display = "none";
      attachmentThumb.src = "";
      attachmentLabel.textContent = "";
      attachmentBar.classList.remove("active");
      updateActionButton();
    }

    removeAttachmentBtn.addEventListener("click", clearAttachment);

    // Client-side canvas resize to max 1024px JPEG
    photoInput.addEventListener("change", (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        const img = new Image();
        img.onload = () => {
          const maxDim = 1024;
          let width = img.width;
          let height = img.height;
          if (width > maxDim || height > maxDim) {
            if (width >= height) {
              height = Math.round((height * maxDim) / width);
              width = maxDim;
            } else {
              width = Math.round((width * maxDim) / height);
              height = maxDim;
            }
          }
          const canvas = document.createElement("canvas");
          canvas.width = width;
          canvas.height = height;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(img, 0, 0, width, height);
          const jpegDataUrl = canvas.toDataURL("image/jpeg", 0.85);

          attachedFileObj = null;
          fileInput.value = "";
          attachedImageDataUrl = jpegDataUrl;

          attachmentThumb.src = jpegDataUrl;
          attachmentThumb.style.display = "block";
          attachmentLabel.textContent = `📷 ${file.name} (${width}x${height} JPEG)`;
          attachmentBar.classList.add("active");
          updateActionButton();
        };
        img.src = ev.target.result;
      };
      reader.readAsDataURL(file);
    });

    fileInput.addEventListener("change", (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      attachedImageDataUrl = "";
      photoInput.value = "";
      attachedFileObj = file;

      attachmentThumb.style.display = "none";
      attachmentThumb.src = "";
      attachmentLabel.textContent = `📎 ${file.name}`;
      attachmentBar.classList.add("active");
      updateActionButton();
    });

    function appendUserMessage(text, hasImg, fileName) {
      emptyState.style.display = "none";
      const row = document.createElement("div");
      row.className = "message-row user";

      const bubble = document.createElement("div");
      bubble.className = "bubble-user";
      let displayText = text || "";
      if (fileName) {
        displayText = displayText ? `${displayText}\n📎 ${fileName}` : `📎 ${fileName}`;
      }
      if (hasImg) {
        displayText = displayText ? `${displayText}\n📷 [Фото]` : "📷 [Фото]";
      }
      bubble.textContent = displayText;

      row.appendChild(bubble);
      chatContainer.appendChild(row);
      chatContainer.scrollTop = chatContainer.scrollHeight;
    }

    function stripMarkdownForSpeech(mdText) {
      return mdText
        .replace(/```[\s\S]*?```/g, " ")
        .replace(/`([^`]+)`/g, "$1")
        .replace(/[*_#>-]+/g, " ")
        .replace(/\s+/g, " ")
        .trim();
    }

    function speakUzbekText(rawMarkdown, onDoneCallback) {
      if (!("speechSynthesis" in window)) {
        if (onDoneCallback) onDoneCallback();
        return;
      }
      window.speechSynthesis.cancel();
      const plain = stripMarkdownForSpeech(rawMarkdown);
      if (!plain) {
        if (onDoneCallback) onDoneCallback();
        return;
      }
      const utter = new SpeechSynthesisUtterance(plain);
      utter.lang = "uz-UZ";
      utter.onend = () => {
        if (onDoneCallback) onDoneCallback();
      };
      utter.onerror = () => {
        if (onDoneCallback) onDoneCallback();
      };
      window.speechSynthesis.speak(utter);
    }

    function createAssistantMessageBubble() {
      emptyState.style.display = "none";
      const row = document.createElement("div");
      row.className = "message-row assistant";

      const bubble = document.createElement("div");
      bubble.className = "bubble-assistant markdown-body";

      const actions = document.createElement("div");
      actions.className = "assistant-actions";

      let rawMarkdown = "";

      const speakBtn = document.createElement("button");
      speakBtn.type = "button";
      speakBtn.className = "action-chip";
      speakBtn.innerHTML = "🔊";
      speakBtn.title = "Озвучить";
      speakBtn.addEventListener("click", () => {
        speakUzbekText(rawMarkdown, null);
      });

      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "action-chip";
      copyBtn.innerHTML = "📋";
      copyBtn.title = "Копировать";
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(rawMarkdown);
          copyBtn.innerHTML = "✓";
          setTimeout(() => {
            copyBtn.innerHTML = "📋";
          }, 1400);
        } catch (err) {
          console.error("Copy error:", err);
        }
      });

      actions.appendChild(speakBtn);
      actions.appendChild(copyBtn);

      row.appendChild(bubble);
      row.appendChild(actions);
      chatContainer.appendChild(row);
      chatContainer.scrollTop = chatContainer.scrollHeight;

      return {
        setText(fullText) {
          rawMarkdown = fullText;
          bubble.innerHTML = marked.parse(rawMarkdown || "");
          chatContainer.scrollTop = chatContainer.scrollHeight;
        },
        getText() {
          return rawMarkdown;
        },
      };
    }

    async function sendMessage(overrideText = null) {
      if (isStreaming) return;
      const textToSend = overrideText !== null ? overrideText.trim() : promptInput.value.trim();
      const imgToSend = attachedImageDataUrl;
      const fileToSend = attachedFileObj;

      if (!textToSend && !imgToSend && !fileToSend) return;

      isStreaming = true;
      appendUserMessage(textToSend, Boolean(imgToSend), fileToSend ? fileToSend.name : "");

      if (overrideText === null) {
        promptInput.value = "";
        autoResizeTextarea();
      }
      clearAttachment();

      const assistantView = createAssistantMessageBubble();
      let accumulated = "";

      const formData = new FormData();
      formData.append("message", textToSend);
      if (imgToSend) {
        formData.append("image", imgToSend);
      }
      if (fileToSend) {
        formData.append("file", fileToSend);
      }

      try {
        const response = await fetch("/api/chat", {
          method: "POST",
          body: formData,
        });

        if (!response.ok || !response.body) {
          const errText = await response.text();
          assistantView.setText(`**Ошибка сервера:** ${errText}`);
          isStreaming = false;
          return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";

          for (const part of parts) {
            const line = part.trim();
            if (!line.startsWith("data:")) continue;
            const dataStr = line.slice(5).trim();
            if (dataStr === "[DONE]") {
              break;
            }
            try {
              const parsed = JSON.parse(dataStr);
              if (parsed && typeof parsed.t === "string") {
                accumulated += parsed.t;
                assistantView.setText(accumulated);
              }
            } catch (parseErr) {
              console.error("SSE parse error:", parseErr);
            }
          }
        }
      } catch (netErr) {
        accumulated += `\n\n**Сетевая ошибка:** ${netErr.message || netErr}`;
        assistantView.setText(accumulated);
      } finally {
        isStreaming = false;
        updateActionButton();

        if (liveVoiceMode && accumulated.trim()) {
          speakUzbekText(accumulated, () => {
            if (liveVoiceMode) {
              startSpeechRecognition(true);
            }
          });
        } else if (liveVoiceMode) {
          startSpeechRecognition(true);
        }
      }
    }

    // Speech Recognition (uz-UZ)
    function initSpeechRecognition() {
      const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRec) return null;
      const rec = new SpeechRec();
      rec.lang = "uz-UZ";
      rec.interimResults = false;
      rec.maxAlternatives = 1;
      return rec;
    }

    function startSpeechRecognition(autoSendWhenDone) {
      if (!recognition) {
        recognition = initSpeechRecognition();
      }
      if (!recognition) {
        alert("Brauzeringiz Web Speech API ni qo'llab-quvvatlamaydi.");
        liveVoiceMode = false;
        updateActionButton();
        return;
      }
      if (isRecognizing) return;

      recognition.onstart = () => {
        isRecognizing = true;
      };

      recognition.onresult = (event) => {
        const transcript =
          event.results &&
          event.results[0] &&
          event.results[0][0] &&
          event.results[0][0].transcript
            ? event.results[0][0].transcript.trim()
            : "";

        if (transcript) {
          if (autoSendWhenDone || liveVoiceMode) {
            sendMessage(transcript);
          } else {
            promptInput.value = promptInput.value
              ? `${promptInput.value} ${transcript}`
              : transcript;
            autoResizeTextarea();
            updateActionButton();
          }
        }
      };

      recognition.onerror = (err) => {
        isRecognizing = false;
        console.warn("SpeechRecognition error:", err);
      };

      recognition.onend = () => {
        isRecognizing = false;
      };

      try {
        recognition.start();
      } catch (e) {
        isRecognizing = false;
      }
    }

    micBtn.addEventListener("click", () => {
      startSpeechRecognition(false);
    });

    actionBtn.addEventListener("click", () => {
      if (hasComposerContent()) {
        sendMessage();
      } else {
        liveVoiceMode = !liveVoiceMode;
        updateActionButton();
        if (liveVoiceMode) {
          startSpeechRecognition(true);
        } else {
          if ("speechSynthesis" in window) {
            window.speechSynthesis.cancel();
          }
          if (recognition && isRecognizing) {
            try {
              recognition.stop();
            } catch (e) {}
          }
        }
      }
    });

    async function loadInitialHistory() {
      try {
        const res = await fetch("/api/history");
        if (!res.ok) return;
        const data = await res.json();
        const msgs = Array.isArray(data.messages) ? data.messages : [];
        if (msgs.length > 0) {
          emptyState.style.display = "none";
          for (const m of msgs) {
            if (m.role === "user") {
              appendUserMessage(m.content || "", false, "");
            } else if (m.role === "assistant") {
              const view = createAssistantMessageBubble();
              view.setText(m.content || "");
            }
          }
          chatContainer.scrollTop = chatContainer.scrollHeight;
        }
      } catch (e) {
        console.error("Failed to load history:", e);
      }
    }

    updateActionButton();
    loadInitialHistory();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def serve_index():
    return HTMLResponse(content=HTML_PAGE)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
