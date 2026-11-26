import os
import re
import json
import uuid
import asyncio
import tempfile
import subprocess
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime, timezone

import uvicorn
import litellm
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ============================================================
# SUPER-AGENT CONFIGURATION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger("Super-Agent")

PORT = int(os.environ.get("PORT", 8000))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

PRIMARY_MODELS = [
    "gemini/gemini-2.5-flash",
    "gemini/gemini-2.0-flash",
]

FALLBACK_MODELS = [
    "openrouter/openrouter/free",
    "openrouter/google/gemma-3-27b-it:free",
]

MAX_FILE_SIZE = 15 * 1024 * 1024
MAX_TEXT_LENGTH = 12000
MAX_HISTORY = 30
PYTHON_TIMEOUT = 6

litellm.drop_params = True
litellm.set_verbose = False

SYSTEM_PROMPT = """
Sen Super-Agent nomli shaxsiy sun'iy intellekt yordamchisisan.

SENING SHAXSIYATING:
- O'zingni "Men" deb ifoda et.
- Tabiiy, samimiy, aqlli va zamonaviy o'zbek tilida gapir.
- Foydalanuvchiga do'stona munosabatda bo'l.
- Robotga o'xshagan standart javoblar bermagin.
- Foydalanuvchining savolini diqqat bilan tushun.
- Javoblaring aniq, foydali va tushunarli bo'lsin.
- Keraksiz kirish gaplarini ishlatma.

JAVOB FORMATI:
- Muhim ma'lumotlarni **qalin** yoz.
- Kerak bo'lsa, punktlardan foydalan.
- Murakkab masalalarni bosqichma-bosqich tushuntir.
- Kodlarni to'liq va to'g'ri formatda yoz.
- Matematik formulalarni aniq ifodala.
- O'zbek tilining lotin yozuvidan foydalan.

RASMLAR:
- Rasmdagi noaniq yoki xira tafsilotlarni o'ylab topma.
- Ko'rinmayotgan matnni taxmin qilib yozma.
- Ishonching komil bo'lmagan ma'lumotni aniq fakt sifatida ko'rsatma.
- Rasmni ko'rib bo'lmasa, buni ochiq ayt.

XOTIRA:
- Berilgan suhbat tarixidan foydalan.
- Oldingi xabarlar bilan bog'liq savollarga kontekst asosida javob ber.
- Qidiruv natijalarini tekshirilgan ma'lumot sifatida ehtiyotkorlik bilan ishlat.

KOD:
- Agar foydalanuvchi hisoblash yoki Python yordamida bajarish mumkin bo'lgan
  masalani so'rasa, zarur bo'lganda quyidagi formatdan foydalan:

[PY]
print(1 + 1)
[/PY]

- Python kodini faqat haqiqiy bajarish zarur bo'lganda ishlat.
- Oddiy kod yozish so'rovlarida [PY] blokini ishlatish shart emas.

Har doim foydalanuvchining savoliga bevosita javob ber.
"""

# ============================================================
# DATABASE
# ============================================================

_db_lock = asyncio.Lock()


def get_db_connection():
    if not DATABASE_URL:
        return None

    try:
        return psycopg2.connect(
            DATABASE_URL,
            connect_timeout=5,
            sslmode="require"
        )
    except Exception as exc:
        logger.error("Database connection error: %s", exc)
        return None


def initialize_database():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL is not configured. Memory is disabled.")
        return

    connection = get_db_connection()

    if connection is None:
        return

    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS mind_chat (
                        id SERIAL PRIMARY KEY,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS mind_chat_created_idx
                    ON mind_chat (created_at DESC)
                """)

        logger.info("PostgreSQL memory initialized.")

    except Exception as exc:
        logger.exception("Database initialization failed: %s", exc)

    finally:
        connection.close()


def save_message(role: str, content: str):
    if not DATABASE_URL or not content:
        return

    connection = get_db_connection()

    if connection is None:
        return

    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO mind_chat (role, content)
                    VALUES (%s, %s)
                    """,
                    (role, content[:50000])
                )

    except Exception as exc:
        logger.error("Could not save memory: %s", exc)

    finally:
        connection.close()


def get_history(limit: int = MAX_HISTORY):
    if not DATABASE_URL:
        return []

    connection = get_db_connection()

    if connection is None:
        return []

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT id, role, content, created_at
                FROM mind_chat
                ORDER BY id DESC
                LIMIT %s
                """,
                (limit,)
            )

            rows = cursor.fetchall()

            result = []

            for row in reversed(rows):
                result.append({
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": (
                        row["created_at"].isoformat()
                        if row["created_at"]
                        else None
                    )
                })

            return result

    except Exception as exc:
        logger.error("History retrieval failed: %s", exc)
        return []

    finally:
        connection.close()


def clear_history():
    if not DATABASE_URL:
        return False

    connection = get_db_connection()

    if connection is None:
        return False

    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM mind_chat")

        return True

    except Exception as exc:
        logger.error("History clearing failed: %s", exc)
        return False

    finally:
        connection.close()


def retrieve_memory(query: str, limit: int = 5):
    """
    Lightweight keyword-based retrieval from previous messages.
    """

    if not query.strip():
        return []

    history = get_history(100)

    words = set(
        word.lower()
        for word in re.findall(r"[\w']+", query)
        if len(word) > 2
    )

    if not words:
        return []

    scored = []

    for item in history:
        content = item["content"]
        content_words = set(
            word.lower()
            for word in re.findall(r"[\w']+", content)
            if len(word) > 2
        )

        score = len(words.intersection(content_words))

        if score:
            scored.append((score, item))

    scored.sort(key=lambda item: item[0], reverse=True)

    return [
        item for _, item in scored[:limit]
    ]


# ============================================================
# DOCUMENT EXTRACTION
# ============================================================

def extract_document_text(filename: str, data: bytes):
    extension = Path(filename).suffix.lower()

    if extension == ".pdf":
        from pypdf import PdfReader
        from io import BytesIO

        reader = PdfReader(BytesIO(data))

        pages = []

        for page in reader.pages:
            text = page.extract_text() or ""

            if text:
                pages.append(text)

            if sum(len(p) for p in pages) >= MAX_TEXT_LENGTH:
                break

        return "\n".join(pages)[:MAX_TEXT_LENGTH]

    if extension == ".docx":
        from docx import Document
        from io import BytesIO

        document = Document(BytesIO(data))

        paragraphs = [
            paragraph.text
            for paragraph in document.paragraphs
            if paragraph.text.strip()
        ]

        for table in document.tables:
            for row in table.rows:
                paragraphs.append(
                    " | ".join(cell.text for cell in row.cells)
                )

        return "\n".join(paragraphs)[:MAX_TEXT_LENGTH]

    if extension == ".xlsx":
        from openpyxl import load_workbook
        from io import BytesIO

        workbook = load_workbook(
            BytesIO(data),
            read_only=True,
            data_only=True
        )

        output = []

        for sheet in workbook.worksheets:
            output.append(f"\n--- Sheet: {sheet.title} ---")

            for row in sheet.iter_rows(values_only=True):
                values = [
                    str(value) if value is not None else ""
                    for value in row
                ]

                if any(values):
                    output.append(" | ".join(values))

                if sum(len(line) for line in output) >= MAX_TEXT_LENGTH:
                    break

            if sum(len(line) for line in output) >= MAX_TEXT_LENGTH:
                break

        workbook.close()

        return "\n".join(output)[:MAX_TEXT_LENGTH]

    if extension in {
        ".txt", ".py", ".js", ".html", ".css", ".json",
        ".xml", ".csv", ".md", ".yaml", ".yml", ".sql",
        ".sh", ".java", ".cpp", ".c", ".h", ".ts",
        ".jsx", ".tsx", ".log", ".ini", ".toml"
    }:
        return data.decode("utf-8", errors="replace")[:MAX_TEXT_LENGTH]

    raise ValueError(
        f"Qo'llab-quvvatlanmaydigan fayl formati: {extension}"
    )


# ============================================================
# IMAGE PROCESSING
# ============================================================

def validate_image_data(image_data: str):
    if not image_data:
        return None

    match = re.match(
        r"^data:(image/(?:jpeg|png|webp|gif));base64,(.+)$",
        image_data,
        re.DOTALL | re.IGNORECASE
    )

    if not match:
        raise ValueError("Rasm formati noto'g'ri.")

    import base64

    encoded = match.group(2)

    if len(encoded) > 12 * 1024 * 1024:
        raise ValueError("Rasm hajmi juda katta.")

    try:
        decoded = base64.b64decode(encoded, validate=True)
    except Exception:
        raise ValueError("Rasmning Base64 ma'lumoti noto'g'ri.")

    if len(decoded) > 8 * 1024 * 1024:
        raise ValueError("Rasm hajmi ruxsat etilgan chegaradan katta.")

    mime = match.group(1).lower()

    return f"data:{mime};base64,{encoded}"


# ============================================================
# LIVE WEB SEARCH
# ============================================================

SEARCH_KEYWORDS = [
    "bugun",
    "hozir",
    "narx",
    "kurs",
    "dollar",
    "ob-havo",
    "ob havo",
    "yangilik",
    "2025",
    "2026",
    "natija",
    "sayt",
    "bugungi",
    "hozirgi",
    "so'nggi",
    "oxirgi",
    "yangilangan",
    "valyuta",
    "bitcoin",
    "ethereum",
    "weather",
    "news",
    "today",
    "latest",
    "price",
    "exchange rate",
    "current",
    "stock",
    "forecast",
    "today's",
    "breaking"
]


def should_search_web(query: str):
    text = query.lower()

    return any(
        keyword in text
        for keyword in SEARCH_KEYWORDS
    )


async def perform_web_search(query: str):
    try:
        from duckduckgo_search import DDGS

        def search():
            with DDGS() as ddgs:
                return list(
                    ddgs.text(
                        query,
                        max_results=4,
                        safesearch="moderate"
                    )
                )

        results = await asyncio.to_thread(search)

        formatted = []

        for index, item in enumerate(results[:4], 1):
            formatted.append(
                f"""
Natija {index}:
Sarlavha: {item.get('title', 'Noma’lum')}
Manba: {item.get('href', '')}
Ma'lumot: {item.get('body', '')}
"""
            )

        return "\n".join(formatted)

    except Exception as exc:
        logger.warning("Web search failed: %s", exc)
        return ""


# ============================================================
# AI MODEL MANAGEMENT
# ============================================================

def get_available_models():
    models = []

    if GEMINI_API_KEY:
        models.extend(PRIMARY_MODELS)

    if OPENROUTER_API_KEY:
        models.extend(FALLBACK_MODELS)

    return models


def configure_model_environment(model: str):
    if model.startswith("gemini/"):
        os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY

    elif model.startswith("openrouter/"):
        os.environ["OPENROUTER_API_KEY"] = OPENROUTER_API_KEY


async def create_ai_response(messages, stream=True):
    """
    Try configured models in priority order.
    """

    models = get_available_models()

    if not models:
        raise RuntimeError(
            "AI API kalitlari sozlanmagan. "
            "GEMINI_API_KEY yoki OPENROUTER_API_KEY kerak."
        )

    last_error = None

    for model in models:
        try:
            configure_model_environment(model)

            logger.info("Trying model: %s", model)

            response = await litellm.acompletion(
                model=model,
                messages=messages,
                stream=stream,
                temperature=0.7,
                max_tokens=4096,
                timeout=60
            )

            return response, model

        except Exception as exc:
            last_error = exc

            logger.warning(
                "Model %s failed: %s",
                model,
                exc
            )

    raise RuntimeError(
        f"Barcha AI modellari ishlamadi: {last_error}"
    )


async def create_explanation(code: str, output: str):
    """
    Explain Python execution output using Gemini 2.5 Flash.
    """

    if not GEMINI_API_KEY:
        return "Natija tayyor, ammo uni izohlash uchun Gemini API kaliti mavjud emas."

    messages = [
        {
            "role": "system",
            "content": (
                "Sen Python natijalarini sodda va qisqa o'zbek tilida "
                "tushuntiradigan yordamchisan. "
                "Natijani tahlil qil va muhim xatolarni tushuntir."
            )
        },
        {
            "role": "user",
            "content": (
                f"Bajarilgan kod:\n```python\n{code}\n```\n\n"
                f"Bajarilish natijasi:\n{output}\n\n"
                "Natijani qisqa tushuntir."
            )
        }
    ]

    try:
        configure_model_environment(PRIMARY_MODELS[0])

        r2 = await litellm.acompletion(
            model="gemini/gemini-2.5-flash",
            messages=messages,
            stream=False,
            temperature=0.3,
            max_tokens=1000,
            timeout=30
        )

        return r2.choices[0].message.content or "Natija olindi."

    except Exception as exc:
        logger.error("Explanation failed: %s", exc)
        return "Hisoblash natijasi olindi, lekin izoh yaratilmadi."


# ============================================================
# PYTHON EXECUTION
# ============================================================

def execute_python_code(code: str):
    """
    Execute a Python snippet in a separate process with a timeout.

    IMPORTANT:
    A subprocess and timeout are not a complete security sandbox.
    For genuinely untrusted code, use a separately isolated container
    with restricted filesystem, network, privileges and resources.
    """

    filename = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            encoding="utf-8",
            delete=False
        ) as temporary_file:

            filename = temporary_file.name
            temporary_file.write(code)

        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1"
        }

        result = subprocess.run(
            [
                os.environ.get("PYTHON", "python3"),
                "-I",
                filename
            ],
            capture_output=True,
            text=True,
            timeout=PYTHON_TIMEOUT,
            cwd=tempfile.gettempdir(),
            env=environment,
            shell=False
        )

        stdout = result.stdout[:12000]
        stderr = result.stderr[:6000]

        if result.returncode == 0:
            return stdout or "Kod muvaffaqiyatli bajarildi. Chiqish ma'lumoti yo'q."

        return (
            f"Xatolik kodi: {result.returncode}\n"
            f"STDOUT:\n{stdout}\n"
            f"STDERR:\n{stderr}"
        )

    except subprocess.TimeoutExpired:
        return "Xatolik: Python kodi 6 soniyalik vaqt chegarasidan oshdi."

    except Exception as exc:
        return f"Python bajarish xatosi: {str(exc)}"

    finally:
        if filename:
            try:
                os.remove(filename)
            except OSError:
                pass


def extract_python_code(text: str):
    pattern = r"\[PY\](.*?)\[/PY\]"

    matches = re.findall(
        pattern,
        text,
        flags=re.DOTALL
    )

    if not matches:
        return None

    return matches[0].strip()


# ============================================================
# SSE UTILITIES
# ============================================================

def sse_message(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_done():
    return "data: [DONE]\n\n"


# ============================================================
# FASTAPI APPLICATION
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()

    logger.info("Super-Agent started on port %s", PORT)

    yield

    logger.info("Super-Agent shutting down.")


app = FastAPI(
    title="Super-Agent",
    description="Personal AI Assistant",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"]
)


# ============================================================
# FRONTEND HTML
# ============================================================

HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="theme-color" content="#05070c">
<meta name="description" content="Super-Agent — Your personal AI assistant.">
<title>Super-Agent</title>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>

<style>
:root {
    --bg: #05070c;
    --surface: #131722;
    --surface2: #1a2030;
    --text: #e8eaf1;
    --muted: #969eae;
    --blue: #4285f4;
    --border: rgba(255,255,255,.09);
    --capsule: #1b2030;
}

* {
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
}

html, body {
    width: 100%;
    height: 100%;
    margin: 0;
    overflow: hidden;
}

body {
    font-family: Inter, -apple-system, BlinkMacSystemFont,
                 "Segoe UI", sans-serif;
    color: var(--text);
    background:
        radial-gradient(
            circle at 50% 100%,
            #0f1d3a 0%,
            #05070c 55%,
            #000 100%
        );
}

button, textarea {
    font: inherit;
}

button {
    color: inherit;
    border: none;
    cursor: pointer;
}

#app {
    height: 100%;
    display: flex;
    flex-direction: column;
    position: relative;
}

header {
    height: 65px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 17px;
    border-bottom: 1px solid rgba(255,255,255,.035);
    background: rgba(5,7,12,.4);
    backdrop-filter: blur(18px);
    z-index: 10;
}

.header-left, .header-right {
    display: flex;
    align-items: center;
    gap: 13px;
}

.icon-button {
    width: 39px;
    height: 39px;
    background: transparent;
    border-radius: 50%;
    font-size: 21px;
    display: grid;
    place-items: center;
}

.icon-button:hover {
    background: rgba(255,255,255,.08);
}

.brand {
    font-size: 19px;
    font-weight: 650;
    letter-spacing: -.5px;
    display: flex;
    align-items: center;
    gap: 5px;
}

.brand-arrow {
    font-size: 12px;
    color: #9ca7bd;
}

.new-chat {
    border: 1px solid var(--border);
    border-radius: 30px;
    background: rgba(255,255,255,.055);
    padding: 9px 14px;
    font-size: 12px;
    font-weight: 600;
}

.avatar {
    width: 34px;
    height: 34px;
    background: linear-gradient(140deg,#4385f4,#a16cce);
    border-radius: 50%;
    display: grid;
    place-items: center;
    font-weight: 700;
    font-size: 15px;
}

#drawer {
    position: fixed;
    top: 0;
    left: -300px;
    bottom: 0;
    width: 285px;
    background: #101521;
    border-right: 1px solid var(--border);
    z-index: 30;
    padding: 22px 16px;
    transition: left .25s ease;
}

#drawer.open {
    left: 0;
}

.drawer-title {
    font-size: 19px;
    font-weight: 700;
    margin: 12px 0 28px;
}

.drawer-action {
    width: 100%;
    text-align: left;
    padding: 15px;
    background: transparent;
    border-radius: 12px;
    color: #d7dce8;
    margin-bottom: 5px;
    font-size: 14px;
}

.drawer-action:hover {
    background: rgba(255,255,255,.07);
}

#overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,.55);
    z-index: 25;
}

#overlay.show {
    display: block;
}

#chat {
    flex: 1;
    overflow-y: auto;
    padding: 20px 0 30px;
    scroll-behavior: smooth;
}

#chat::-webkit-scrollbar {
    width: 5px;
}

#chat::-webkit-scrollbar-thumb {
    background: #30394b;
    border-radius: 10px;
}

.empty-state {
    height: 100%;
    min-height: 350px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    padding: 20px;
    text-align: center;
}

.gemini-star {
    width: 78px;
    height: 78px;
    margin-bottom: 30px;
    filter: drop-shadow(0 0 25px rgba(91,130,255,.25));
    animation: float 4s ease-in-out infinite;
}

@keyframes float {
    0%,100% { transform: translateY(0); }
    50% { transform: translateY(-9px); }
}

.empty-state h1 {
    font-size: clamp(27px,5vw,38px);
    font-weight: 500;
    letter-spacing: -1px;
    margin: 0 0 10px;
    background: linear-gradient(100deg,#8bb8ff,#c9a5fa,#e9a6a8);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
}

.empty-state p {
    font-size: 14px;
    color: var(--muted);
    max-width: 310px;
    line-height: 1.6;
}

.message {
    max-width: 850px;
    width: calc(100% - 36px);
    margin: 0 auto 27px;
    display: flex;
    flex-direction: column;
    animation: appear .2s ease;
}

@keyframes appear {
    from { opacity: 0; transform: translateY(7px); }
    to { opacity: 1; transform: translateY(0); }
}

.user-message {
    align-items: flex-end;
}

.ai-message {
    align-items: flex-start;
}

.user-bubble {
    max-width: 85%;
    padding: 13px 17px;
    border-radius: 19px 19px 5px 19px;
    background: #25334b;
    line-height: 1.6;
    font-size: 15px;
    overflow-wrap: anywhere;
}

.ai-content {
    width: 100%;
    font-size: 15px;
    line-height: 1.8;
    color: #e5e8ef;
    overflow-wrap: anywhere;
}

.ai-content p {
    margin: 7px 0 14px;
}

.ai-content h1,
.ai-content h2,
.ai-content h3 {
    line-height: 1.4;
    margin: 20px 0 10px;
    color: #fff;
}

.ai-content h1 { font-size: 23px; }
.ai-content h2 { font-size: 20px; }
.ai-content h3 { font-size: 17px; }

.ai-content strong {
    color: #fff;
    font-weight: 700;
}

.ai-content ul,
.ai-content ol {
    padding-left: 23px;
}

.ai-content li {
    margin-bottom: 6px;
}

.ai-content blockquote {
    border-left: 3px solid #709cff;
    padding: 5px 15px;
    margin: 12px 0;
    color: #aeb9cb;
    background: rgba(255,255,255,.025);
}

.ai-content pre {
    background: #101522;
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 15px;
    overflow-x: auto;
    font-size: 13px;
}

.ai-content code {
    background: rgba(255,255,255,.08);
    padding: 2px 5px;
    border-radius: 5px;
    font-family: monospace;
}

.ai-content pre code {
    background: transparent;
    padding: 0;
}

.ai-content a {
    color: #83b2ff;
}

.ai-content table {
    display: block;
    overflow-x: auto;
    border-collapse: collapse;
}

.ai-content td, .ai-content th {
    border: 1px solid var(--border);
    padding: 8px;
}

.ai-tools {
    display: flex;
    gap: 9px;
    margin-top: 12px;
}

.ai-tools button {
    background: rgba(255,255,255,.06);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 7px 12px;
    font-size: 12px;
    color: #b9c2d4;
}

.ai-tools button:hover {
    background: rgba(255,255,255,.13);
}

.typing {
    display: flex;
    gap: 5px;
    padding: 15px 5px;
}

.typing span {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #9bbcff;
    animation: pulse 1s infinite alternate;
}

.typing span:nth-child(2) { animation-delay: .2s; }
.typing span:nth-child(3) { animation-delay: .4s; }

@keyframes pulse {
    to { opacity: .25; transform: translateY(-4px); }
}

#composer-area {
    flex-shrink: 0;
    padding: 10px 15px calc(14px + env(safe-area-inset-bottom));
    background: linear-gradient(
        180deg,
        transparent,
        rgba(5,7,12,.65) 30%,
        rgba(5,7,12,.9)
    );
}

.composer-wrap {
    max-width: 850px;
    margin: 0 auto;
}

#file-preview {
    display: none;
    margin-bottom: 9px;
    gap: 9px;
    align-items: center;
    flex-wrap: wrap;
}

.preview-item {
    background: #202b40;
    padding: 8px 11px;
    border-radius: 12px;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 9px;
}

.preview-item button {
    background: transparent;
    color: #ff9a9a;
}

.composer {
    background: var(--capsule);
    border: 1px solid rgba(255,255,255,.10);
    border-radius: 27px;
    padding: 10px 12px;
    display: flex;
    align-items: flex-end;
    gap: 8px;
    box-shadow: 0 12px 50px rgba(0,0,0,.25);
    transition: border-color .2s;
}

.composer:focus-within {
    border-color: rgba(113,155,255,.45);
}

#message-input {
    flex: 1;
    min-width: 0;
    max-height: 150px;
    min-height: 40px;
    resize: none;
    border: none;
    outline: none;
    background: transparent;
    color: white;
    padding: 10px 2px;
    font-size: 15px;
    line-height: 1.5;
}

#message-input::placeholder {
    color: #929bac;
}

.composer-button {
    width: 39px;
    height: 39px;
    flex-shrink: 0;
    border-radius: 50%;
    background: transparent;
    font-size: 20px;
    display: grid;
    place-items: center;
}

.composer-button:hover {
    background: rgba(255,255,255,.09);
}

#send-button {
    background: #e8edf7;
    color: #111725;
    font-size: 23px;
    font-weight: 700;
    display: none;
}

#send-button.visible {
    display: grid;
}

#voice-button.active {
    background: #f04455;
    color: white;
    animation: voicePulse 1.5s infinite;
}

@keyframes voicePulse {
    50% { box-shadow: 0 0 0 9px rgba(240,68,85,.15); }
}

#mic-button {
    color: #a9b6ca;
}

#plus-menu {
    position: absolute;
    bottom: 80px;
    left: 15px;
    width: 245px;
    max-width: calc(100% - 30px);
    padding: 8px;
    background: #202738;
    border: 1px solid var(--border);
    border-radius: 16px;
    display: none;
    z-index: 20;
    box-shadow: 0 12px 35px rgba(0,0,0,.4);
}

#plus-menu.show {
    display: block;
}

.menu-option {
    display: block;
    width: 100%;
    text-align: left;
    padding: 13px;
    background: transparent;
    border-radius: 10px;
    font-size: 13px;
}

.menu-option:hover {
    background: rgba(255,255,255,.08);
}

#status {
    text-align: center;
    color: #8793a8;
    font-size: 11px;
    margin-top: 9px;
    min-height: 14px;
}

#image-input, #file-input {
    display: none;
}

@media (min-width: 700px) {
    #composer-area {
        padding-bottom: 25px;
    }

    .message {
        width: calc(100% - 70px);
    }

    .user-bubble {
        max-width: 75%;
    }

    #plus-menu {
        left: max(15px, calc((100% - 850px) / 2));
    }
}

@media (max-width: 400px) {
    header {
        padding: 0 10px;
    }

    .brand {
        font-size: 16px;
    }

    .new-chat {
        padding: 8px 10px;
        font-size: 11px;
    }

    .header-left, .header-right {
        gap: 5px;
    }
}
</style>
</head>

<body>

<div id="app">

    <header>
        <div class="header-left">
            <button class="icon-button" id="drawer-toggle" aria-label="Menu">☰</button>
            <div class="brand">
                Super-Agent <span class="brand-arrow">▾</span>
            </div>
        </div>

        <div class="header-right">
            <button class="new-chat" id="new-chat">Новый чат</button>
            <div class="avatar">N</div>
        </div>
    </header>

    <div id="drawer">
        <div class="drawer-title">Super-Agent</div>

        <button class="drawer-action" id="drawer-new">
            ＋ Новый чат
        </button>

        <button class="drawer-action" id="clear-history">
            🗑️ Очистить историю
        </button>

        <div style="position:absolute;bottom:25px;left:20px;color:#778399;font-size:12px">
            Personal AI Assistant
            <br><br>
            Super-Agent v1.0
        </div>
    </div>

    <div id="overlay"></div>

    <main id="chat">

        <div class="empty-state" id="empty-state">

            <svg class="gemini-star" viewBox="0 0 100 100" fill="none">
                <defs>
                    <linearGradient id="geminiGradient"
                        x1="0" y1="0" x2="100" y2="100"
                        gradientUnits="userSpaceOnUse">
                        <stop offset="0%" stop-color="#4285f4"/>
                        <stop offset="36%" stop-color="#9b72cb"/>
                        <stop offset="70%" stop-color="#d96570"/>
                        <stop offset="100%" stop-color="#f4b400"/>
                    </linearGradient>
                </defs>
                <path
                    d="M50 2
                    C55 30 68 43 98 50
                    C68 56 55 70 50 98
                    C44 70 31 56 2 50
                    C31 43 44 30 50 2Z"
                    fill="url(#geminiGradient)"
                />
            </svg>

            <h1>Salom, qanday yordam beray?</h1>

            <p>
                Men Super-Agent. Savollaringizga javob beraman,
                fikrlaringizni rivojlantiraman va vazifalaringizni bajarishga yordam beraman.
            </p>
        </div>

    </main>

    <div id="plus-menu">

        <button class="menu-option" id="upload-image">
            📷 Yuklash фото
        </button>

        <button class="menu-option" id="upload-file">
            📎 Yuklash файл (PDF/Word/Excel)
        </button>

    </div>

    <div id="composer-area">

        <div class="composer-wrap">

            <div id="file-preview"></div>

            <div class="composer">

                <button class="composer-button" id="plus-button" aria-label="Attach">
                    ＋
                </button>

                <textarea
                    id="message-input"
                    placeholder="Спросить ..."
                    rows="1"
                    aria-label="Message"
                ></textarea>

                <button class="composer-button" id="mic-button" aria-label="Speech input">
                    🎙️
                </button>

                <button class="composer-button" id="voice-button" aria-label="Live voice">
                    ıll
                </button>

                <button class="composer-button" id="send-button" aria-label="Send">
                    ↑
                </button>

            </div>

            <div id="status">
                Super-Agent sizga yordam berishga tayyor.
            </div>

        </div>

        <input type="file" id="image-input" accept="image/*">
        <input type="file" id="file-input"
            accept=".pdf,.docx,.xlsx,.txt,.py,.js,.html,.css,.json,.csv,.md,.xml,.sql,.log">

    </div>

</div>

<script>
const chat = document.getElementById("chat");
const input = document.getElementById("message-input");
const sendButton = document.getElementById("send-button");
const voiceButton = document.getElementById("voice-button");
const micButton = document.getElementById("mic-button");
const plusButton = document.getElementById("plus-button");
const plusMenu = document.getElementById("plus-menu");
const statusElement = document.getElementById("status");
const emptyState = document.getElementById("empty-state");
const preview = document.getElementById("file-preview");

const drawer = document.getElementById("drawer");
const overlay = document.getElementById("overlay");

let selectedImage = null;
let selectedFile = null;
let liveVoice = false;
let isListening = false;
let isSpeaking = false;
let isSending = false;
let recognition = null;
let manualStop = false;
let voiceRestartTimer = null;

let currentAbortController = null;

marked.setOptions({
    breaks: true,
    gfm: true
});

function setStatus(text) {
    statusElement.textContent = text;
}

function scrollBottom() {
    chat.scrollTop = chat.scrollHeight;
}

function openDrawer() {
    drawer.classList.add("open");
    overlay.classList.add("show");
}

function closeDrawer() {
    drawer.classList.remove("open");
    overlay.classList.remove("show");
}

document.getElementById("drawer-toggle").onclick = openDrawer;
overlay.onclick = closeDrawer;

function newChat() {
    if (currentAbortController) {
        currentAbortController.abort();
        currentAbortController = null;
    }

    stopLiveVoice();

    chat.innerHTML = "";
    selectedImage = null;
    selectedFile = null;
    updatePreview();

    emptyState.style.display = "flex";
    chat.appendChild(emptyState);

    input.value = "";
    resizeTextarea();
    updateActionButton();

    setStatus("Yangi suhbat boshlandi.");
    closeDrawer();
}

document.getElementById("new-chat").onclick = newChat;
document.getElementById("drawer-new").onclick = newChat;

document.getElementById("clear-history").onclick = async () => {
    const confirmed = confirm(
        "Barcha suhbat tarixini o'chirishni xohlaysizmi?"
    );

    if (!confirmed) return;

    try {
        const response = await fetch("/api/clear", {
            method: "POST"
        });

        if (!response.ok) {
            throw new Error("Tarixni o'chirib bo'lmadi.");
        }

        newChat();
        setStatus("Suhbat tarixi tozalandi.");

    } catch (error) {
        setStatus(error.message);
    }

    closeDrawer();
};

plusButton.onclick = () => {
    plusMenu.classList.toggle("show");
};

document.addEventListener("click", (event) => {
    if (
        !plusMenu.contains(event.target) &&
        !plusButton.contains(event.target)
    ) {
        plusMenu.classList.remove("show");
    }
});

document.getElementById("upload-image").onclick = () => {
    plusMenu.classList.remove("show");
    document.getElementById("image-input").click();
};

document.getElementById("upload-file").onclick = () => {
    plusMenu.classList.remove("show");
    document.getElementById("file-input").click();
};

function updatePreview() {
    preview.innerHTML = "";

    const items = [];

    if (selectedImage) {
        items.push({
            label: "📷 Rasm tayyor",
            remove: () => selectedImage = null
        });
    }

    if (selectedFile) {
        items.push({
            label: "📎 " + selectedFile.name,
            remove: () => selectedFile = null
        });
    }

    if (items.length === 0) {
        preview.style.display = "none";
        return;
    }

    preview.style.display = "flex";

    items.forEach(item => {
        const element = document.createElement("div");
        element.className = "preview-item";

        const text = document.createElement("span");
        text.textContent = item.label;

        const remove = document.createElement("button");
        remove.textContent = "✕";
        remove.onclick = () => {
            item.remove();
            updatePreview();
            updateActionButton();
        };

        element.appendChild(text);
        element.appendChild(remove);
        preview.appendChild(element);
    });
}

function resizeImage(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();

        reader.onload = () => {
            const image = new Image();

            image.onload = () => {
                const maxSize = 1024;

                let width = image.width;
                let height = image.height;

                if (width > maxSize || height > maxSize) {
                    const scale = Math.min(
                        maxSize / width,
                        maxSize / height
                    );

                    width = Math.round(width * scale);
                    height = Math.round(height * scale);
                }

                const canvas = document.createElement("canvas");

                canvas.width = width;
                canvas.height = height;

                const context = canvas.getContext("2d");

                context.drawImage(
                    image,
                    0,
                    0,
                    width,
                    height
                );

                resolve(
                    canvas.toDataURL("image/jpeg", 0.82)
                );
            };

            image.onerror = () => reject(
                new Error("Rasmni ochib bo'lmadi.")
            );

            image.src = reader.result;
        };

        reader.onerror = () => reject(
            new Error("Rasmni o'qib bo'lmadi.")
        );

        reader.readAsDataURL(file);
    });
}

document.getElementById("image-input").onchange = async event => {
    const file = event.target.files[0];

    if (!file) return;

    if (!file.type.startsWith("image/")) {
        setStatus("Faqat rasm fayllari mumkin.");
        return;
    }

    try {
        setStatus("Rasm tayyorlanmoqda...");

        selectedImage = await resizeImage(file);

        updatePreview();
        updateActionButton();

        setStatus("Rasm yuklashga tayyor.");

    } catch (error) {
        setStatus(error.message);
    }

    event.target.value = "";
};

document.getElementById("file-input").onchange = event => {
    const file = event.target.files[0];

    if (!file) return;

    if (file.size > 15 * 1024 * 1024) {
        setStatus("Fayl 15 MB dan katta bo'lmasligi kerak.");
        event.target.value = "";
        return;
    }

    selectedFile = file;

    updatePreview();
    updateActionButton();

    setStatus("Fayl tayyor.");

    event.target.value = "";
};

function resizeTextarea() {
    input.style.height = "auto";

    input.style.height = Math.min(
        input.scrollHeight,
        150
    ) + "px";
}

function updateActionButton() {
    const hasContent =
        input.value.trim().length > 0 ||
        selectedImage !== null ||
        selectedFile !== null;

    sendButton.classList.toggle("visible", hasContent);
    voiceButton.style.display = hasContent ? "none" : "grid";
}

input.addEventListener("input", () => {
    resizeTextarea();
    updateActionButton();
});

input.addEventListener("keydown", event => {
    if (
        event.key === "Enter" &&
        !event.shiftKey &&
        !event.isComposing
    ) {
        event.preventDefault();
        sendMessage();
    }
});

sendButton.onclick = sendMessage;

function addUserMessage(text, image) {
    emptyState.style.display = "none";

    const wrapper = document.createElement("div");
    wrapper.className = "message user-message";

    const bubble = document.createElement("div");
    bubble.className = "user-bubble";

    if (text) {
        const content = document.createElement("div");
        content.textContent = text;
        bubble.appendChild(content);
    }

    if (image) {
        const img = document.createElement("img");
        img.src = image;
        img.style.maxWidth = "100%";
        img.style.maxHeight = "280px";
        img.style.borderRadius = "12px";
        img.style.marginTop = text ? "10px" : "0";
        bubble.appendChild(img);
    }

    if (selectedFile) {
        const fileLabel = document.createElement("div");
        fileLabel.style.marginTop = "8px";
        fileLabel.textContent = "📎 " + selectedFile.name;
        bubble.appendChild(fileLabel);
    }

    wrapper.appendChild(bubble);
    chat.appendChild(wrapper);

    scrollBottom();
}

function createAIMessage() {
    emptyState.style.display = "none";

    const wrapper = document.createElement("div");
    wrapper.className = "message ai-message";

    const content = document.createElement("div");
    content.className = "ai-content";

    const tools = document.createElement("div");
    tools.className = "ai-tools";

    const readButton = document.createElement("button");
    readButton.textContent = "🔊 Ovoz chiqarish";

    const copyButton = document.createElement("button");
    copyButton.textContent = "📋 Nusxalash";

    tools.appendChild(readButton);
    tools.appendChild(copyButton);

    wrapper.appendChild(content);
    wrapper.appendChild(tools);

    chat.appendChild(wrapper);

    let fullText = "";

    readButton.onclick = () => {
        if (!fullText) return;

        speechSynthesis.cancel();

        const utterance = new SpeechSynthesisUtterance(
            fullText.replace(/[#*`]/g, "")
        );

        utterance.lang = "uz-UZ";
        utterance.rate = 1;

        speechSynthesis.speak(utterance);
    };

    copyButton.onclick = async () => {
        try {
            await navigator.clipboard.writeText(fullText);

            copyButton.textContent = "✓ Nusxalandi";

            setTimeout(() => {
                copyButton.textContent = "📋 Nusxalash";
            }, 1500);

        } catch {
            setStatus("Nusxalash amalga oshmadi.");
        }
    };

    return {
        content,
        setText(text) {
            fullText = text;

            try {
                content.innerHTML = marked.parse(text);
            } catch {
                content.textContent = text;
            }

            scrollBottom();
        },
        append(text) {
            fullText += text;

            try {
                content.innerHTML = marked.parse(fullText);
            } catch {
                content.textContent = fullText;
            }

            scrollBottom();
        },
        getText() {
            return fullText;
        }
    };
}

async function sendMessage(voiceTriggered = false) {
    if (isSending) return;

    const message = input.value.trim();

    const image = selectedImage;
    const file = selectedFile;

    if (!message && !image && !file) return;

    isSending = true;

    if (currentAbortController) {
        currentAbortController.abort();
    }

    currentAbortController = new AbortController();

    addUserMessage(message, image);

    input.value = "";
    resizeTextarea();

    selectedImage = null;
    selectedFile = null;

    updatePreview();
    updateActionButton();

    const ai = createAIMessage();

    const loading = document.createElement("div");
    loading.className = "typing";
    loading.innerHTML = "<span></span><span></span><span></span>";

    ai.content.appendChild(loading);

    setStatus("Super-Agent javob tayyorlamoqda...");

    try {
        const formData = new FormData();

        formData.append("message", message);

        if (image) {
            formData.append("image", image);
        }

        if (file) {
            formData.append("file", file, file.name);
        }

        const response = await fetch("/api/chat", {
            method: "POST",
            body: formData,
            signal: currentAbortController.signal
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({
                detail: "Server xatosi."
            }));

            throw new Error(
                error.detail || "AI server xatosi."
            );
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();

        let buffer = "";
        let received = false;

        ai.content.innerHTML = "";

        while (true) {
            const { value, done } = await reader.read();

            if (done) break;

            buffer += decoder.decode(
                value,
                { stream: true }
            );

            const events = buffer.split("\n\n");

            buffer = events.pop() || "";

            for (const event of events) {
                if (!event.startsWith("data: ")) continue;

                const payload = event.slice(6).trim();

                if (payload === "[DONE]") {
                    continue;
                }

                try {
                    const parsed = JSON.parse(payload);

                    if (parsed.error) {
                        throw new Error(parsed.error);
                    }

                    if (parsed.t !== undefined) {
                        ai.append(parsed.t);
                        received = true;
                    }

                } catch (error) {
                    if (
                        error.message &&
                        !error.message.includes("JSON")
                    ) {
                        throw error;
                    }
                }
            }
        }

        if (!received) {
            ai.setText("Javob olishning imkoni bo'lmadi.");
        }

        setStatus("Javob tayyor.");

        if (voiceTriggered && liveVoice && ai.getText()) {
            await speakAndContinue(ai.getText());
        }

    } catch (error) {
        if (error.name !== "AbortError") {
            ai.setText(
                "⚠️ Xatolik: " + error.message
            );

            setStatus("Ulanishda xatolik.");
        }

    } finally {
        isSending = false;
        currentAbortController = null;
        scrollBottom();
    }
}


// ============================================================
// WEB SPEECH API: MICROPHONE
// ============================================================

const SpeechRecognition =
    window.SpeechRecognition ||
    window.webkitSpeechRecognition;

if (SpeechRecognition) {
    recognition = new SpeechRecognition();

    recognition.lang = "uz-UZ";
    recognition.continuous = false;
    recognition.interimResults = true;

    let micFinalText = "";

    recognition.onstart = () => {
        isListening = true;
        micButton.style.color = "#ff6878";
        setStatus("Sizni tinglayapman...");
    };

    recognition.onresult = event => {
        let interim = "";

        for (
            let i = event.resultIndex;
            i < event.results.length;
            i++
        ) {
            const text = event.results[i][0].transcript;

            if (event.results[i].isFinal) {
                micFinalText += text + " ";
            } else {
                interim += text;
            }
        }

        input.value = (
            micFinalText + interim
        ).trim();

        resizeTextarea();
        updateActionButton();
    };

    recognition.onerror = event => {
        setStatus(
            "Mikrofon xatosi: " + event.error
        );
    };

    recognition.onend = () => {
        isListening = false;
        micButton.style.color = "";

        if (micFinalText.trim()) {
            input.value = micFinalText.trim();
            micFinalText = "";
            updateActionButton();
            sendMessage();
        }
    };

    micButton.onclick = () => {
        if (!recognition) return;

        micFinalText = "";

        try {
            recognition.start();
        } catch {
            setStatus("Mikrofon allaqachon ishlayapti.");
        }
    };

} else {
    micButton.onclick = () => {
        setStatus(
            "Brauzeringiz Speech Recognition xizmatini qo'llab-quvvatlamaydi."
        );
    };
}


// ============================================================
// LIVE VOICE CONVERSATION
// ============================================================

function initializeLiveRecognition() {
    if (!SpeechRecognition) {
        setStatus(
            "Live ovoz rejimi brauzeringizda mavjud emas."
        );

        return false;
    }

    if (recognition) {
        try {
            recognition.abort();
        } catch {}
    }

    recognition = new SpeechRecognition();

    recognition.lang = "uz-UZ";
    recognition.continuous = false;
    recognition.interimResults = true;

    let finalTranscript = "";

    recognition.onstart = () => {
        isListening = true;
        voiceButton.classList.add("active");
        setStatus("🎙️ Tinglayapman...");
    };

    recognition.onresult = event => {
        let interim = "";

        for (
            let i = event.resultIndex;
            i < event.results.length;
            i++
        ) {
            const transcript =
                event.results[i][0].transcript;

            if (event.results[i].isFinal) {
                finalTranscript += transcript + " ";
            } else {
                interim += transcript;
            }
        }

        input.value = (
            finalTranscript + interim
        ).trim();

        resizeTextarea();
        updateActionButton();
    };

    recognition.onerror = event => {
        if (liveVoice) {
            setStatus(
                "Ovoz aniqlash xatosi: " + event.error
            );
        }
    };

    recognition.onend = async () => {
        isListening = false;

        if (!liveVoice || manualStop) return;

        const text = finalTranscript.trim();

        finalTranscript = "";

        if (text) {
            input.value = text;
            updateActionButton();

            await sendMessage(true);

        } else if (!isSpeaking && !isSending) {
            scheduleVoiceRestart();
        }
    };

    return true;
}

function scheduleVoiceRestart() {
    clearTimeout(voiceRestartTimer);

    if (!liveVoice || isSpeaking || isSending) return;

    voiceRestartTimer = setTimeout(() => {
        if (!liveVoice || isSpeaking || isSending) return;

        try {
            recognition.start();
        } catch {
            scheduleVoiceRestart();
        }

    }, 700);
}

function speakAndContinue(text) {
    return new Promise(resolve => {
        if (!liveVoice) {
            resolve();
            return;
        }

        isSpeaking = true;

        setStatus("🔊 Javobni ovoz chiqarib aytyapman...");

        const cleanText = text
            .replace(/```[\s\S]*?```/g, "Kod mavjud.")
            .replace(/[#*`_~]/g, "")
            .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
            .slice(0, 12000);

        const utterance = new SpeechSynthesisUtterance(
            cleanText
        );

        utterance.lang = "uz-UZ";
        utterance.rate = 1;
        utterance.pitch = 1;

        utterance.onend = () => {
            isSpeaking = false;
            resolve();

            if (liveVoice) {
                scheduleVoiceRestart();
            }
        };

        utterance.onerror = () => {
            isSpeaking = false;
            resolve();

            if (liveVoice) {
                scheduleVoiceRestart();
            }
        };

        speechSynthesis.cancel();
        speechSynthesis.speak(utterance);
    });
}

function startLiveVoice() {
    if (!SpeechRecognition) {
        setStatus(
            "Brauzeringiz jonli ovoz rejimini qo'llab-quvvatlamaydi."
        );

        return;
    }

    if (!initializeLiveRecognition()) return;

    liveVoice = true;
    manualStop = false;

    voiceButton.classList.add("active");

    setStatus("Live Voice faol. Sizni tinglayapman...");

    try {
        recognition.start();
    } catch {
        scheduleVoiceRestart();
    }
}

function stopLiveVoice() {
    liveVoice = false;
    manualStop = true;

    clearTimeout(voiceRestartTimer);

    voiceButton.classList.remove("active");

    isListening = false;
    isSpeaking = false;

    speechSynthesis.cancel();

    if (recognition) {
        try {
            recognition.abort();
        } catch {}
    }

    setStatus("Live Voice o'chirildi.");
}

voiceButton.onclick = () => {
    if (liveVoice) {
        stopLiveVoice();
    } else {
        startLiveVoice();
    }
};


// ============================================================
// INITIALIZATION
// ============================================================

async function loadHistory() {
    try {
        const response = await fetch("/api/history");

        if (!response.ok) return;

        const data = await response.json();

        if (!data.history || data.history.length === 0) {
            return;
        }

        emptyState.style.display = "none";

        for (const item of data.history) {
            if (item.role === "user") {
                const wrapper = document.createElement("div");
                wrapper.className = "message user-message";

                const bubble = document.createElement("div");
                bubble.className = "user-bubble";
                bubble.textContent = item.content;

                wrapper.appendChild(bubble);
                chat.appendChild(wrapper);

            } else if (item.role === "assistant") {
                const ai = createAIMessage();
                ai.setText(item.content);
            }
        }

        scrollBottom();

    } catch (error) {
        console.error("History error:", error);
    }
}

window.addEventListener("beforeunload", () => {
    if (currentAbortController) {
        currentAbortController.abort();
    }

    speechSynthesis.cancel();
});

updateActionButton();
loadHistory();

</script>

</body>
</html>
"""


# ============================================================
# ROUTES
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(
        content=HTML_PAGE,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"
        }
    )


@app.get("/api/history")
async def history_endpoint():
    return JSONResponse({
        "success": True,
        "history": get_history(MAX_HISTORY)
    })


@app.post("/api/clear")
async def clear_endpoint():
    success = clear_history()

    if not success and DATABASE_URL:
        return JSONResponse(
            {
                "success": False,
                "error": "Database bilan bog'lanib bo'lmadi."
            },
            status_code=503
        )

    return JSONResponse({
        "success": True,
        "message": "Suhbat tarixi tozalandi."
    })


@app.get("/api/health")
async def health_endpoint():
    return {
        "status": "online",
        "application": "Super-Agent",
        "version": "1.0.0",
        "time": datetime.now(timezone.utc).isoformat(),
        "database_configured": bool(DATABASE_URL),
        "gemini_configured": bool(GEMINI_API_KEY),
        "openrouter_configured": bool(OPENROUTER_API_KEY),
        "models": get_available_models()
    }


# ============================================================
# CHAT ENDPOINT WITH SSE STREAMING
# ============================================================

@app.post("/api/chat")
async def chat_endpoint(
    request: Request,
    message: str = Form(""),
    image: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None)
):
    message = message.strip()

    if len(message) > 30000:
        return JSONResponse(
            {
                "error": "Xabar hajmi juda katta."
            },
            status_code=413
        )

    if not message and not image and not file:
        return JSONResponse(
            {
                "error": "Xabar, rasm yoki fayl yuboring."
            },
            status_code=400
        )

    # --------------------------------------------------------
    # Validate image
    # --------------------------------------------------------

    try:
        image_url = validate_image_data(image) if image else None

    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=400
        )

    # --------------------------------------------------------
    # Extract uploaded document
    # --------------------------------------------------------

    document_text = ""

    if file:
        try:
            filename = file.filename or "uploaded_file"

            file_data = await file.read(
                MAX_FILE_SIZE + 1
            )

            if len(file_data) > MAX_FILE_SIZE:
                return JSONResponse(
                    {
                        "error": "Fayl hajmi 15 MB dan oshmasligi kerak."
                    },
                    status_code=413
                )

            document_text = extract_document_text(
                filename,
                file_data
            )

        except ValueError as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=400
            )

        except Exception as exc:
            logger.exception("File processing error: %s", exc)

            return JSONResponse(
                {
                    "error": "Faylni o'qishda xatolik yuz berdi."
                },
                status_code=400
            )

        finally:
            await file.close()

    # --------------------------------------------------------
    # Retrieve memory
    # --------------------------------------------------------

    memory_items = retrieve_memory(message, limit=5)

    memory_context = ""

    if memory_items:
        memory_lines = []

        for item in memory_items:
            memory_lines.append(
                f"{item['role']}: {item['content'][:1500]}"
            )

        memory_context = (
            "\n\nOLDINGI SUHBATLARDAN TOPILGAN XOTIRA:\n"
            + "\n".join(memory_lines)
        )

    # --------------------------------------------------------
    # Web search
    # --------------------------------------------------------

    web_context = ""

    if message and should_search_web(message):
        logger.info("Web search triggered.")

        search_results = await perform_web_search(message)

        if search_results:
            web_context = (
                "\n\nINTERNET QIDIRUVI NATIJALARI:\n"
                "Quyidagi natijalardan foydalan. "
                "Ularning sanasi va ishonchliligiga e'tibor ber. "
                "Manbalarni ko'rsat.\n"
                + search_results
            )

    # --------------------------------------------------------
    # Build user message
    # --------------------------------------------------------

    user_content = []

    text_parts = []

    if message:
        text_parts.append(message)

    if document_text:
        text_parts.append(
            "\n\nYUKLANGAN FAYL MAZMUNI:\n"
            + document_text
        )

    if memory_context:
        text_parts.append(memory_context)

    if web_context:
        text_parts.append(web_context)

    combined_text = "\n".join(text_parts)

    if combined_text:
        user_content.append({
            "type": "text",
            "text": combined_text
        })

    if image_url:
        user_content.append({
            "type": "image_url",
            "image_url": {
                "url": image_url
            }
        })

    # --------------------------------------------------------
    # Prepare model messages
    # --------------------------------------------------------

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        },
        {
            "role": "user",
            "content": (
                user_content
                if len(user_content) > 1 or image_url
                else combined_text
            )
        }
    ]

    # --------------------------------------------------------
    # Save user message
    # --------------------------------------------------------

    saved_user_message = message or "Yuborilgan fayl yoki rasm"

    if file:
        saved_user_message += f"\n[Fayl: {filename}]"

    if image_url:
        saved_user_message += "\n[Rasm yuborildi]"

    await asyncio.to_thread(
        save_message,
        "user",
        saved_user_message
    )

    # --------------------------------------------------------
    # Stream response
    # --------------------------------------------------------

    async def generate():
        full_response = ""

        try:
            response, model = await create_ai_response(
                messages,
                stream=True
            )

            logger.info(
                "Streaming started with model: %s",
                model
            )

            async for chunk in response:
                if await request.is_disconnected():
                    logger.info("Client disconnected.")
                    return

                try:
                    # CRITICAL: Strict streaming delta access.
                    delta = chunk.choices[0].delta.content

                except (AttributeError, IndexError, TypeError):
                    delta = None

                if delta:
                    if not isinstance(delta, str):
                        continue

                    full_response += delta

                    yield sse_message({
                        "t": delta
                    })

            # ------------------------------------------------
            # Python execution
            # ------------------------------------------------

            python_code = extract_python_code(full_response)

            if python_code:
                logger.info("Python execution requested.")

                yield sse_message({
                    "t": "\n\n**Python kod bajarilmoqda...**\n\n"
                })

                execution_output = await asyncio.to_thread(
                    execute_python_code,
                    python_code
                )

                explanation = await create_explanation(
                    python_code,
                    execution_output
                )

                appended_result = (
                    "\n\n---\n\n"
                    "**Python bajarilish natijasi:**\n\n"
                    "```text\n"
                    f"{execution_output[:12000]}\n"
                    "```\n\n"
                    "**Natija izohi:**\n\n"
                    f"{explanation}"
                )

                full_response += appended_result

                yield sse_message({
                    "t": appended_result
                })

            # ------------------------------------------------
            # Persist assistant response
            # ------------------------------------------------

            await asyncio.to_thread(
                save_message,
                "assistant",
                full_response
            )

            yield sse_done()

        except asyncio.CancelledError:
            logger.info("Generation cancelled.")
            raise

        except Exception as exc:
            logger.exception(
                "AI generation error: %s",
                exc
            )

            error_message = (
                "\n\n⚠️ **Xatolik:** "
                "Hozircha javob yaratib bo'lmadi. "
                "API kalitlari va internet aloqasini tekshiring."
            )

            yield sse_message({
                "t": error_message
            })

            yield sse_done()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*"
        }
    )


# ============================================================
# APPLICATION ENTRY POINT
# ============================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        access_log=True,
        proxy_headers=True,
        forwarded_allow_ips="*"
        )
