import os
import io
import re
import asyncio
import subprocess
import tempfile
import psycopg2

from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from litellm import acompletion
from duckduckgo_search import DDGS
from pypdf import PdfReader
from docx import Document
import openpyxl

app = FastAPI(title="Super-Agent AI")

G_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
O_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
DB = os.environ.get("DATABASE_URL", "").strip()


# ==============================
# SYSTEM PROMPT
# ==============================

SYS = """
Sen Super-Agent AI yordamchisisan.
Foydalanuvchining aqlli, mustaqil va dangal fikrlaydigan hamkorisan.

Senda 6 ta modul mavjud:

1. Agent System — vazifalarni rejalashtirish.
2. Long-Term Memory — RAG uzoq xotira.
3. Code Interpreter — Python kodlarini hisoblash va tekshirish.
4. File Intelligence — PDF, DOCX, XLSX tahlili.
5. Autonomous Worker — murakkab vazifalarni bajarish.
6. Self-Evaluation — javoblarni tekshirish.

Qoidalar:
- Foydalanuvchiga o'zbek tilida javob ber.
- Kerak bo'lsa boshqa tillardan ham foydalan.
- Aniq va tushunarli javob ber.
- Bilmagan narsangni to'qima.
- Internet ma'lumotlaridan foydalan.
- Manbalarni ko'rsat.
- Rasmdagi xira joylarni to'qima.
- Kod kerak bo'lsa [PY]kod[/PY] formatidan foydalan.
"""


# ==============================
# DATABASE
# ==============================

def db(sql, params=(), fetch=False):

    if not DB:
        return [] if fetch else None

    try:
        conn = psycopg2.connect(DB)

        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

                result = cur.fetchall() if fetch else None

        conn.close()
        return result

    except Exception as e:
        print("DATABASE ERROR:", e)
        return [] if fetch else None


db("""
CREATE TABLE IF NOT EXISTS mind_chat (
    id SERIAL PRIMARY KEY,
    role TEXT,
    content TEXT
);
""")


# ==============================
# FILE INTELLIGENCE
# ==============================

def read_file(name, data):

    ext = name.lower().split(".")[-1]

    try:

        if ext == "pdf":

            pdf = PdfReader(io.BytesIO(data))

            return "\n".join(
                page.extract_text() or ""
                for page in pdf.pages
            )[:12000]

        elif ext == "docx":

            doc = Document(io.BytesIO(data))

            return "\n".join(
                p.text for p in doc.paragraphs
            )[:12000]

        elif ext == "xlsx":

            wb = openpyxl.load_workbook(
                io.BytesIO(data),
                data_only=True
            )

            output = []

            for sheet in wb.sheetnames[:3]:

                for row in list(
                    wb[sheet].iter_rows(values_only=True)
                )[:60]:

                    output.append(
                        " | ".join(
                            str(x)
                            for x in row
                            if x is not None
                        )
                    )

            return "\n".join(output)[:12000]

        else:

            return data.decode(
                "utf-8",
                errors="ignore"
            )[:12000]

    except Exception as e:

        return f"Fayl xatosi: {e}"


# ==============================
# PYTHON CODE INTERPRETER
# ==============================

def run_py(code):

    path = None

    try:

        with tempfile.NamedTemporaryFile(
            "w",
            suffix=".py",
            delete=False
        ) as tf:

            tf.write(code)
            path = tf.name

        result = subprocess.run(
            ["python3", path],
            capture_output=True,
            text=True,
            timeout=8
        )

        return (
            result.stdout + "\n" + result.stderr
        ).strip()[:3000] or "Bajarildi."

    except Exception as e:

        return f"Kod xatosi: {e}"

    finally:

        if path and os.path.exists(path):
            os.remove(path)


# ==============================
# INTERNET SEARCH
# ==============================

def web_search(query):

    try:

        results = DDGS().text(
            query,
            max_results=6
        )

        return "\n\n".join(

            f"""
[{i}]
Sarlavha: {r.get('title')}
URL: {r.get('href')}
Ma'lumot: {r.get('body')}
"""

            for i, r in enumerate(results, 1)

        ) if results else ""

    except Exception as e:

        print("SEARCH ERROR:", e)
        return ""


# ==============================
# RAG MEMORY
# ==============================

def rag_memory(query):

    words = [
        w for w in re.findall(
            r"\w+",
            query.lower()
        )
        if len(w) > 3
    ][:4]

    if not words:
        return ""

    rows = db(
        """
        SELECT role, content
        FROM mind_chat
        ORDER BY id DESC
        LIMIT 100
        """,
        fetch=True
    ) or []

    hits = []

    for role, content in rows:

        if any(
            word in content.lower()
            for word in words
        ):

            hits.append(
                f"{role}: {content[:300]}"
            )

        if len(hits) >= 5:
            break

    return "\n".join(hits)


# ==============================
# AI ENGINE
# ==============================

async def ask(messages):

    # GEMINI

    if G_KEY:

        try:

            response = await acompletion(
                model="gemini/gemini-2.5-flash",
                messages=messages,
                api_key=G_KEY,
                timeout=35
            )

            choice = response.choices[0]

            if choice.message.content:
                return choice.message.content

        except Exception as e:

            print("GEMINI ERROR:", e)

    # OPENROUTER FALLBACK

    if O_KEY:

        models = [
            "openrouter/deepseek/deepseek-r1-0528:free",
            "openrouter/meta-llama/llama-3.3-70b-instruct:free",
            "openrouter/google/gemma-3-27b-it:free",
            "openrouter/openrouter/free"
        ]

        for model in models:

            try:

                response = await acompletion(
                    model=model,
                    messages=messages,
                    api_key=O_KEY,
                    timeout=30
                )

                choice = response.choices[0]

                if choice.message.content:
                    return choice.message.content

            except Exception as e:

                print(
                    f"OPENROUTER {model}:",
                    e
                )

    return None


# ==============================
# CHAT API
# ==============================

@app.post("/api/chat")
async def chat(

    message: str = Form(""),
    image: str = Form(""),
    file: Optional[UploadFile] = File(None)

):

    txt = message.strip()
    file_ctx = ""

    # FILE

    if file and file.filename:

        raw = await file.read()

        file_ctx = f"""
YUKLANGAN FAYL: {file.filename}

{read_file(file.filename, raw)}
"""

    if not txt and not image and not file_ctx:

        return JSONResponse(
            {"error": "Xabar, rasm yoki fayl yuboring"},
            400
        )

    # SAVE USER MESSAGE

    db(
        """
        INSERT INTO mind_chat(role, content)
        VALUES(%s, %s)
        """,
        (
            "user",
            txt
        )
    )

    # IMAGE ANALYSIS

    if image:

        prompt = txt or (
            "Rasmdagi ma'lumotlarni tahlil qil. "
            "Xira joylarini to'qima."
        )

        answer = await ask([

            {
                "role": "system",
                "content": SYS
            },

            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image
                        }
                    }
                ]
            }

        ])

        if not answer:

            return JSONResponse(
                {"error": "Rasm tahlilida xatolik."},
                500
            )

        db(
            """
            INSERT INTO mind_chat(role, content)
            VALUES(%s, %s)
            """,
            ("assistant", answer)
        )

        return {"reply": answer}

    # INTERNET + MEMORY

    inet, mem = await asyncio.gather(

        asyncio.to_thread(web_search, txt),

        asyncio.to_thread(rag_memory, txt)

    )

    full_sys = SYS

    if mem:

        full_sys += f"""

OLDINGI XOTIRA:

{mem}
"""

    if inet:

        full_sys += f"""

JONLI INTERNET NATIJALARI:

{inet}
"""

    if file_ctx:

        full_sys += f"""

FAYL MA'LUMOTLARI:

{file_ctx}
"""

    # CHAT HISTORY

    history = db(
        """
        SELECT role, content
        FROM mind_chat
        ORDER BY id DESC
        LIMIT 10
        """,
        fetch=True
    ) or []

    history = [
        {
            "role": role,
            "content": content
        }

        for role, content in reversed(history)
    ]

    messages = [
        {
            "role": "system",
            "content": full_sys
        }
    ] + history

    answer = await ask(messages)

    if not answer:

        return JSONResponse(
            {
                "error": "API kalit yoki tarmoqda uzilish bor."
            },
            500
        )

    # PYTHON EXECUTION

    py_match = re.search(
        r"\[PY\](.*?)\[/PY\]",
        answer,
        re.S
    )

    if py_match:

        output = await asyncio.to_thread(
            run_py,
            py_match.group(1).strip()
        )

        messages.append({
            "role": "assistant",
            "content": answer
        })

        messages.append({
            "role": "user",
            "content": f"""
Python natijasi:

{output}

Endi natija asosida yakuniy javob ber.
Ichki PY teglarini ko'rsatma.
"""
        })

        answer = await ask(messages) or answer

    # SAVE AI RESPONSE

    db(
        """
        INSERT INTO mind_chat(role, content)
        VALUES(%s, %s)
        """,
        ("assistant", answer)
    )

    return {"reply": answer}


# ==============================
# CHAT HISTORY
# ==============================

@app.get("/api/history")
async def history():

    rows = db(
        """
        SELECT role, content
        FROM mind_chat
        ORDER BY id DESC
        LIMIT 30
        """,
        fetch=True
    ) or []

    return {
        "messages": [
            {
                "role": r,
                "content": c
            }
            for r, c in reversed(rows)
        ]
    }


# ==============================
# CLEAR HISTORY
# ==============================

@app.post("/api/clear")
async def clear():

    db("DELETE FROM mind_chat")

    return {"status": "ok"}


# ==============================
# ONE-FILE FRONTEND
# ==============================

@app.get("/", response_class=HTMLResponse)
async def home():

    return r"""
<!DOCTYPE html>
<html lang="uz">

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width, initial-scale=1">

<title>Super-Agent AI</title>

<style>

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    font-family: sans-serif;
}

body {
    background: #090d16;
    color: white;
    display: flex;
    flex-direction: column;
    height: 100dvh;
}

header {
    background: #111827;
    padding: 14px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #1f2937;
}

h1 {
    font-size: 17px;
    color: #38bdf8;
}

button {
    cursor: pointer;
}

.t {
    background: #1f2937;
    color: white;
    border: 1px solid #374151;
    padding: 7px 12px;
    border-radius: 8px;
}

#chat {
    flex: 1;
    overflow-y: auto;
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.msg {
    max-width: 88%;
    padding: 12px 14px;
    border-radius: 14px;
    line-height: 1.5;
    font-size: 15px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
}

.msg a {
    color: #38bdf8;
    text-decoration: underline;
}

.user {
    background: #2563eb;
    align-self: flex-end;
}

.assistant {
    background: #111827;
    border: 1px solid #1f2937;
    align-self: flex-start;
}

.sb {
    margin-top: 8px;
    background: #1f2937;
    color: #94a3b8;
    border: 1px solid #374151;
    padding: 5px 9px;
    border-radius: 6px;
}

#pv {
    background: #111827;
    padding: 8px 14px;
    display: none;
    justify-content: space-between;
    color: #38bdf8;
}

.bar {
    background: #111827;
    padding: 9px;
    display: flex;
    gap: 6px;
    align-items: flex-end;
    border-top: 1px solid #1f2937;
}

textarea {
    flex: 1;
    background: #090d16;
    color: white;
    border: 1px solid #374151;
    border-radius: 10px;
    padding: 11px;
    font-size: 15px;
    resize: none;
    max-height: 110px;
    min-width: 0;
}

.b {
    background: #1f2937;
    color: white;
    border: 1px solid #374151;
    width: 39px;
    height: 39px;
    border-radius: 10px;
    flex-shrink: 0;
}

.snd {
    background: #10b981;
    font-weight: bold;
}

button:disabled {
    opacity: .5;
}

</style>

</head>

<body>

<header>

<h1>🧠 Super-Agent AI</h1>

<button class="t" onclick="clr()">
🗑️
</button>

</header>

<div id="chat"></div>

<div id="pv">

<span id="pn"></span>

<button class="t" onclick="rm()">
❌
</button>

</div>

<div class="bar">

<input type="file"
id="fi"
accept="image/*"
hidden
onchange="hi(event)">

<input type="file"
id="ff"
accept=".pdf,.docx,.xlsx,.txt,.py,.html"
hidden
onchange="hf(event)">

<button class="b"
onclick="fi.click()">📷</button>

<button class="b"
onclick="ff.click()">📎</button>

<textarea
id="ip"
rows="1"
placeholder="Xabaringizni yozing...">
</textarea>

<button class="b snd"
id="sd"
onclick="sn()">➤</button>

</div>

<script>

let img = null;
let doc = null;

function fmt(s) {

    return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(
        /(https?:\/\/[^\s<]+)/g,
        '<a href="$1" target="_blank" rel="noopener">$1</a>'
    );

}

function add(role, text) {

    const d = document.createElement("div");

    d.className = "msg " + role;

    const c = document.createElement("div");

    c.innerHTML = fmt(text);

    d.appendChild(c);

    if (role === "assistant") {

        const b = document.createElement("button");

        b.className = "sb";

        b.textContent = "🔊 Eshitish";

        b.onclick = () => {

            speechSynthesis.cancel();

            const u = new SpeechSynthesisUtterance(text);

            u.lang = "uz-UZ";

            speechSynthesis.speak(u);

        };

        d.appendChild(b);

    }

    chat.appendChild(d);

    chat.scrollTop = chat.scrollHeight;

    return d;

}

function hi(e) {

    const f = e.target.files[0];

    if (!f) return;

    const reader = new FileReader();

    reader.onload = v => {

        const i = new Image();

        i.onload = () => {

            const c = document.createElement("canvas");

            const scale = Math.min(
                1,
                1024 / i.width,
                1024 / i.height
            );

            c.width = i.width * scale;

            c.height = i.height * scale;

            c.getContext("2d").drawImage(
                i, 0, 0, c.width, c.height
            );

            img = c.toDataURL("image/jpeg", .85);

            pv.style.display = "flex";

            pn.textContent = "📷 " + f.name;

        };

        i.src = v.target.result;

    };

    reader.readAsDataURL(f);

}

function hf(e) {

    doc = e.target.files[0];

    if (doc) {

        pv.style.display = "flex";

        pn.textContent = "📎 " + doc.name;

    }

}

function rm() {

    img = null;

    doc = null;

    fi.value = "";

    ff.value = "";

    pv.style.display = "none";

}

async function ld() {

    try {

        const d = await (
            await fetch("/api/history")
        ).json();

        chat.innerHTML = "";

        if (d.messages && d.messages.length) {

            d.messages.forEach(m => {
                add(m.role, m.content);
            });

        } else {

            add(
                "assistant",
                "Salom! Super-Agent AI ishga tushdi. " +
                "Savolingizni yuboring!"
            );

        }

    } catch(e) {

        console.log(e);

    }

}

async function sn() {

    const t = ip.value.trim();

    if (!t && !img && !doc) return;

    const fd = new FormData();

    fd.append("message", t);

    if (img) fd.append("image", img);

    if (doc) fd.append("file", doc);

    ip.value = "";

    add(
        "user",
        (img ? "📷 " : "") +
        (doc ? "📎 " + doc.name + " " : "") +
        t
    );

    rm();

    const wait = add(
        "assistant",
        "⚡ Tahlil qilyapman..."
    );

    sd.disabled = true;

    try {

        const response = await fetch(
            "/api/chat",
            {
                method: "POST",
                body: fd
            }
        );

        const data = await response.json();

        wait.remove();

        add(
            "assistant",
            data.reply || ("❌ " + data.error)
        );

    } catch(e) {

        wait.textContent = "Server bilan aloqa uzildi.";

    }

    sd.disabled = false;

}

async function clr() {

    if (!confirm("Barcha suhbatlar o'chirilsinmi?")) {
        return;
    }

    await fetch(
        "/api/clear",
        { method: "POST" }
    );

    ld();

}

ip.addEventListener("keydown", e => {

    if (e.key === "Enter" && !e.shiftKey) {

        e.preventDefault();

        sn();

    }

});

ld();

</script>

</body>
</html>
"""


# ==============================
# START SERVER
# ==============================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000))
        )
