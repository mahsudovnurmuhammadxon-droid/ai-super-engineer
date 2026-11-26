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
            if G_KEY:
                models.extend([("gemini/gemini-3.8-flash", G_KEY), ("gemini/gemini-2.5-flash", G_KEY)])
            if O_KEY:
                models.extend([("openrouter/openrouter/free", O_KEY), ("openrouter/google/gemma-3-27b-it:free", O_KEY)])

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
                r2 = await acompletion(model="gemini/gemini-3.8-flash", messages=msgs, api_key=G_KEY, timeout=20)
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

HTML_PAGE = r'''<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>Super-Agent</title>
<script src="https://jsdelivr.net"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{background:radial-gradient(circle at 50% 100%,#0f1d3a 0%,#05070c 55%,#000 100%);color:#e2e8f0;display:flex;flex-direction:column;height:100dvh;overflow:hidden}
header{padding:14px 18px;display:flex;justify-content:space-between;align-items:center}
.hl,.hr{display:flex;align-items:center;gap:12px}
.menu-btn{background:none;border:none;color:#cbd5e1;font-size:22px;cursor:pointer}
.brand{font-size:19px;font-weight:500;color:#e2e8f0;display:flex;align-items:center;gap:5px}
.plan{background:#12254a;color:#a8c7fa;border:none;padding:7px 14px;border-radius:20px;font-size:13px}
.av{width:32px;height:32px;border-radius:50%;background:#5c4033;color:#fff;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:600}
#drawer{position:fixed;top:0;left:-270px;width:260px;height:100%;background:#0e121b;border-right:1px solid #1e293b;z-index:50;transition:.25s;padding:20px 16px;display:flex;flex-direction:column;gap:12px}
#drawer.open{left:0}
#ov{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;z-index:40}
.db{background:#161d2b;color:#e2e8f0;border:1px solid #263147;padding:12px 14px;border-radius:14px;text-align:left;font-size:14px;cursor:pointer}
#chat{flex:1;overflow-y:auto;padding:16px 18px;display:flex;flex-direction:column;gap:18px}
.center-star{margin:auto;display:flex;align-items:center;justify-content:center;animation:fl 3.5s ease-in-out infinite}
@keyframes fl{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
.user{align-self:flex-end;background:#1b2436;color:#f8fafc;padding:12px 16px;border-radius:22px 22px 6px 22px;max-width:84%;font-size:15.5px;line-height:1.5}
.ai{align-self:flex-start;width:100%;font-size:15.5px;line-height:1.68;color:#e2e8f0}
.ai p{margin-bottom:10px}.ai ul,.ai ol{margin:8px 0 12px 20px}.ai li{margin-bottom:5px}
.ai code{background:#161e2e;padding:2px 6px;border-radius:6px;font-family:monospace;font-size:13.5px;color:#7dd3fc}
.ai pre{background:#0d131f;border:1px solid #1e293b;padding:12px;border-radius:14px;overflow-x:auto;margin:10px 0}
.ai pre code{background:none;padding:0;color:#e2e8f0}
.acts{display:flex;gap:10px;margin-top:6px}.ab{background:none;border:none;color:#64748b;font-size:13px;cursor:pointer;padding:4px 6px}#pv{margin:0 16px 8px;background:#161e2e;padding:8px 14px;border-radius:14px;display:none;justify-content:space-between;align-items:center;font-size:13px;color:#7dd3fc}
#plusMenu{position:fixed;bottom:78px;left:18px;background:#161d2b;border:1px solid #28354d;border-radius:18px;padding:8px;display:none;flex-direction:column;gap:4px;z-index:30}
.pm{background:none;border:none;color:#e2e8f0;padding:10px 14px;border-radius:12px;text-align:left;font-size:14px;cursor:pointer}
.dock{padding:8px 14px 18px}
.pill{background:#141923;border:1px solid #222a3a;border-radius:30px;padding:7px 8px 7px 14px;display:flex;align-items:center;gap:8px}
.plus{background:none;border:none;color:#cbd5e1;font-size:24px;width:34px;height:34px;cursor:pointer;flex-shrink:0}
textarea{flex:1;background:transparent;border:none;color:#f8fafc;font-size:16px;resize:none;max-height:100px;outline:none;line-height:1.4;padding:6px 0;min-width:0}
.ib{background:none;border:none;color:#cbd5e1;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
.ib.rec{color:#ef4444;background:rgba(239,68,68,.15)}
.live{background:#12254a;color:#a8c7fa;border:none;width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
.live.on{background:#ef4444;color:#fff}
</style>
</head><body>
<header><div class="hl"><button class="menu-btn" onclick="toggleDrawer()">☰</button><div class="brand">✦ Super-Agent</div></div><div class="hr"><button class="plan">PRO</button><div class="av">U</div></div></header>
<div id="ov" onclick="toggleDrawer()"></div>
<aside id="drawer"><h3>✦ Super-Agent</h3><button class="db" onclick="newChat()">＋ Yangi chat</button><button class="db" onclick="clearHistory()">🗑️ Tarixni tozalash</button></aside>
<main id="chat"><div class="center-star">✦</div></main>
<div id="pv"><span id="pvname"></span><button onclick="removeFile()">✕</button></div>
<div id="plusMenu"><button class="pm" onclick="document.getElementById('file').click()">📎 Fayl yuklash</button><button class="pm" onclick="document.getElementById('file').click()">🖼️ Rasm yuborish</button></div>
<div class="dock"><div class="pill"><button class="plus" onclick="togglePlus()">＋</button><textarea id="msg" rows="1" placeholder="Xabar yozing..." onkeydown="keySend(event)"></textarea><button class="ib" onclick="document.getElementById('file').click()">📎</button><button class="live" id="send" onclick="send()">↑</button></div><input type="file" id="file" hidden onchange="pickFile(this)"></div>
<script>
let selected=null, busy=false;
const chat=document.getElementById('chat');
function toggleDrawer(){document.getElementById('drawer').classList.toggle('open');document.getElementById('ov').style.display=document.getElementById('drawer').classList.contains('open')?'block':'none'}
function togglePlus(){let p=document.getElementById('plusMenu');p.style.display=p.style.display==='flex'?'none':'flex'}
function pickFile(el){selected=el.files[0];if(selected){document.getElementById('pv').style.display='flex';document.getElementById('pvname').textContent='📎 '+selected.name}}
function removeFile(){selected=null;document.getElementById('file').value='';document.getElementById('pv').style.display='none'}
function add(role,text){let d=document.createElement('div');d.className=role;d.textContent=text;chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d}
function keySend(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}
async function send(){
 let input=document.getElementById('msg'),txt=input.value.trim();if(busy||(!txt&&!selected))return;
 let f=selected;add('user',txt+(f?'\n📎 '+f.name:''));input.value='';removeFile();document.getElementById('plusMenu').style.display='none';
 let ai=add('ai','');busy=true;document.getElementById('send').disabled=true;
 try{
  let form=new FormData();form.append('message',txt);
  if(f&&f.type.startsWith('image/')){let data=await new Promise((ok,no)=>{let r=new FileReader();r.onload=()=>ok(r.result);r.onerror=no;r.readAsDataURL(f)});form.append('image',data)}
  else if(f)form.append('file',f);
  let res=await fetch('/api/chat',{method:'POST',body:form});let reader=res.body.getReader(),decoder=new TextDecoder(),buf='';
  while(true){let {value,done}=await reader.read();if(done)break;buf+=decoder.decode(value,{stream:true});let parts=buf.split('\n\n');buf=parts.pop();for(let p of parts){let line=p.split('\n').find(x=>x.startsWith('data: '));if(!line)continue;let raw=line.slice(6);if(raw==='[DONE]')continue;try{let obj=JSON.parse(raw);ai.textContent+=obj.t;chat.scrollTop=chat.scrollHeight}catch(e){}}}
 }catch(e){ai.textContent='⚠️ Xato: '+e}
 busy=false;document.getElementById('send').disabled=false;
}
async function loadHistory(){try{let r=await fetch('/api/history'),d=await r.json();chat.innerHTML='';for(let m of d.messages)add(m.role==='user'?'user':'ai',m.content);if(!d.messages.length)chat.innerHTML='<div class="center-star">✦</div>'}catch(e){}}
async function clearHistory(){await fetch('/api/clear',{method:'POST'});chat.innerHTML='<div class="center-star">✦</div>';toggleDrawer()}
function newChat(){document.getElementById('msg').value='';removeFile();toggleDrawer()}
loadHistory();
</script></body></html>'''

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_PAGE
