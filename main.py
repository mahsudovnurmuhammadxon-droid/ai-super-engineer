import os, asyncio, psycopg2
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
from litellm import acompletion

app = FastAPI()
KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
DB = os.environ.get("DATABASE_URL", "").strip()

CORES = [
    "openrouter/deepseek/deepseek-r1-0528:free",
    "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "openrouter/qwen/qwen-2.5-72b-instruct:free",
    "openrouter/google/gemma-3-27b-it:free",
]
VISION = [
    "openrouter/google/gemma-3-27b-it:free",
    "openrouter/qwen/qwen2.5-vl-72b-instruct:free",
    "openrouter/openrouter/free",
]
SYNTH = "openrouter/openrouter/free"

SYS = (
    "Sen bitta yagona, IQ darajasi o'ta yuqori universal daho shaxssan ('Men' deb gapirasan). "
    "Sening ongingda shifokor, muhandis, dasturchi, arabshunos-tarjimon va ustoz bilimlari mujassam. "
    "Faqat o'zbek tilida jonli, aniq va amaliy javob ber. "
    "Rasm yoki kitob berilsa, xira joylarini ASLO to'qima, [xira] deb belgilab, qolganini aniq tarjima qil."
)

def db_exec(sql, params=(), fetch=False):
    if not DB: return [] if fetch else None
    try:
        c = psycopg2.connect(DB)
        cur = c.cursor()
        cur.execute(sql, params)
        res = cur.fetchall() if fetch else None
        c.commit(); cur.close(); c.close()
        return res
    except Exception:
        return [] if fetch else None

db_exec("CREATE TABLE IF NOT EXISTS mind_chat (id SERIAL PRIMARY KEY, role TEXT, content TEXT);")

async def ask_ai(model, msgs):
    try:
        r = await acompletion(model=model, messages=msgs, api_key=KEY, timeout=35)
        fc = next(iter(r.choices), None)
        return fc.message.content if fc else None
    except Exception:
        return None

class Req(BaseModel):
    message: str = ""
    image: Optional[str] = None

@app.post("/api/chat")
async def chat(req: Req):
    txt = req.message.strip()
    if not txt and not req.image:
        return JSONResponse({"error": "Xabar yoki rasm yuboring"}, 400)
    db_exec("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("user", f"📷 {txt}" if req.image else txt))
    rows = db_exec("SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 8;", fetch=True)
    hist = [{"role": r, "content": c} for r, c in reversed(rows)]

    if req.image:
        p = txt or "Rasmdagi matnni diqqat bilan o'qi va o'zbek tiliga tarjima qil. Xira joylarini to'qima."
        v_msg = [{"role": "system", "content": SYS}, {"role": "user", "content": [{"type": "text", "text": p}, {"type": "image_url", "image_url": {"url": req.image}}]}]
        raw = None
        for m in VISION:
            raw = await ask_ai(m, v_msg)
            if raw: break
        if not raw: return JSONResponse({"error": "Rasmni o'qishda yuklama bor, qayta urining."}, 500)
        chk = [{"role": "system", "content": SYS}, {"role": "user", "content": f"So'rov: {p}\nNatija:\n{raw}\n\nXatolarini tekshirib mukammal javob ber."}]
        ans = await ask_ai(SYNTH, chk) or raw
        db_exec("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("assistant", ans))
        return {"reply": ans}

    msgs = [{"role": "system", "content": SYS}] + hist
    res = [t for t in await asyncio.gather(*[ask_ai(m, msgs) for m in CORES]) if t]
    if not res:
        fb = await ask_ai(SYNTH, msgs)
        if not fb: return JSONResponse({"error": "Bog'lanishda uzilish, qayta yuboring."}, 500)
        db_exec("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("assistant", fb))
        return {"reply": fb}
    first = next(iter(res))
    if len(res) == 1:
        ans = first
    else:
        s_msg = [{"role": "system", "content": SYS + " Quyidagi fikrlarni birlashtirib bitta mukammal javob ber."}, {"role": "user", "content": f"Savol: {txt}\n\nTahlillar:\n" + "\n---\n".join(res)}]
        ans = await ask_ai(SYNTH, s_msg) or first
    db_exec("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("assistant", ans))
    return {"reply": ans}

@app.get("/api/history")
async def history():
    rows = db_exec("SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 30;", fetch=True)
    return {"messages": [{"role": r, "content": c} for r, c in reversed(rows)]}

@app.post("/api/clear")
async def clear():
    db_exec("DELETE FROM mind_chat;")
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Super-Ong</title><style>
*{box-sizing:border-box;margin:0;padding:0;font-family:sans-serif}body{background:#090d16;color:#fff;display:flex;flex-direction:column;height:100dvh}
header{background:#111827;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2937}
h1{font-size:17px;color:#38bdf8}button.t{background:#1f2937;color:#fff;border:1px solid #374151;padding:6px 10px;border-radius:8px}
#chat{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:88%;padding:11px 14px;border-radius:14px;line-height:1.5;font-size:15px;white-space:pre-wrap;word-wrap:break-word}
.user{background:#2563eb;align-self:flex-end}.assistant{background:#111827;border:1px solid #1f2937;align-self:flex-start}
.sb{margin-top:6px;background:#1f2937;border:1px solid #374151;color:#94a3b8;padding:4px 8px;border-radius:6px;font-size:12px}
#pv{background:#111827;padding:6px 14px;display:none;justify-content:space-between;color:#38bdf8;font-size:13px}
.bar{background:#111827;padding:8px;display:flex;gap:6px;align-items:flex-end;border-top:1px solid #1f2937}
textarea{flex:1;background:#090d16;color:#fff;border:1px solid #374151;border-radius:10px;padding:10px;font-size:15px;resize:none;max-height:100px}
.b{background:#1f2937;color:#fff;border:1px solid #374151;width:40px;height:40px;border-radius:10px;font-size:17px}
.rec{background:#dc2626}.snd{background:#10b981;font-weight:bold}
</style></head><body>
<header><h1>🧠 Shaxsiy Super-Ong</h1><button class="t" onclick="clr()">🗑️</button></header>
<div id="chat"></div><div id="pv"><span id="pn">📷</span><button class="t" onclick="rm()">❌</button></div>
<div class="bar"><input type="file" id="fi" accept="image/*" hidden onchange="hi(event)">
<button class="b" onclick="fi.click()">📷</button><button class="b" id="mc" onclick="tm()">🎤</button>
<textarea id="ip" rows="1" placeholder="Yozing, rasm yuklang yoki gapiring..."></textarea>
<button class="b snd" id="sd" onclick="sn()">➤</button></div>
<script>
let img=null,rec,act=false;
function hi(e){const f=e.target.files.item(0);if(!f)return;const r=new FileReader();r.onload=v=>{const i=new Image();i.onload=()=>{const c=document.createElement("canvas"),s=Math.min(1,1024/i.width);c.width=i.width*s;c.height=i.height*s;c.getContext("2d").drawImage(i,0,0,c.width,c.height);img=c.toDataURL("image/jpeg",.85);pv.style.display="flex";pn.textContent="📷 "+f.name};i.src=v.target.result};r.readAsDataURL(f)}
function rm(){img=null;fi.value="";pv.style.display="none"}
function add(r,t){const d=document.createElement("div");d.className="msg "+r;d.textContent=t;if(r==="assistant"){const b=document.createElement("button");b.className="sb";b.textContent="🔊 Eshitish";b.onclick=()=>{speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t);u.lang="uz-UZ";speechSynthesis.speak(u)};d.appendChild(document.createElement("br"));d.appendChild(b)}chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d}
async function ld(){try{const d=await(await fetch("/api/history")).json();chat.innerHTML="";if(d.messages&&d.messages.length)d.messages.forEach(m=>add(m.role,m.content));else add("assistant","Salom! Men sening shaxsiy yagona ongingman. Matn yoz, 📷 rasm yukla yoki 🎤 gapir.")}catch(e){}}
async function sn(){const t=ip.value.trim();if(!t&&!img)return;const im=img;ip.value="";rm();add("user",im?("📷 "+(t||"Rasm")):t);const w=add("assistant","⏳...");sd.disabled=true;try{const d=await(await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:t,image:im})})).json();w.remove();add("assistant",d.reply||("❌ "+d.error))}catch(e){w.textContent="Sahifani yangilang"}sd.disabled=false}
async function clr(){if(confirm("O'chirilsinmi?")){await fetch("/api/clear",{method:"POST"});ld()}}
if("webkitSpeechRecognition"in window||"SpeechRecognition"in window){const S=window.SpeechRecognition||window.webkitSpeechRecognition;rec=new S();rec.lang="uz-UZ";rec.onresult=e=>{ip.value+=(ip.value?" ":"")+e.results.item(0).item(0).transcript};rec.onend=()=>{act=false;mc.classList.remove("rec")}}
function tm(){if(!rec)return alert("Klaviaturadagi mikrofonni bosing");act?rec.stop():(act=true,mc.classList.add("rec"),rec.start())}
ld();</script></body></html>"""

