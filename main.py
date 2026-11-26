import os, asyncio, psycopg2
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
from litellm import acompletion
from duckduckgo_search import DDGS

app = FastAPI()
KEY, DB = os.environ.get("OPENROUTER_API_KEY", "").strip(), os.environ.get("DATABASE_URL", "").strip()
CORES = ["openrouter/deepseek/deepseek-r1-0528:free", "openrouter/meta-llama/llama-3.3-70b-instruct:free", "openrouter/qwen/qwen-2.5-72b-instruct:free", "openrouter/google/gemma-3-27b-it:free"]
VISION = ["openrouter/google/gemma-3-27b-it:free", "openrouter/qwen/qwen2.5-vl-72b-instruct:free", "openrouter/openrouter/free"]
SYNTH = "openrouter/openrouter/free"
SYS = "Sen shablonli yordamchi emassan! Sen foydalanuvchining eng yaqin, o'ta aqlli, dangal va mustaqil fikrlaydigan do'stisan ('Men' deb gapirasan). Rasmiyatchiliksiz, jonli o'zbek tilida javob ber. JONLI INTERNET QIDIRUV NATIJALARI va bilimingni birlashtir. Fakt yonida, [2] manba raqamini qo'y va oxirida '📚 Manbalar:' bo'limida sayt nomi hamda to'liq URL manzilini yoz. Rasm yuborilsa, xira joylarini to'qima, [xira] deb belgilab aniq tahlil qil."

def db(sql, p=(), f=False):
    if not DB: return [] if f else None
    try:
        c = psycopg2.connect(DB); cur = c.cursor(); cur.execute(sql, p)
        res = cur.fetchall() if f else None; c.commit(); cur.close(); c.close(); return res
    except Exception: return [] if f else None

db("CREATE TABLE IF NOT EXISTS mind_chat (id SERIAL PRIMARY KEY, role TEXT, content TEXT);")

def search(q):
    try:
        r = DDGS().text(q, max_results=7)
        return "\n\n".join([f"[{i}] {x.get('title','')}\nURL: {x.get('href','')}\nInfo: {x.get('body','')}" for i, x in enumerate(r, 1)]) if r else ""
    except Exception: return ""

async def ai(m, msgs):
    try:
        r = await acompletion(model=m, messages=msgs, api_key=KEY, timeout=35)
        fc = next(iter(r.choices), None); return fc.message.content if fc else None
    except Exception: return None

class Req(BaseModel):
    message: str = ""
    image: Optional[str] = None

@app.post("/api/chat")
async def chat(req: Req):
    t = req.message.strip()
    if not t and not req.image: return JSONResponse({"error": "Xabar yuboring"}, 400)
    db("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("user", f"📷 {t}" if req.image else t))
    hist = [{"role": r, "content": c} for r, c in reversed(db("SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 12;", f=True))]
    if req.image:
        p = t or "Rasmdagi matnni o'qi va o'zbek tiliga tarjima qil. Xira joylarini to'qima."
        vm = [{"role": "system", "content": SYS}, {"role": "user", "content": [{"type": "text", "text": p}, {"type": "image_url", "image_url": {"url": req.image}}]}]
        raw = None
        for m in VISION:
            raw = await ai(m, vm)
            if raw: break
        if not raw: return JSONResponse({"error": "Yuklama bor, qayta urining"}, 500)
        ans = await ai(SYNTH, [{"role": "system", "content": SYS}, {"role": "user", "content": f"So'rov: {p}\nNatija:\n{raw}\n\nTekshirib mukammal javob ber."}]) or raw
        db("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("assistant", ans)); return {"reply": ans}
    web = await asyncio.to_thread(search, t)
    sw = SYS + (f"\n\nJONLI INTERNET QIDIRUV NATIJALARI:\n{web}" if web else "")
    msgs = [{"role": "system", "content": sw}] + hist
    res = [x for x in await asyncio.gather(*[ai(m, msgs) for m in CORES]) if x]
    if not res:
        fb = await ai(SYNTH, msgs)
        if not fb: return JSONResponse({"error": "Uzilish bor, qayta yuboring"}, 500)
        db("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("assistant", fb)); return {"reply": fb}
    first = next(iter(res))
    ans = first if len(res) == 1 else (await ai(SYNTH, [{"role": "system", "content": sw + "\nFikrlarni birlashtirib dangal ohangda, [1], [2] manbalari va URL havolalari bilan javob ber."}, {"role": "user", "content": f"Savol: {t}\n\n" + "\n---\n".join(res)}]) or first)
    db("INSERT INTO mind_chat (role, content) VALUES (%s, %s);", ("assistant", ans)); return {"reply": ans}

@app.get("/api/history")
async def history():
    return {"messages": [{"role": r, "content": c} for r, c in reversed(db("SELECT role, content FROM mind_chat ORDER BY id DESC LIMIT 30;", f=True))]}

@app.post("/api/clear")
async def clear():
    db("DELETE FROM mind_chat;"); return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Super-Ong</title><style>
*{box-sizing:border-box;margin:0;padding:0;font-family:sans-serif}body{background:#090d16;color:#fff;display:flex;flex-direction:column;height:100dvh}
header{background:#111827;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2937}
h1{font-size:17px;color:#38bdf8}button.t{background:#1f2937;color:#fff;border:1px solid #374151;padding:6px 10px;border-radius:8px}
#chat{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:88%;padding:11px 14px;border-radius:14px;line-height:1.5;font-size:15px;white-space:pre-wrap;word-wrap:break-word}.msg a{color:#38bdf8;text-decoration:underline;word-break:break-all}
.user{background:#2563eb;align-self:flex-end}.assistant{background:#111827;border:1px solid #1f2937;align-self:flex-start}
.sb{margin-top:6px;background:#1f2937;border:1px solid #374151;color:#94a3b8;padding:4px 8px;border-radius:6px;font-size:12px}
#pv{background:#111827;padding:6px 14px;display:none;justify-content:space-between;color:#38bdf8;font-size:13px}
.bar{background:#111827;padding:8px;display:flex;gap:6px;align-items:flex-end;border-top:1px solid #1f2937}
textarea{flex:1;background:#090d16;color:#fff;border:1px solid #374151;border-radius:10px;padding:10px;font-size:15px;resize:none;max-height:100px}
.b{background:#1f2937;color:#fff;border:1px solid #374151;width:40px;height:40px;border-radius:10px;font-size:17px}.rec{background:#dc2626}.snd{background:#10b981;font-weight:bold}
</style></head><body><header><h1>🧠 Shaxsiy Super-Ong 🌐</h1><button class="t" onclick="clr()">🗑️</button></header><div id="chat"></div><div id="pv"><span id="pn">📷</span><button class="t" onclick="rm()">❌</button></div><div class="bar"><input type="file" id="fi" accept="image/*" hidden onchange="hi(event)"><button class="b" onclick="fi.click()">📷</button><button class="b" id="mc" onclick="tm()">🎤</button><textarea id="ip" rows="1" placeholder="Yozing, rasm yuklang yoki gapiring..."></textarea><button class="b snd" id="sd" onclick="sn()">➤</button></div><script>
let img=null,rec,act=false;const fmt=s=>s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/(https?:\/\/[^\s<]+)/g,'<a href="$1" target="_blank">$1</a>');
function hi(e){const f=e.target.files.item(0);if(!f)return;const r=new FileReader();r.onload=v=>{const i=new Image();i.onload=()=>{const c=document.createElement("canvas"),s=Math.min(1,1024/i.width);c.width=i.width*s;c.height=i.height*s;c.getContext("2d").drawImage(i,0,0,c.width,c.height);img=c.toDataURL("image/jpeg",.85);pv.style.display="flex";pn.textContent="📷 "+f.name};i.src=v.target.result};r.readAsDataURL(f)}
function rm(){img=null;fi.value="";pv.style.display="none"}
function add(r,t){const d=document.createElement("div");d.className="msg "+r;const c=document.createElement("div");c.innerHTML=fmt(t);d.appendChild(c);if(r==="assistant"){const b=document.createElement("button");b.className="sb";b.textContent="🔊 Eshitish";b.onclick=()=>{speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t);u.lang="uz-UZ";speechSynthesis.speak(u)};d.appendChild(b)}chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d}
async function ld(){try{const d=await(await fetch("/api/history")).json();chat.innerHTML="";if(d.messages&&d.messages.length)d.messages.forEach(m=>add(m.role,m.content));else add("assistant","Salom! Jonli internetga ulanganman 🌐 va o'z fikrimga egaman. Gaplashamiz!")}catch(e){}}
async function sn(){const t=ip.value.trim();if(!t&&!img)return;const im=img;ip.value="";rm();add("user",im?("📷 "+(t||"Rasm")):t);const w=add("assistant","🌐 Internetni tityapman...");sd.disabled=true;try{const d=await(await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:t,image:im})})).json();w.remove();add("assistant",d.reply||("❌ "+d.error))}catch(e){w.textContent="Sahifani yangilang"}sd.disabled=false}
async function clr(){if(confirm("O'chirilsinmi?")){await fetch("/api/clear",{method:"POST"});ld()}}
function tm(){const SR=window.SpeechRecognition||window.webkitSpeechRecognition;if(!SR)return alert("Ovozli boshqaruv yo'q");if(act&&rec){rec.stop();return}rec=new SR();rec.lang="uz-UZ";rec.continuous=false;rec.interimResults=true;rec.onstart=()=>{act=true;mc.classList.add("rec");mc.textContent="⏹";ip.placeholder="Gapiring..."};rec.onresult=e=>{let t="";for(let i=e.resultIndex;i<e.results.length;i++)t+=e.results.item(i).item(0).transcript;ip.value=t};rec.onend=()=>{act=false;mc.classList.remove("rec");mc.textContent="🎤";ip.placeholder="Yozing, rasm yuklang yoki gapiring...";if(ip.value.trim())sn()};rec.start()}
ip.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sn()}});ld();</script></body></html>"""


