import os, io, re, json, asyncio, subprocess, tempfile, psycopg2
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from litellm import acompletion
from duckduckgo_search import DDGS
from pypdf import PdfReader
from docx import Document
import openpyxl

app = FastAPI()
G_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
O_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
DB = os.environ.get("DATABASE_URL", "").strip()
SYS = "Sen samimiy, aqlli o'zbek tilidagi yordamchisan. Muhim joylarni **qalin** qil. Hisob-kitob uchun [PY]kod[/PY] yoz."

def db(sql, p=(), f=False):
    if not DB: return [] if f else None
    try:
        c = psycopg2.connect(DB)
        with c:
            with c.cursor() as cur:
                cur.execute(sql, p); res = cur.fetchall() if f else None
        c.close(); return res
    except Exception: return [] if f else None

db("CREATE TABLE IF NOT EXISTS mind_chat (id SERIAL PRIMARY KEY, role TEXT, content TEXT);")

def read_f(n, b):
    e = n.lower().split(".")[-1]
    try:
        if e == "pdf": return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(b)).pages)[:9000]
        if e == "docx": return "\n".join(p.text for p in Document(io.BytesIO(b)).paragraphs)[:9000]
        if e == "xlsx":
            wb = openpyxl.load_workbook(io.BytesIO(b), data_only=True)
            return "\n".join(" | ".join(str(x) for x in r if x is not None) for s in wb.sheetnames[:2] for r in list(wb[s].iter_rows(values_only=True))[:45])[:9000]
        return b.decode("utf-8", errors="ignore")[:9000]
    except Exception as ex: return str(ex)

@app.get("/health")
async def health():
    return {"gemini_key": bool(G_KEY), "openrouter_key": bool(O_KEY), "db": bool(DB)}

@app.post("/api/chat")
async def chat(message: str = Form(""), image: str = Form(""), file: Optional[UploadFile] = File(None)):
    t = message.strip()
    fc = f"\nFayl ({file.filename}):\n{read_f(file.filename, await file.read())}" if file and file.filename else ""
    db("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("user", t or "Fayl/Rasm"))
    async def gen():
        full = ""
        try:
            if image:
                msgs = [{"role":"system","content":SYS},{"role":"user","content":[{"type":"text","text":t or "Rasmni tahlil qil"},{"type":"image_url","image_url":{"url":image}}]}]
            else:
                w = ""
                if len(t.split()) > 2 and any(k in t.lower() for k in ["bugun","narx","kurs","ob-havo","yangilik","2025","2026","kim","qayerda"]):
                    try: w = "\nInternet:\n" + "\n".join(r.get("body","") for r in DDGS().text(t, max_results=3))
                    except Exception: pass
                hist = [{"role":r,"content":c} for r,c in reversed(db("SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 6;", f=True) or [])]
                msgs = [{"role":"system","content":SYS+w+fc}] + hist
            models = ([("gemini/gemini-2.5-flash", G_KEY), ("gemini/gemini-2.0-flash", G_KEY)] if G_KEY else []) + ([("openrouter/openrouter/free", O_KEY), ("openrouter/google/gemma-3-27b-it:free", O_KEY)] if O_KEY else [])
            ok = False; last_err = ""
            for m, k in models:
                try:
                    resp = await acompletion(model=m, messages=msgs, api_key=k, stream=True, timeout=25)
                    async for ch in resp:
                        d = (ch.choices[0].delta.content or "") if getattr(ch, "choices", None) else ""
                        if d: ok = True; full += d; yield f"data: {json.dumps({'t': d})}\n\n"
                    if ok: break
                except Exception as ex:
                    last_err = str(ex)
                    if ok: break
            if not ok:
                yield f"data: {json.dumps({'t': f'⚠️ Xato: {last_err or \"API kalit topilmadi\"}'})}\n\ndata: [DONE]\n\n"; return
            pm = re.search(r"\[PY\](.*?)\[/PY\]", full, re.S)
            if pm:
                with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
                    tf.write(pm.group(1).strip()); p = tf.name
                r = subprocess.run(["python3", p], capture_output=True, text=True, timeout=5); os.remove(p)
                ex = "\n\n✅ **Natija:** " + (r.stdout or r.stderr).strip()[:1000]
                full += ex; yield f"data: {json.dumps({'t': ex})}\n\n"
            db("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("assistant", full))
        except Exception as e:
            yield f"data: {json.dumps({'t': f'⚠️ {e}'})}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/api/history")
async def history():
    return {"messages": [{"role":r,"content":c} for r,c in reversed(db("SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 25;", f=True) or [])]}

@app.post("/api/clear")
async def clear():
    db("DELETE FROM mind_chat;"); return {"ok": 1}

HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Super-Agent</title>
<script src="https://jsdelivr.net"></script>
<style>*{box-sizing:border-box;margin:0;padding:0;font-family:sans-serif}body{background:radial-gradient(circle at 50% 100%,#0f1d3a 0%,#05070c 60%,#000 100%);color:#e2e8f0;display:flex;flex-direction:column;height:100dvh}header{padding:14px 18px;display:flex;justify-content:space-between;align-items:center}.btn{background:#12254a;color:#a8c7fa;border:none;padding:7px 14px;border-radius:20px}#chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:14px}.star{margin:auto}.user{align-self:flex-end;background:#1b2436;padding:11px 15px;border-radius:18px;max-width:85%}.ai{align-self:flex-start;width:100%;line-height:1.6}.dock{padding:10px 14px 18px}.pill{background:#141923;border:1px solid #222a3a;border-radius:30px;padding:6px 10px;display:flex;align-items:center;gap:8px}input{flex:1;background:transparent;border:none;color:#fff;font-size:16px;outline:none}.ib{background:none;border:none;color:#cbd5e1;font-size:18px}.live{background:#12254a;color:#a8c7fa;border:none;width:38px;height:38px;border-radius:50%}.live.on{background:#ef4444;color:#fff}</style></head><body>
<header><b>✦ Super-Agent</b><button class="btn" onclick="clr()">Новый чат</button></header><div id="chat"></div>
<div class="dock"><div class="pill"><input type="file" id="fi" accept="image/*" hidden onchange="hi(event)"><input type="file" id="ff" accept=".pdf,.docx,.xlsx,.txt" hidden onchange="hf(event)"><button class="ib" onclick="fi.click()">📷</button><button class="ib" onclick="ff.click()">📎</button><input id="ip" placeholder="Спросить ..." onkeydown="if(event.key==='Enter')sn()"><button class="ib" onclick="mic()">🎙️</button><button class="live" id="ab" onclick="onA()">ıll</button></div></div>
<script>let img=null,doc=null,rc=null,lv=false;
function star(){chat.innerHTML='<div class="star"><svg width="46" height="46" viewBox="0 0 48 48"><defs><linearGradient id="g" x1="0" y1="0" x2="48" y2="48"><stop offset="0%" stop-color="#4285f4"/><stop offset="40%" stop-color="#9b72cb"/><stop offset="70%" stop-color="#d96570"/><stop offset="100%" stop-color="#f4b400"/></linearGradient></defs><path d="M24 2C24 14.5 33.5 24 46 24C33.5 24 24 33.5 24 46C24 33.5 14.5 24 2 24C14.5 24 24 14.5 24 2Z" fill="url(#g)"/></svg></div>'}
function spk(t,cb){try{speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t.replace(/[*#`]/g,""));u.lang="uz-UZ";u.onend=cb;u.onerror=cb;speechSynthesis.speak(u)}catch(e){if(cb)cb()}}
function onA(){if(ip.value.trim()||img||doc)sn();else{lv=!lv;ab.classList.toggle("on",lv);if(lv)mic();else{if(rc)rc.stop();speechSynthesis.cancel()}}}
function add(r,t){const s=chat.querySelector('.star');if(s)s.remove();const d=document.createElement("div");d.className=r==="user"?"user":"ai";d.innerHTML=r==="user"?t:marked.parse(t||"...");chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d}
function hi(e){const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=v=>img=v.target.result;r.readAsDataURL(f)}
function hf(e){doc=e.target.files[0]}
function mic(){const SR=window.SpeechRecognition||window.webkitSpeechRecognition;if(!SR)return;rc=new SR();rc.lang="uz-UZ";rc.onend=()=>{if(lv&&ip.value.trim())sn()};rc.onresult=e=>ip.value=e.results[0][0].transcript;rc.start()}
async function ld(){const d=await(await fetch("/api/history")).json();chat.innerHTML="";if(d.messages&&d.messages.length)d.messages.forEach(m=>add(m.role,m.content));else star()}
async function sn(){const t=ip.value.trim();if(!t&&!img&&!doc)return;const fd=new FormData();fd.append("message",t);if(img)fd.append("image",img);if(doc)fd.append("file",doc);ip.value="";img=null;doc=null;add("user",t||"📎 Fayl");const d=add("assistant","...");let a="";const res=await fetch("/api/chat",{method:"POST",body:fd});const rd=res.body.getReader(),dc=new TextDecoder();while(true){const{done,value}=await rd.read();if(done)break;for(const l of dc.decode(value).split("\n"))if(l.startsWith("data: ")){const p=l.slice(6);if(p==="[DONE]")break;try{const j=JSON.parse(p);if(j.t){a+=j.t;d.innerHTML=marked.parse(a);chat.scrollTop=chat.scrollHeight}}catch(e){}}}if(lv&&a)spk(d.innerText,()=>{if(lv)mic()})}
async function clr(){await fetch("/api/clear",{method:"POST"});star()}ld();</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
async def home(): return HTML

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
