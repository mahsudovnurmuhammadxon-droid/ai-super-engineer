import os, io, re, json, asyncio, subprocess, tempfile, psycopg2
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from litellm import acompletion
from duckduckgo_search import DDGS
from pypdf import PdfReader
from docx import Document
import openpyxl

app = FastAPI()
G_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
O_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
DB = os.environ.get("DATABASE_URL", "").strip()

SYS = (
    "Sen shablonli robot emassan — sen foydalanuvchining eng aqlli, samimiy, hazilkash va erkin fikrlaydigan hamkorisan. "
    "Xuddi yaqin do'st va kuchli mutaxassis kabi tabiiy, ravon o'zbek tilida suhbatlash ('Men' deb gapir, ichki modullaringni sanab o'tirma). "
    "Javoblaringni chiroyli tuz: muhim joylarni **qalin** qil, ro'yxatlar va sarlavhalardan foydalan. "
    "Aniq hisob-kitob yoki kodni tekshirish kerak bo'lsa, [PY]kod[/PY] yoz — tizim uni ishga tushirib natijasini beradi. "
    "Rasmdagi xira joylarni ASLO to'qima."
)

def db(sql, p=(), f=False):
    if not DB: return [] if f else None
    try:
        c = psycopg2.connect(DB)
        with c:
            with c.cursor() as cur:
                cur.execute(sql, p)
                res = cur.fetchall() if f else None
        c.close(); return res
    except Exception: return [] if f else None

db("CREATE TABLE IF NOT EXISTS mind_chat (id SERIAL PRIMARY KEY, role TEXT, content TEXT);")

def read_file(name: str, data: bytes) -> str:
    ext = name.lower().split(".")[-1]
    try:
        if ext == "pdf": return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)[:12000]
        if ext == "docx": return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)[:12000]
        if ext == "xlsx":
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
            return "\n".join(" | ".join(str(x) for x in r if x is not None) for s in wb.sheetnames[:3] for r in list(wb[s].iter_rows(values_only=True))[:60])[:12000]
        return data.decode("utf-8", errors="ignore")[:12000]
    except Exception as e: return f"[Fayl xatosi: {e}]"

def run_py(code: str) -> str:
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
            tf.write(code); path = tf.name
        r = subprocess.run(["python3", path], capture_output=True, text=True, timeout=6)
        return (r.stdout + "\n" + r.stderr).strip()[:2500] or "Bajarildi"
    except Exception as e: return f"Xato: {e}"
    finally:
        if path and os.path.exists(path): os.remove(path)

def need_web(q: str) -> bool:
    kw = ["bugun", "hozir", "narx", "kurs", "dollar", "ob-havo", "yangilik", "xabar", "2025", "2026", "kim", "qayerda", "qachon", "natija", "o'yin", "sayt", "link"]
    ql = q.lower()
    return len(q.split()) > 2 and any(k in ql for k in kw)

def web_search(q: str) -> str:
    try:
        res = DDGS().text(q, max_results=4)
        return "\n".join(f"[{i}] {r.get('title')} ({r.get('href')}): {r.get('body')}" for i, r in enumerate(res, 1)) if res else ""
    except Exception: return ""

def rag_memory(q: str) -> str:
    words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 3][:3]
    if not words: return ""
    rows = db("SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 60;", f=True) or []
    return "\n".join(f"{r}: {c[:220]}" for r, c in rows if any(w in c.lower() for w in words))[:1000]

@app.post("/api/chat")
async def chat(message: str = Form(""), image: str = Form(""), file: Optional[UploadFile] = File(None)):
    txt = message.strip()
    file_ctx = ""
    if file and file.filename:
        file_ctx = f"\n\nYUKLANGAN FAYL ({file.filename}):\n{read_file(file.filename, await file.read())}"
    if not txt and not image and not file_ctx:
        return JSONResponse({"error": "Xabar yozing"}, 400)

    db("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("user", f"{'📷 ' if image else ''}{'📎 '+file.filename+' ' if file_ctx else ''}{txt}"))

    async def gen():
        full_reply = ""
        try:
            if image:
                msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": [{"type": "text", "text": txt or "Rasmni tahlil qil."}, {"type": "image_url", "image_url": {"url": image}}]}]
            else:
                inet = await asyncio.to_thread(web_search, txt) if need_web(txt) else ""
                mem = await asyncio.to_thread(rag_memory, txt) if len(txt) > 8 else ""
                s = SYS + (f"\n\nESKI XOTIRA:\n{mem}" if mem else "") + (f"\n\nJONLI INTERNET:\n{inet}" if inet else "") + file_ctx
                hist = [{"role": r, "content": c} for r, c in reversed(db("SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 8;", f=True) or [])]
                msgs = [{"role": "system", "content": s}] + hist

            models = []
            if G_KEY: models.append(("gemini/gemini-2.5-flash", G_KEY))
            if O_KEY:
                models.extend([
                    ("openrouter/google/gemma-3-27b-it:free", O_KEY),
                    ("openrouter/meta-llama/llama-3.3-70b-instruct:free", O_KEY),
                    ("openrouter/openrouter/free", O_KEY)
                ])

            streamed = False
            for m, k in models:
                try:
                    resp = await acompletion(model=m, messages=msgs, api_key=k, stream=True, timeout=25)
                    async for chunk in resp:
                        delta = chunk.choices.delta.content or ""
                        if delta:
                            streamed = True
                            full_reply += delta
                            yield f"data: {json.dumps({'t': delta})}\n\n"
                    if streamed: break
                except Exception: continue

            if not streamed:
                yield f"data: {json.dumps({'t': '⚠️ Tarmoqda yuklama bor, qayta urinib ko‘ring.'})}\n\n"
                return

            py_m = re.search(r"\[PY\](.*?)\[/PY\]", full_reply, re.S)
            if py_m and G_KEY:
                out = await asyncio.to_thread(run_py, py_m.group(1).strip())
                msgs.append({"role": "assistant", "content": full_reply})
                msgs.append({"role": "user", "content": f"Kod natijasi: {out}. Shu natijani qisqa va aniq tushuntir."})
                r2 = await acompletion(model="gemini/gemini-2.5-flash", messages=msgs, api_key=G_KEY, timeout=20)
                extra = "\n\n✅ **Natija:**\n" + (r2.choices.message.content or out)
                full_reply += extra
                yield f"data: {json.dumps({'t': extra})}\n\n"

            db("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("assistant", full_reply))
        except Exception as e:
            yield f"data: {json.dumps({'t': f'⚠️ Xato: {e}'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
@app.get("/api/history")
async def history():
    rows = db("SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 30;", f=True) or []
    return {"messages": [{"role": r, "content": c} for r, c in reversed(rows)]}

@app.post("/api/clear")
async def clear():
    db("DELETE FROM mind_chat;"); return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def home():
    return r"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>Super-Agent</title>
<script src="https://jsdelivr.net"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{background:radial-gradient(circle at 50% 100%,#0f1d3a 0%,#05070c 55%,#000000 100%);color:#e2e8f0;display:flex;flex-direction:column;height:100dvh;overflow:hidden}
header{padding:14px 18px;display:flex;justify-content:space-between;align-items:center;z-index:10}
.hl{display:flex;align-items:center;gap:14px}
.menu-btn{background:none;border:none;color:#cbd5e1;font-size:22px;cursor:pointer;padding:2px 4px}
.brand{font-size:19px;font-weight:500;color:#e2e8f0;display:flex;align-items:center;gap:5px;letter-spacing:-0.3px}
.brand span{font-size:12px;color:#94a3b8}
.hr{display:flex;align-items:center;gap:10px}
.plan{background:#12254a;color:#a8c7fa;border:none;padding:7px 14px;border-radius:20px;font-size:13px;font-weight:500}
.av{width:32px;height:32px;border-radius:50%;background:#5c4033;color:#f8fafc;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:600}
#drawer{position:fixed;top:0;left:-270px;width:260px;height:100%;background:#0e121b;border-right:1px solid #1e293b;z-index:50;transition:.25s;padding:20px 16px;display:flex;flex-direction:column;gap:12px}
#drawer.open{left:0}
#ov{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;z-index:40}
.db{background:#161d2b;color:#e2e8f0;border:1px solid #263147;padding:12px 14px;border-radius:14px;text-align:left;font-size:14px;cursor:pointer}
#chat{flex:1;overflow-y:auto;padding:16px 18px;display:flex;flex-direction:column;gap:18px;scroll-behavior:smooth}
.center-star{margin:auto;display:flex;align-items:center;justify-content:center;animation:float 3.5s ease-in-out infinite}
@keyframes float{0%,100%{transform:translateY(0) scale(1)}50%{transform:translateY(-6px) scale(1.05)}}
.user{align-self:flex-end;background:#1b2436;color:#f8fafc;padding:12px 16px;border-radius:22px 22px 6px 22px;max-width:84%;font-size:15.5px;line-height:1.5}
.ai{align-self:flex-start;width:100%;font-size:15.5px;line-height:1.68;color:#e2e8f0;padding:2px 0}
.ai p{margin-bottom:10px}.ai ul,.ai ol{margin:8px 0 12px 20px}.ai li{margin-bottom:5px}
.ai h1,.ai h2,.ai h3{color:#fff;margin:14px 0 6px;font-size:17px}
.ai code{background:#161e2e;padding:2px 6px;border-radius:6px;font-family:monospace;font-size:13.5px;color:#7dd3fc}
.ai pre{background:#0d131f;border:1px solid #1e293b;padding:12px;border-radius:14px;overflow-x:auto;margin:10px 0}
.ai pre code{background:none;padding:0;color:#e2e8f0}
.ai a{color:#7dd3fc;text-decoration:underline}
.acts{display:flex;gap:10px;margin-top:6px}
.ab{background:none;border:none;color:#64748b;font-size:13px;cursor:pointer;padding:4px 6px}
#pv{margin:0 16px 8px;background:#161e2e;padding:8px 14px;border-radius:14px;display:none;justify-content:space-between;align-items:center;font-size:13px;color:#7dd3fc}
#plusMenu{position:fixed;bottom:78px;left:18px;background:#161d2b;border:1px solid #28354d;border-radius:18px;padding:8px;display:none;flex-direction:column;gap:4px;z-index:30;box-shadow:0 10px 30px rgba(0,0,0,.6)}
.pm{background:none;border:none;color:#e2e8f0;padding:10px 14px;border-radius:12px;text-align:left;font-size:14px;display:flex;align-items:center;gap:10px;cursor:pointer}
.pm:active{background:#222d42}
.dock{padding:8px 14px 18px}
.pill{background:#141923;border:1px solid #222a3a;border-radius:30px;padding:7px 8px 7px 14px;display:flex;align-items:center;gap:8px;box-shadow:0 6px 24px rgba(0,0,0,.45)}
.plus{background:none;border:none;color:#cbd5e1;font-size:24px;font-weight:300;width:34px;height:34px;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
textarea{flex:1;background:transparent;border:none;color:#f8fafc;font-size:16px;resize:none;max-height:100px;outline:none;line-height:1.4;padding:6px 0;min-width:0}
textarea::placeholder{color:#8e98a8}
.ib{background:none;border:none;color:#cbd5e1;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
.ib.rec{color:#ef4444;background:rgba(239,68,68,.15);animation:pulse 1s infinite}
.live{background:#12254a;color:#a8c7fa;border:none;width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
.live.on{background:#ef4444;color:#fff;animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.5}}
</style></head><body>
<div id="ov" onclick="tgDrawer(false)"></div>
<div id="drawer">
<div style="font-size:17px;font-weight:600;color:#fff;margin-bottom:8px">✦ Super-Agent</div>
<button class="db" onclick="clr()">＋ Новый чат</button>
<button class="db" onclick="clr()">🗑️ Очистить историю</button>
</div>
<div id="plusMenu">
<button class="pm" onclick="pmClose();fi.click()">📷 Загрузить фото</button>
<button class="pm" onclick="pmClose();ff.click()">📎 Загрузить файл (PDF/Word/Excel)</button>
</div>
<header>
<div class="hl"><button class="menu-btn" onclick="tgDrawer(true)">☰</button><div class="brand">Super-Agent <span>▾</span></div></div>
<div class="hr"><button class="plan" onclick="clr()">Новый чат</button><div class="av">N</div></div>
</header>
<div id="chat"></div>
<div id="pv"><span id="pn"></span><button class="ab" style="color:#fff" onclick="rm()">✕</button></div>
<div class="dock"><div class="pill">
<input type="file" id="fi" accept="image/*" hidden onchange="hi(event)">
<input type="file" id="ff" accept=".pdf,.docx,.xlsx,.txt,.py,.html" hidden onchange="hf(event)">
<button class="plus" onclick="tgPlus()">+</button>
<textarea id="ip" rows="1" placeholder="Спросить ..." oninput="onType()"></textarea>
<button class="ib" id="mc" onclick="mic()" title="Голосовой ввод">
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
</button>
<button class="live" id="actBtn" onclick="onAction()" title="Live / Отправить">
<svg id="waveIco" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="6" y1="10" x2="6" y2="14"/><line x1="10" y1="6" x2="10" y2="18"/><line x1="14" y1="8" x2="14" y2="16"/><line x1="18" y1="10" x2="18" y2="14"/></svg>
<svg id="sendIco" style="display:none" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
</button>
</div></div>
<script>
let img=null,doc=null,recg=null,isRec=false,liveMode=false;
marked.setOptions({breaks:true});
function showStar(){chat.innerHTML=`<div class="center-star"><svg width="46" height="46" viewBox="0 0 48 48" fill="none"><defs><linearGradient id="g1" x1="0" y1="0" x2="48" y2="48" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#4285f4"/><stop offset="35%" stop-color="#9b72cb"/><stop offset="70%" stop-color="#d96570"/><stop offset="100%" stop-color="#f4b400"/></linearGradient></defs><path d="M24 2C24 14.5 33.5 24 46 24C33.5 24 24 33.5 24 46C24 33.5 14.5 24 2 24C14.5 24 24 14.5 24 2Z" fill="url(#g1)"/></svg></div>`}
function tgDrawer(s){drawer.classList.toggle("open",s);ov.style.display=s?"block":"none"}
function tgPlus(){plusMenu.style.display=plusMenu.style.display==="flex"?"none":"flex"}
function pmClose(){plusMenu.style.display="none"}
function onType(){const has=ip.value.trim().length>0||img||doc;waveIco.style.display=has?"none":"block";sendIco.style.display=has?"block":"none"}
function onAction(){const has=ip.value.trim().length>0||img||doc;if(has){sn()}else{toggleLive()}}
function toggleLive(){liveMode=!liveMode;actBtn.classList.toggle("on",liveMode);if(liveMode){mic()}else if(recg){recg.stop();speechSynthesis.cancel()}}
function addMsg(role,text){const st=chat.querySelector('.center-star');if(st)st.remove();const d=document.createElement("div");d.className=role==="user"?"user":"ai";if(role==="user"){d.textContent=text}else{const b=document.createElement("div");b.innerHTML=marked.parse(text||"...");d.appendChild(b);const ac=document.createElement("div");ac.className="acts";const sp=document.createElement("button");sp.className="ab";sp.textContent="🔊";sp.onclick=()=>{speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(b.innerText);u.lang="uz-UZ";speechSynthesis.speak(u)};const cp=document.createElement("button");cp.className="ab";cp.textContent="📋";cp.onclick=()=>{navigator.clipboard.writeText(b.innerText);cp.textContent="✅";setTimeout(()=>cp.textContent="📋",1200)};ac.append(sp,cp);d.appendChild(ac);d._body=b}chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d}
function hi(e){const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=v=>{const i=new Image();i.onload=()=>{const c=document.createElement("canvas"),s=Math.min(1,1024/i.width);c.width=i.width*s;c.height=i.height*s;c.getContext("2d").drawImage(i,0,0,c.width,c.height);img=c.toDataURL("image/jpeg",.85);pv.style.display="flex";pn.textContent="📷 "+f.name;onType()};i.src=v.target.result};r.readAsDataURL(f)}
function hf(e){doc=e.target.files[0];if(doc){pv.style.display="flex";pn.textContent="📎 "+doc.name;onType()}}
