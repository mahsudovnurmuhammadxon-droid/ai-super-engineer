import os, json, subprocess, tempfile, traceback
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import litellm
import psycopg2
from psycopg2.extras import RealDictCursor
from upstash_redis import Redis
from duckduckgo_search import DDGS

app = FastAPI(title="Personal Autonomous AI Engineer")

# 1. XOTIRA VA BAZALARNI ULASH
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

redis_client = Redis(url=REDIS_URL, token=REDIS_TOKEN) if REDIS_URL and REDIS_TOKEN else None

def init_db():
    if not DATABASE_URL:
        return
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            title TEXT,
            prompt TEXT,
            plan TEXT,
            code TEXT,
            test_result TEXT,
            model_used TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS error_logs (
            id SERIAL PRIMARY KEY,
            error_text TEXT,
            solution TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print("DB init warning:", e)

# 2. KO'P MODELLI AI MIYA (AVTO-FALLBACK VA ROUTING)
FREE_MODELS = [
        "openrouter/openrouter/free",
]



def ask_ai_brain(messages, preferred_model="auto"):
    models_to_try = FREE_MODELS if preferred_model == "auto" else [preferred_model] + FREE_MODELS
    last_err = ""
    for m in models_to_try:
        try:
            resp = litellm.completion(model=m, messages=messages, timeout=45)
            return resp.choices[0].message.content, m, resp.usage.total_tokens if resp.usage else 0
        except Exception as e:
            last_err = f"{m}: {str(e)}"
            continue
    raise Exception(f"Barcha bepul modellarda limit yoki xato: {last_err}")

# 3. JONLI INTERNET QIDIRUV
def web_search(query: str):
    try:
        results = DDGS().text(query, max_results=3)
        return "\n".join([f"- {r['title']}: {r['body']}" for r in results])
    except Exception as e:
        return f"Qidiruv xatosi: {e}"

# 4. XAVFSIZ SANDBOX TEST VA BANDIT XAVFSIZLIK TEKSHIRUVI
def run_sandbox_test(python_code: str):
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", encoding="utf-8") as f:
        f.write(python_code)
        temp_path = f.name
    try:
        sec = subprocess.run(["bandit", "-q", temp_path], capture_output=True, text=True, timeout=10)
        run = subprocess.run(["python3", temp_path], capture_output=True, text=True, timeout=15)
        passed = (run.returncode == 0)
        output = f"EXIT CODE: {run.returncode}\nSTDOUT:\n{run.stdout}\nSTDERR:\n{run.stderr}\nSECURITY:\n{sec.stdout or 'Xavfsiz'}"
        return passed, output
    except Exception as e:
        return False, str(e)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

class TaskRequest(BaseModel):
    prompt: str
    model: str = "auto"
    use_search: bool = True

@app.post("/api/engineer")
def autonomous_engineer(req: TaskRequest):
    search_context = web_search(req.prompt) if req.use_search else ""
    
    # 1-bosqich: Arxitektura va Reja
    plan_prompt = [
        {"role": "system", "content": "Sen bosh dasturchi va tizim arxitektorisan. O'zbek tilida qisqa va aniq texnik reja tuz."},
        {"role": "user", "content": f"Topshiriq: {req.prompt}\nInternet ma'lumotlari: {search_context}"}
    ]
    plan, used_model, tokens1 = ask_ai_brain(plan_prompt, req.model)

    # 2-bosqich: Kod yozish
    code_prompt = [
        {"role": "system", "content": "Faqat toza, ishlaydigan Python kodini yoz. Hech qanday markdown (```python) yoki izoh yozma, faqat sof kodni qaytar. Kod o'zini o'zi assert bilan test qilsin va oxirida 'TEST_PASSED' deb print qilsin."},
        {"role": "user", "content": f"Topshiriq: {req.prompt}\nReja: {plan}"}
    ]
    code, used_model, tokens2 = ask_ai_brain(code_prompt, req.model)
    code = code.replace("```python", "").replace("```", "").strip()

    # 3-bosqich: Avtomatik Test va Xatoni Tuzatish Sikli (3 urinish)
    passed, test_out = False, ""
    for attempt in range(3):
        passed, test_out = run_sandbox_test(code)
        if passed:
            break
        fix_prompt = [
            {"role": "system", "content": "Koddagi xatoni tuzat va faqat sof Python kodini qaytar (markdown belgilarisiz)."},
            {"role": "user", "content": f"Kod:\n{code}\n\nXato natijasi:\n{test_out}"}
        ]
        code, used_model, _ = ask_ai_brain(fix_prompt, req.model)
        code = code.replace("```python", "").replace("```", "").strip()

    # 4-bosqich: Doimiy Xotiraga Saqlash
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO projects (title, prompt, plan, code, test_result, model_used, status) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (req.prompt[:50], req.prompt, plan, code, test_out, used_model, "Muvaffaqiyatli" if passed else "Xatolik")
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print("Save error:", e)

    return {
        "status": "Muvaffaqiyatli" if passed else "Tekshirish kerak",
        "model_used": used_model,
        "tokens": tokens1 + tokens2,
        "plan": plan,
        "code": code,
        "test_result": test_out
    }

@app.get("/api/projects")
def get_projects():
    if not DATABASE_URL:
        return []
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, title, model_used, status, created_at FROM projects ORDER BY id DESC LIMIT 20")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Muhandis Boshqaruv Oynasi</title>
<style>
body{font-family:sans-serif;background:#0f172a;color:#f8fafc;margin:0;padding:15px}
.card{background:#1e293b;border-radius:12px;padding:15px;margin-bottom:15px;border:1px solid #334155}
textarea,select,button{width:100%;padding:12px;margin-top:8px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#fff;font-size:15px;box-sizing:border-box}
button{background:#10b981;color:#000;font-weight:bold;border:none;cursor:pointer}
button:disabled{background:#64748b}
pre{background:#020617;padding:10px;border-radius:8px;overflow-x:auto;font-size:13px;color:#38bdf8;white-space:pre-wrap}
.badge{display:inline-block;padding:4px 8px;border-radius:6px;font-size:12px;background:#3b82f6}
</style>
</head>
<body>
<h2>🤖 Shaxsiy AI Muhandis</h2>
<div class="card">
  <label>AI Modelni tanlang:</label>
  <select id="model">
    <option value="auto">⚡ Avtomatik (Eng yaxshi bepul model)</option>
    <option value="gemini/gemini-2.5-flash">Google Gemini 2.5 Flash (Bepul)</option>
    <option value="openrouter/deepseek/deepseek-r1:free">DeepSeek R1 (Bepul)</option>
    <option value="openrouter/qwen/qwen-2.5-coder-32b-instruct:free">Qwen 2.5 Coder 32B (Bepul)</option>
  </select>
  <textarea id="prompt" rows="4" placeholder="Vazifa yozing (masalan: Valyuta kursini hisoblaydigan Python funksiya yoz va test qil)..."></textarea>
  <button id="btn" onclick="runTask()">🚀 Vazifani Boshlash</button>
</div>
<div id="result" class="card" style="display:none">
  <span id="m_badge" class="badge"></span> <span id="t_badge" class="badge"></span>
  <h3>📋 Texnik Reja:</h3><pre id="plan"></pre>
  <h3>💻 Yozilgan Kod:</h3><pre id="code"></pre>
  <h3>✅ Avtomatik Test va Xavfsizlik Isboti:</h3><pre id="test"></pre>
</div>
<div class="card">
  <h3>🗄 Doimiy Xotira (Oxirgi loyihalar)</h3>
  <div id="history">Yuklanmoqda...</div>
</div>
<script>
async function loadHistory(){
  const r = await fetch('/api/projects'); const data = await r.json();
  document.getElementById('history').innerHTML = data.map(p=>`<p>• <b>${p.title}</b> [${p.status}] - <i>${p.model_used}</i></p>`).join('') || 'Hali loyihalar yo\\'q';
}
async function runTask(){
  const btn = document.getElementById('btn'); btn.disabled=true; btn.innerText='⏳ AI rejalashtirmoqda, kod yozmoqda va test qilmoqda...';
  try{
    const res = await fetch('/api/engineer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:document.getElementById('prompt').value,model:document.getElementById('model').value})});
    const d = await res.json();
    document.getElementById('result').style.display='block';
    document.getElementById('m_badge').innerText='Model: '+d.model_used;
    document.getElementById('t_badge').innerText='Token: '+d.tokens+' | '+d.status;
    document.getElementById('plan').innerText=d.plan;
    document.getElementById('code').innerText=d.code;
    document.getElementById('test').innerText=d.test_result;
    loadHistory();
  }catch(e){alert('Xato: '+e)}
  btn.disabled=false; btn.innerText='🚀 Vazifani Boshlash';
}
loadHistory();
</script>
</body></html>"""
