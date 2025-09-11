from flask import Flask, request, jsonify
import os, json, re, requests
from datetime import datetime

VERIFY_TOKEN   = os.getenv("VERIFY_TOKEN", "changeme")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID= os.getenv("PHONE_NUMBER_ID", "")
GRAPH_BASE     = "https://graph.facebook.com/v20.0"

app = Flask(__name__)

def _headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

def send_text(to: str, body: str):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}}
    r = requests.post(url, headers=_headers(), json=payload, timeout=30); r.raise_for_status(); return r.json()

def send_buttons(to: str, titles, prompt_text="Pick one:"):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    buttons = [{"type":"reply","reply":{"id":f"b{i+1}","title":t[:20]}} for i,t in enumerate(titles[:3])]
    payload = {"messaging_product":"whatsapp","to":to,"type":"interactive",
               "interactive":{"type":"button","body":{"text":prompt_text},"action":{"buttons":buttons}}}
    r = requests.post(url, headers=_headers(), json=payload, timeout=30); r.raise_for_status(); return r.json()

def send_image(to: str, link: str, caption: str = ""):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product":"whatsapp","to":to,"type":"image","image":{"link":link,"caption":caption}}
    r = requests.post(url, headers=_headers(), json=payload, timeout=30); r.raise_for_status(); return r.json()

CATALOG = [
    {"name":"56 Eggs","capacity":56,"price":13000,"solar":True,"free_gen":False,"image":""},
    {"name":"64 Eggs","capacity":64,"price":14000,"solar":True,"free_gen":False,"image":""},
    {"name":"112 Eggs","capacity":104,"price":19000,"solar":True,"free_gen":False,"image":""},
    {"name":"128 Eggs","capacity":128,"price":20000,"solar":True,"free_gen":False,"image":""},
    {"name":"192 Eggsr","capacity":192,"price":28000,"solar":True,"free_gen":False,"image":""},
    {"name":"204 Eggs","capacity":204,"price":30000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2025/07/204-eggs-incubator-600x650.jpg"},
    {"name":"256 Eggs","capacity":256,"price":33000,"solar":True,"free_gen":False,"image":""},
    {"name":"Neo-264","capacity":264,"price":45000,"solar":False,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/264-Eggs-automatic-incubator-1.jpg"},
    {"name":"300 Eggs","capacity":300,"price":52000,"solar":True,"free_gen":False,"image":""},
     {"name":"350 Eggs","capacity":350,"price":54000,"solar":True,"free_gen":False,"image":""},
    {"name":"Neo-352","capacity":352,"price":54000,"solar":False,"free_gen":False,"image":""},
    {"name":"Neo-528","capacity":528,"price":63000,"solar":False,"free_gen":True,"image":""},
    {"name":"Neo-616","capacity":616,"price":66000,"solar":False,"free_gen":True,"image":""},
    {"name":"Neo-880","capacity":880,"price":75000,"solar":False,"free_gen":True,"image":""},
    {"name":"Neo-1056","capacity":1056,"price":80000,"solar":False,"free_gen":True,"image":""},
    {"name":"Neo-1232","capacity":1232,"price":90000,"solar":False,"free_gen":True,"image":""},
    {"name":"Neo-1584","capacity":1584,"price":115000,"solar":False,"free_gen":True,"image":""},
    {"name":"Neo-2112","capacity":2112,"price":120000,"solar":False,"free_gen":True,"image":""},
    {"name":"Neo-4928","capacity":4928,"price":230000,"solar":False,"free_gen":True,"image":""},
    {"name":"Neo-5280","capacity":5280,"price":240000,"solar":False,"free_gen":True,"image":""},
]

DELIVERY_ETA = {"nairobi": "same day"}  # else: 24 hours

WELCOME_TEXT = (
    "🐣 Karibu *Neochicks Poultry Ltd*! 🚛\n\n"
    "Pick an option or ask anything:\n"
    "• Prices 💰\n• Capacities 📦\n• Delivery 🚚\n• Troubleshoot 🛠️\n• Agent 👩🏽‍💼\n\n"
    "This line is *chat-only*. For calls: 0707 787884"
)
MENU_BUTTONS = ["Prices 💰", "Capacities 📦", "Delivery 🚚", "Troubleshoot 🛠️", "Agent 👩🏽‍💼"]

def ksh(n:int) -> str:
    return f"KSh {n:,.0f}"

def product_line(p:dict) -> str:
    tag = " (Solar)" if p.get("solar") else ""
    gen = " + *Free Backup Generator*" if p.get("free_gen") else ""
    return f"- {p['name']}{tag} — {ksh(p['price'])}{gen}"

def price_page_text(page:int=1, per_page:int=12) -> str:
    items = sorted(CATALOG, key=lambda x: x["capacity"])
    total = len(items)
    pages = max(1, (total + per_page - 1)//per_page)
    page = max(1, min(page, pages))
    start = (page-1)*per_page
    chunk = items[start:start+per_page]
    lines = [product_line(p) for p in chunk]
    footer = f"\n\nPage {page}/{pages}. Type *next* or *back* to see more capacities, or type a *number of eggs that you have in mind* (e.g. 64, 100, 204, 528, 1000 etc)."
    return "🐣 *Neochicks Incubators Price List*\n" + "\n".join(lines) + footer

def find_by_capacity(cap:int):
    items = sorted(CATALOG, key=lambda x: x["capacity"])
    for p in items:
        if p["capacity"] >= cap:
            return p
    return items[-1] if items else None

SESS = {}  # {phone: {"state": "...", "page": int, "batch": int}}

def is_after_hours():
    hour_utc = datetime.utcnow().hour
    return not (5 <= hour_utc < 15)  # ≈ 08:30–18:00 EAT

def brain_reply(text: str, from_wa: str = "") -> dict:
    t = (text or "").strip()
    low = t.lower()
    sess = SESS.setdefault(from_wa, {"state": None, "page": 1})

    after_note = "\n\n⏰ We’re back at 8:30am EAT. I can still help with basics now." if is_after_hours() else ""

    if low in {"", "hi", "hello", "menu", "start"}:
        return {"text": WELCOME_TEXT + after_note, "buttons": MENU_BUTTONS}

    if "agent" in low:
        SESS[from_wa] = {"state": None, "page": 1}
        return {"text": "👩🏽‍💼 Connecting you to a Neochicks rep… You can also call 0707 787884."}

    if any(k in low for k in ["prices", "price", "bei", "gharama"]):
        sess["state"] = "prices"; sess["page"] = 1
        return {"text": price_page_text(page=1)}

    if sess.get("state") == "prices" and low in {"next", "more"}:
        sess["page"] += 1
        return {"text": price_page_text(page=sess["page"])}
    if sess.get("state") == "prices" and low in {"back", "prev", "previous"}:
        sess["page"] = max(1, sess["page"]-1)
        return {"text": price_page_text(page=sess["page"])}

    if sess.get("state") == "prices":
        m = re.search(r"([0-9]{2,5})", low)
        if m:
            cap = int(m.group(1))
            p = find_by_capacity(cap)
            if p:
                extra = " (Solar)" if p["solar"] else ""
                gen = "\n🎁 Includes *Free Backup Generator*" if p["free_gen"] else ""
                out = {"text": f"📦 *{p['name']}*{extra}\n"
                               f"Capacity: {p['capacity']} eggs\n"
                               f"Price: {ksh(p['price'])}{gen}\n\n"
                               f"We offer Free Delivery and Training."}
                if p.get("image"):
                    out.update({"mediaUrl": p["image"], "caption": f"{p['name']} — {ksh(p['price'])}"})
                return out

    if any(k in low for k in ["capacities", "recommend", "size"]):
        sess["state"] = "await_batch"
        return {"text": "Great—what’s your *batch size per set* (eggs)?"}

    if sess.get("state") == "await_batch":
        m = re.search(r"([0-9]{2,5})", low)
        if not m:
            return {"text": "Enter a number for your *batch size* (e.g., 264)."}
        sess["batch"] = int(m.group(1))
        sess["state"] = "await_sets"
        return {"text": "How many *sets per month* do you plan (e.g., 2)?"}

    if sess.get("state") == "await_sets":
        m = re.search(r"([0-9]{1,3})", low)
        if not m:
            return {"text": "Enter a number of *sets per month* (e.g., 2)."}
        sets_pm = int(m.group(1))
        batch = int(sess.get("batch", 0))
        best = find_by_capacity(batch)
        alt  = find_by_capacity(max(1, batch//2))
        msg = (f"📦 Recommended: *{best['name']}* ({best['capacity']} eggs) – {ksh(best['price'])}."
               + (" (Solar)" if best["solar"] else "")
               + ("\n🎁 Includes *Free Backup Generator*" if best["free_gen"] else ""))
        if alt and alt["capacity"] < best["capacity"]:
            msg += f"\nOr 2× *{alt['name']}* for staggered hatches."
        sess["state"] = None
        out = {"text": msg + "\n\nWant delivery ETA? Tell me your *county*."}
        if best.get("image"):
            out.update({"mediaUrl": best["image"], "caption": f"{best['name']} — {ksh(best['price'])}"})
        return out

    if "delivery" in low or "deliver" in low:
        sess["state"] = "await_county"
        return {"text": "🚚 We deliver countrywide. Payment is *on delivery*. Which *county* are you in?"}

    if sess.get("state") == "await_county":
        county = re.sub(r"[^a-z ]", "", low).strip()
        if not county:
            return {"text": "Please type your *county* name (e.g., Nairobi, Nakuru, Mombasa)."}
        sess["state"] = None
        key = county.split()[0].lower()
        eta = "same day" if key == "nairobi" else "24 hours"
        return {"text": f"📍 {county.title()} → Typical delivery {eta}. *Pay on delivery.*"}

    if any(k in low for k in ["troubleshoot", "hatch rate", "problem"]):
        sess["state"] = None
        return {"text": (
            "🛠️ Quick checks:\n"
            "1) Temp 37.5°C (±0.2)\n"
            "2) Humidity 45–55% set / 65% hatch\n"
            "3) Turning 3–5×/day (auto OK)\n"
            "4) Candle day 7 & 14; remove clears\n"
            "5) Ventilation okay (no drafts)\n\n"
            "Still low hatch rate? Type *Agent* and our tech will help."
        )}

    if re.search(r"warranty|guarantee", low):
        return {"text": "✅ 12-month warranty + free setup guidance by phone/video."}
    if re.search(r"backup|inverter|power", low):
        return {"text": "🔋 Power backup available (inverter + battery). We size it to your model—ask for a quote."}

    return {"text": "Got it! Type *Prices*, *Capacities*, *Delivery*, *Troubleshoot*, or *Agent*.", "buttons": MENU_BUTTONS}

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.get("/webhook")
def verify():
    mode = request.args.get("hub.mode"); token = request.args.get("hub.verify_token"); challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN: return challenge, 200
    return "forbidden", 403

@app.post("/webhook")
def webhook():
    data = request.get_json(force=True, silent=True) or {}
    try:
        entry = (data.get("entry") or [{}])[0]; changes = (entry.get("changes") or [{}])[0]; value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages: return "no message", 200

        msg = messages[0]; from_wa = msg.get("from"); text = ""
        if msg.get("type") == "text":
            text = msg.get("text", {}).get("body", "")
        elif msg.get("type") == "interactive":
            inter = msg.get("interactive", {})
            if inter.get("type") == "button_reply":
                text = inter.get("button_reply", {}).get("title", "")
            elif inter.get("type") == "list_reply":
                text = inter.get("list_reply", {}).get("title", "")

        reply = brain_reply(text, from_wa)

        if reply.get("text"): send_text(from_wa, reply["text"])
        if reply.get("buttons"): send_buttons(from_wa, reply["buttons"])
        if reply.get("mediaUrl"): send_image(from_wa, reply["mediaUrl"], reply.get("caption", ""))

        return "ok", 200
    except Exception as e:
        print("Webhook error:", e, "payload:", json.dumps(data)[:1000]); return "error", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 3000)))
