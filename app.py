
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

BUSINESS_NAME = "Neochicks Poultry Ltd."
CALL_LINE = "0707787884"

WELCOME_TEXT = (
    "🐣 Karibu *Neochicks Ltd.*\n"
    "The leading incubators supplier in Kenya and East Africa.\n"
    "Click one of the options below and I will answer you:\n\n"
    f"☎️ {CALL_LINE}"
)

MENU_BUTTONS = [
    "Capacities with Prices 💰📦",
    "Delivery Terms 🚚",
    "Troubleshoot my incubators 🛠️",
    "Talk to an Agent 👩🏽‍💼"
]

PAYMENT_NOTE = "Pay on delivery"

def is_after_hours():
    eat_hour = (datetime.utcnow().hour + 3) % 24
    return not (6 <= eat_hour < 23)

AFTER_HOURS_NOTE = "We are currently off till early morning."

CATALOG = [
    {"name":"56 Eggs","capacity":56,"price":13000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2018/12/56-Eggs-solar-electric-incubator-1-600x449.png"},
    {"name":"64 Eggs","capacity":64,"price":14000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/64-Eggs-solar-electric-incubator-e1630976080329-600x450.jpg"},
    {"name":"112 Eggs","capacity":104,"price":19000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/104-Eggs-Incubator-1.png"},
    {"name":"128 Eggs","capacity":128,"price":20000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/128-Eggs-solar-incubator-2.png"},
    {"name":"192 Eggsr","capacity":192,"price":28000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/192-egg-incubator-1-600x600.jpg"},
    {"name":"204 Eggs","capacity":204,"price":30000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2025/07/204-eggs-incubator-600x650.jpg"},
    {"name":"256 Eggs","capacity":256,"price":33000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2023/01/256-eggs-large-photo-600x676.jpeg"},
    {"name":"Neo-264","capacity":264,"price":45000,"solar":False,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/264-Eggs-automatic-incubator-1.jpg"},
    {"name":"300 Eggs","capacity":300,"price":52000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/300-Eggs-solar-incubator.jpg"},
     {"name":"350 Eggs","capacity":350,"price":54000,"solar":True,"free_gen":False,
      "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/300-Eggs-solar-incubator.jpg"},
    {"name":"Neo-352","capacity":352,"price":54000,"solar":False,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/352-Eggs-automatic-incubator-1.jpg"},
    {"name":"Neo-528","capacity":528,"price":63000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/528-Eggs-automatic-Incubator-1-600x425.jpg"},
    {"name":"Neo-616","capacity":616,"price":66000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2022/01/528-inc-600x800.png"},
    {"name":"Neo-880","capacity":880,"price":75000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/880-Eggs-incubator-2.jpg"},
    {"name":"Neo-1056","capacity":1056,"price":80000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1056-full-front-view.jpg"},
    {"name":"Neo-1232","capacity":1232,"price":90000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1232-Eggs-automatic-incubator.jpg"},
    {"name":"Neo-1584","capacity":1584,"price":115000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1584-Eggs-Incubator.jpg"},
    {"name":"Neo-2112","capacity":2112,"price":120000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/2112-Eggs-Incubator.png"},
    {"name":"Neo-4928","capacity":4928,"price":230000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/5280Incubator.jpg"},
    {"name":"Neo-5280","capacity":5280,"price":240000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/5280-Eggs-Incubator.png"},
]


def ksh(n:int) -> str:
    return f"KSh {n:,.0f}"

def product_line(p:dict) -> str:
    tag = " (Solar)" if p.get("solar") else ""
    gen = " + *Free Backup Generator*" if p.get("free_gen") else ""
    return f"- {p['name']}{tag} — {p['capacity']} eggs → {ksh(p['price'])}{gen}"

def price_page_text(page:int=1, per_page:int=6) -> str:
    items = sorted(CATALOG, key=lambda x: x["capacity"])
    total = len(items)
    pages = max(1, (total + per_page - 1)//per_page)
    page = max(1, min(page, pages))
    start = (page-1)*per_page
    chunk = items[start:start+per_page]
    lines = [product_line(p) for p in chunk]
    footer = f"\nPage {page}/{pages}. Type *next*/*back* to browse, or type a *capacity number* (e.g., 204 or 528)."
    return "🐣 *Capacities with Prices*\n" + "\n".join(lines) + footer

def find_by_capacity(cap:int):
    items = sorted(CATALOG, key=lambda x: x["capacity"])
    for p in items:
        if p["capacity"] >= cap:
            return p
    return items[-1] if items else None

SESS = {}

def delivery_eta_text(county: str) -> str:
    key = (county or "").strip().lower().split()[0]
    return "same day" if key == "nairobi" else "24 hours"

def brain_reply(text: str, from_wa: str = "") -> dict:
    t = (text or "").strip()
    low = t.lower()
    sess = SESS.setdefault(from_wa, {"state": None, "page": 1})

    after_note = f"\n\n⏰ {AFTER_HOURS_NOTE}" if is_after_hours() else ""

    if low in {"", "hi", "hello", "menu", "start"}:
        return {"text": WELCOME_TEXT + after_note, "buttons": MENU_BUTTONS}

    if "agent" in low or "talk to an agent" in low:
        SESS[from_wa] = {"state": None, "page": 1}
        return {"text": f"👩🏽‍💼 Connecting you to a Neochicks rep… You can also call {CALL_LINE}."}

    if any(k in low for k in ["capacities", "capacity", "capacities with prices", "prices", "price", "bei", "gharama"]):
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
                               f"Reply with your *county* for delivery ETA and quote. {PAYMENT_NOTE}."}
                if p.get("image"):
                    out.update({"mediaUrl": p["image"], "caption": f"{p['name']} — {ksh(p['price'])}"})
                return out

    if "delivery" in low or "deliver" in low or "delivery terms" in low:
        sess["state"] = "await_county"
        return {"text": f"🚚 Delivery terms: Nairobi → same day; other counties → 24 hours. {PAYMENT_NOTE}.
Which *county* are you in?"}
        

     return {"text": f"🚚 Delivery terms: Nairobi → same day; other counties → 24 hours. {PAYMENT_NOTE}.
Which *county* are you in?"}

    if sess.get("state") == "await_county":
        county = re.sub(r"[^a-z ]", "", low).strip()
        if not county:
            return {"text": "Please type your *county* name (e.g., Nairobi, Nakuru, Mombasa)."}
        sess["state"] = None
        eta = delivery_eta_text(county)
return {
    "text": (
        f"📍 {county.title()} → Typical delivery {eta}. {PAYMENT_NOTE}."
        "\nNeed a recommendation or pro-forma invoice?"
    )
}

    if any(k in low for k in ["troubleshoot", "hatch rate", "problem", "fault", "issue"]):
        sess["state"] = None
        return {"text": (
            "🛠️ Quick checks:
"
            "1) Temp 37.5°C (±0.2)
"
            "2) Humidity 45–55% set / 65% hatch
"
            "3) Turning 3–5×/day (auto OK)
"
            "4) Candle day 7 & 14; remove clears
"
            "5) Ventilation okay (no drafts)

"
            "Still low hatch rate? Type *Talk to an Agent* and our tech will help."
        )}

    if re.search(r"warranty|guarantee", low):
        return {"text": "✅ 12-month warranty + free setup guidance. We also connect you to our technician from your nearest town."}

    if re.search(r"backup|inverter|power|solar", low):
        return {"text": "🔋 Solar panels + battery available (sized per model). We assist to outsource solar packages depending on your incubator power rating."}

    if re.search(r"sell.*chicks|chicks|kienyeji", low):
        return {"text": "🐥 Improved Kienyeji chicks available — 3 days old up to 2 months old. Call: 0793585968."}

    if re.search(r"payment|mpesa|cash", low):
        return {"text": f"💳 Any mode of payment acceptable. {PAYMENT_NOTE}."}

    if re.search(r"include.*solar|price.*include.*solar|solar.*include", low):
        return {"text": "ℹ️ Prices do not include solar panels. We guide you to get the best solar/battery package for your incubator."}

    return {"text": "Got it! Tap *Capacities with Prices*, *Delivery Terms*, *Troubleshoot my incubators*, or *Talk to an Agent*.", "buttons": MENU_BUTTONS}

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
