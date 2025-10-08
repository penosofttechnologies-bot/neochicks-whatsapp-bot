from flask import Flask, request, jsonify
import os, json, re, requests, smtplib, ssl
from datetime import datetime

# =========================
# Config (env variables)
# =========================

COUNTIES = {
    "baringo","bomet","bungoma","busia","elgeyo marakwet","embu","garissa","homa bay","isiolo",
    "kajiado","kakamega","kericho","kiambu","kilifi","kirinyaga","kisii","kisumu","kitui",
    "kwale","laikipia","lamu","machakos","makueni","mandera","marsabit","meru","migori","mombasa",
    "murang'a","muranga","nairobi","nakuru","nandi","narok","nyamira","nyandarua","nyeri",
    "samburu","siaya","taita taveta","tana river","tharaka nithi","trans nzoia","turkana",
    "uasin gishu","vihiga","wajir","west pokot"
}

def guess_county(text: str) -> str | None:
    # normalize & keep letters/spaces
    cleaned = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if not cleaned:
        return None
    # exact match
    if cleaned in COUNTIES:
        return cleaned
    # handle trailing "county"
    if cleaned.endswith(" county"):
        c = cleaned[:-7].strip()
        if c in COUNTIES:
            return c
    # 2-word counties often typed as one or two words; try compacting spaces
    parts = cleaned.split()
    if len(parts) in (2, 3):
        joined = " ".join(parts)
        if joined in COUNTIES:
            return joined
    return None

VERIFY_TOKEN   = os.getenv("VERIFY_TOKEN", "changeme")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID= os.getenv("PHONE_NUMBER_ID", "")
GRAPH_BASE     = "https://graph.facebook.com/v20.0"

app = Flask(__name__)

# =========================
# Email helper (SendGrid HTTPS API)
# =========================
def send_email(subject: str, body: str):
    api_key = os.getenv("SENDGRID_API_KEY", "")
    sender  = os.getenv("SENDGRID_FROM", "")
    to      = os.getenv("SALES_EMAIL", sender)

    if not (api_key and sender and to):
        print("Email not sent—missing SENDGRID_API_KEY/SENDGRID_FROM/SALES_EMAIL")
        return False

    try:
        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "personalizations": [{"to": [{"email": to}]}],
                "from": {"email": sender, "name": "Neochicks Bot"},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            },
            timeout=20,
        )
        if r.status_code in (200, 202):
            return True
        print("SendGrid error:", r.status_code, r.text)
        return False
    except Exception as e:
        print("SendGrid exception:", e)
        return False

# =========================
# HTTP send helpers
# =========================
def _headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

def send_text(to: str, body: str):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}}
    r = requests.post(url, headers=_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_buttons(to: str, titles, prompt_text="Pick one:"):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    buttons = [{"type":"reply","reply":{"id":f"b{i+1}","title":t[:20]}} for i,t in enumerate(titles[:3])]
    payload = {"messaging_product":"whatsapp","to":to,"type":"interactive",
               "interactive":{"type":"button","body":{"text":prompt_text},"action":{"buttons":buttons}}}
    r = requests.post(url, headers=_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_image(to: str, link: str, caption: str = ""):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product":"whatsapp","to":to,"type":"image","image":{"link":link,"caption":caption}}
    r = requests.post(url, headers=_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# =========================
# Branding & Messages
# =========================
BUSINESS_NAME = "Neochicks Poultry Ltd."
CALL_LINE = "0707787884"

WELCOME_TEXT = (
    "🐣 Karibu *Neochicks Ltd.*\n"
    "The leading incubators supplier in Kenya and East Africa.\n"
    "Click one of the options below and I will answer you:\n\n"
    "☎️ " + CALL_LINE
)

MENU_BUTTONS = [
    "Prices/Capacities 💰📦",
    "Delivery Terms 🚚",
    "Incubator issues 🛠️",
    "Talk to an Agent 👩🏽‍💼"
]

PAYMENT_NOTE = "Pay on delivery"

# Business hours in EAT: 06:00–23:00
def is_after_hours():
    eat_hour = (datetime.utcnow().hour + 3) % 24
    # Open when hour in [06, 23); closed otherwise
    return not (6 <= eat_hour < 23)

AFTER_HOURS_NOTE = "We are currently off till early morning."

# =========================
# Catalog (Prices & Images)
# =========================
CATALOG = [
    {"name":"56 Eggs","capacity":56,"price":13000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2018/12/56-Eggs-solar-electric-incubator-1-600x449.png"},
    {"name":"64 Eggs","capacity":64,"price":14000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/64-Eggs-solar-electric-incubator-e1630976080329-600x450.jpg"},
    {"name":"112 Eggs","capacity":104,"price":19000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/104-Eggs-Incubator-1.png"},
    {"name":"128 Eggs","capacity":128,"price":20000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/128-Eggs-solar-incubator-2.png"},
    {"name":"192 Eggs","capacity":192,"price":28000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/192-egg-incubator-1-600x600.jpg"},
    {"name":"204 Eggs","capacity":204,"price":30000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2025/07/204-eggs-incubator-600x650.jpg"},
    {"name":"256 Eggs","capacity":256,"price":33000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2023/01/256-eggs-large-photo-600x676.jpeg"},
    {"name":"264 Eggs","capacity":264,"price":45000,"solar":False,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/264-Eggs-automatic-incubator-1.jpg"},
    {"name":"300 Eggs","capacity":300,"price":52000,"solar":True,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/300-Eggs-solar-incubator.jpg"},
     {"name":"350 Eggs","capacity":350,"price":54000,"solar":True,"free_gen":False,
      "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/300-Eggs-solar-incubator.jpg"},
    {"name":"352 Eggs","capacity":352,"price":54000,"solar":False,"free_gen":False,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/352-Eggs-automatic-incubator-1.jpg"},
    {"name":"528 Eggs","capacity":528,"price":63000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/528-Eggs-automatic-Incubator-1-600x425.jpg"},
    {"name":"616 Eggs","capacity":616,"price":66000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2022/01/528-inc-600x800.png"},
    {"name":"880 Eggs","capacity":880,"price":75000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/880-Eggs-incubator-2.jpg"},
    {"name":"1056 Eggs","capacity":1056,"price":80000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1056-full-front-view.jpg"},
    {"name":"1232 Eggs","capacity":1232,"price":90000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1232-Eggs-automatic-incubator.jpg"},
    {"name":"1584 Eggs","capacity":1584,"price":115000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1584-Eggs-Incubator.jpg"},
    {"name":"2112 Eggs","capacity":2112,"price":120000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/2112-Eggs-Incubator.png"},
    {"name":"4928 Eggs","capacity":4928,"price":230000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/5280Incubator.jpg"},
    {"name":"5280 Eggs","capacity":5280,"price":240000,"solar":False,"free_gen":True,
     "image":"https://neochickspoultry.com/wp-content/uploads/2021/09/5280-Eggs-Incubator.png"},
]

# =========================
# Catalog utilities & Session
# =========================
def ksh(n:int) -> str:
    return f"KSh {n:,.0f}"

def product_line(p:dict) -> str:
    tag = " (Solar/Electric)" if p.get("solar") else ""
    gen = " + *Free Backup Generator*" if p.get("free_gen") else ""
    return f"- {p['name']}{tag} → {ksh(p['price'])}{gen}"

def price_page_text(page:int=1, per_page:int=12) -> str:
    items = sorted(CATALOG, key=lambda x: x["capacity"])
    total = len(items)
    pages = max(1, (total + per_page - 1)//per_page)
    page = max(1, min(page, pages))
    start = (page-1)*per_page
    chunk = items[start:start+per_page]
    lines = [product_line(p) for p in chunk]
    footer = (
        "\n\nPage " + str(page) + " of " + str(pages) +
        ". Type *next* to see more, or type a *capacity that you have in mind* (e.g., 100, 200, 528, 1000 etc)."
    )
    return "🐣 *Capacities with Prices*\n" + "\n".join(lines) + footer

def find_by_capacity(cap:int):
    items = sorted(CATALOG, key=lambda x: x["capacity"])
    for p in items:
        if p["capacity"] >= cap:
            return p
    return items[-1] if items else None

SESS = {}  # {phone: {"state": "...", "page": int, "batch": int}}

# Delivery rule: Nairobi same day, others 24 hours
def delivery_eta_text(county: str) -> str:
    key = (county or "").strip().lower().split()[0]
    return "same day" if key == "nairobi" else "24 hours"

# =========================
# Pro-forma builder (NEW helper)
# =========================
def build_proforma_text(sess: dict) -> str:
    p = sess.get("last_product") or {}
    county = sess.get("last_county", "-")
    eta = sess.get("last_eta", "24 hours")
    model = p.get("name", "—")
    cap   = p.get("capacity", "—")
    price = ksh(p.get("price", 0)) if "price" in p else "—"
    name  = sess.get("customer_name", "")
    phone = sess.get("customer_phone", "")

    return (
        "🧾 *Pro-Forma Invoice*\n"
        f"Customer: {name}\n"
        f"Phone: {phone}\n"
        f"County: {county}\n"
        f"Item: {model} ({cap} eggs)\n"
        f"Price: {price}\n"
        f"Delivery: {eta} | {PAYMENT_NOTE}\n"
        "—\n"
        "If this looks correct, reply *CONFIRM* to place the order, or type *EDIT* to change details."
    )

# =========================
# Brain / Router
# =========================
def brain_reply(text: str, from_wa: str = "") -> dict:
    t = (text or "").strip()
    low = t.lower()
    sess = SESS.setdefault(from_wa, {"state": None, "page": 1})

    after_note = ("\n\n⏰ " + AFTER_HOURS_NOTE) if is_after_hours() else ""

    # MENU
    if low in {"", "hi", "hello", "menu", "start", "want", "incubator", "need an incubator"}:
        return {"text": WELCOME_TEXT + after_note, "buttons": MENU_BUTTONS}

    # AGENT
    if ("agent" in low) or ("talk to an agent" in low):
        SESS[from_wa] = {"state": None, "page": 1}
        return {"text": "👩🏽‍💼 Connecting you to a Neochicks rep… You can also call " + CALL_LINE + "."}

    # CAPACITIES WITH PRICES (combined intent)
    if any(k in low for k in ["capacities", "capacity", "capacities with prices", "prices", "price", "bei", "gharama"]):
        sess["state"] = "prices"
        sess["page"] = 1
        return {"text": price_page_text(page=1)}

    # Paging
    if sess.get("state") == "prices" and low in {"next", "more"}:
        sess["page"] += 1
        return {"text": price_page_text(page=sess["page"])}
    if sess.get("state") == "prices" and low in {"back", "prev", "previous"}:
        sess["page"] = max(1, sess["page"]-1)
        return {"text": price_page_text(page=sess["page"])}

    # Capacity-specific detail while in prices
    if sess.get("state") == "prices":
        m = re.search(r"([0-9]{2,5})", low)
        if m:
            cap = int(m.group(1))
            p = find_by_capacity(cap)
            if p:
                extra = " (Solar)" if p["solar"] else ""
                gen = "\n🎁 Includes *Free Backup Generator*" if p["free_gen"] else ""
                text = (
                    "📦 *" + p['name'] + "*" + extra + "\n"
                    "Capacity: " + str(p['capacity']) + " eggs\n"
                    "Price: " + ksh(p['price']) + gen + "\n\n"
                    "Reply with your *county* for delivery ETA and quote. " + PAYMENT_NOTE + "."
                )
                out = {"text": text}
                if p.get("image"):
                    out.update({"mediaUrl": p["image"], "caption": p['name'] + " — " + ksh(p['price'])})

                # remember last viewed product for quoting
                sess["last_product"] = p

                return out

    # DELIVERY TERMS  -> ask county -> (NEW) ask name -> phone -> pro-forma -> CONFIRM
    if ("delivery" in low) or ("deliver" in low) or ("delivery terms" in low):
        sess["state"] = "await_county"
        return {"text": "🚚 Delivery terms: Nairobi → same day; other counties → 24 hours. " + PAYMENT_NOTE + ".\nWhich *county* are you in?"}

    if sess.get("state") == "await_county":
        county = re.sub(r"[^a-z ]", "", low).strip()
        if not county:
            return {"text": "Please type your *county* name (e.g., Nairobi, Nakuru, Mombasa)."}
        eta = delivery_eta_text(county)

        # remember location
        sess["last_county"] = county.title()
        sess["last_eta"] = eta

        # continue to name capture
        sess["state"] = "await_name"
        return {"text": "📍 " + county.title() + " → Typical delivery " + eta + ". " + PAYMENT_NOTE + ".\nGreat! Please share your *full name* for the pro-forma."}

    # Ask for customer name
    if sess.get("state") == "await_name":
        name = t.strip()
        if len(name) < 2:
            return {"text": "Please type your *full name* (e.g., Jane Wanjiku)."}
        sess["customer_name"] = name
        sess["state"] = "await_phone"
        return {"text": "Thanks! Now your *phone number* (for delivery coordination):"}

    # Ask for phone, build pro-forma, ask to CONFIRM
    if sess.get("state") == "await_phone":
        phone = re.sub(r"[^0-9+ ]", "", t)
        if len(re.sub(r"\D", "", phone)) < 9:
            return {"text": "That phone seems short. Please type a valid phone (e.g., 07XX... or +2547...)."}

        sess["customer_phone"] = phone
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    # TROUBLESHOOT
    if any(k in low for k in ["troubleshoot", "hatch rate", "problem", "fault", "issue"]):
        sess["state"] = None
        return {"text": (
            "🛠️ Quick checks:\n"
            "1) Temp 37.8°C (±0.2)\n"
            "2) Humidity 55–60% set / 65% hatch\n"
            "3) Turning 3–5×/day (auto OK)\n"
            "4) Candle day 7 & 14; remove clears\n"
            "5) Ventilation okay (no drafts)\n"
            "6) Disinfection after hatching?\n\n"
            "Do you check all abobe? Type *Talk to us: 0707787884* and we will help."
        )}

    # =========================
    # FAQs & Extras
    # =========================
    if re.search(r"warranty|guarantee", low):
        return {"text": "✅ 12-month warranty + free setup guidance. We also connect you to our technician from your nearest town."}

    if re.search(r"backup|inverter|power|solar", low):
        return {"text": "🔋 Solar panels + battery available (sized per model). We assist to outsource solar packages depending on your incubator power rating."}

    if re.search(r"sell.*chicks|\\bchicks\\b|kienyeji", low):
        return {"text": "🐥 Improved Kienyeji chicks available — 3 days old up to 2 months old. Call: 0793585968."}

    if re.search(r"payment|mpesa|cash", low):
        return {"text": "💳 Any mode of payment acceptable. " + PAYMENT_NOTE + "."}

    if re.search(r"include.*solar|price.*include.*solar|solar.*include", low):
        return {"text": "ℹ️ Prices do not include solar panels. We guide you to get the best solar/battery package for your incubator."}

    # YES to recommendation / pro-forma (adjusted to collect name/phone first)
    if low in {"yes", "yeah", "yep", "ok", "okay", "sure", "invoice", "profoma", "pro-forma", "quote", "quotation", "recommendation"} and sess.get("state") in {"await_quote", None}:
        product = sess.get("last_product")
        county  = sess.get("last_county")

        if not product:
            sess["state"] = "prices"
            return {"text": (
                "Great! Tell me the capacity you want (e.g., 204 or 528) so I can prepare your quote.\n\n"
                + price_page_text(page=1)
            )}

        if not county:
            sess["state"] = "await_county"
            return {"text": "Which *county* are you in? (e.g., Nairobi, Nakuru, Mombasa)"}

        # We have product+county; proceed to name capture
        sess["state"] = "await_name"
        return {"text": "Perfect. Please share your *full name* for the pro-forma."}

    # --------------------------------
    # EDIT menu & edit states (NEW)
    # --------------------------------
    if sess.get("state") == "await_confirm":
        if "edit" in low:
            sess["state"] = "edit_menu"
            return {"text": (
                "What would you like to change?\n"
                "1) Name\n"
                "2) Phone\n"
                "3) County\n"
                "4) Model (capacity)\n\n"
                "Reply with *1, 2, 3,* or *4*."
            )}

    # --- EDIT menu selection ---
    if sess.get("state") == "edit_menu":
        choice = re.sub(r"[^0-9a-z ]", "", low).strip()
        if choice in {"1", "name"}:
            sess["state"] = "edit_name"
            return {"text": "Okay — please type the *correct full name*:"}
        if choice in {"2", "phone"}:
            sess["state"] = "edit_phone"
            return {"text": "Okay — please type the *correct phone number* (07XX... or +2547...):"}
        if choice in {"3", "county"}:
            sess["state"] = "edit_county"
            return {"text": "Okay — please type your *county* (e.g., Nairobi, Nakuru, Mombasa):"}
        if choice in {"4", "model", "capacity"}:
            sess["state"] = "edit_model"
            return {"text": "Type the *capacity number* you want (e.g., 204, 528, 1056):"}
        return {"text": "Please reply with *1, 2, 3,* or *4*."}

    # --- Edit Name ---
    if sess.get("state") == "edit_name":
        name = (t or "").strip()
        if len(name) < 2:
            return {"text": "That looks too short. Please type your *full name* (e.g., Jane Wanjiku)."}
        sess["customer_name"] = name
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    # --- Edit Phone ---
    if sess.get("state") == "edit_phone":
        phone = re.sub(r"[^0-9+ ]", "", (t or ""))
        if len(re.sub(r"\D", "", phone)) < 9:
            return {"text": "That phone seems short. Please type a valid phone (e.g., 07XX... or +2547...)."}
        sess["customer_phone"] = phone
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    # --- Edit County ---
    if sess.get("state") == "edit_county":
        county_raw = (t or "").strip()
        county = re.sub(r"[^a-z ]", "", county_raw.lower()).strip()
        if not county:
            return {"text": "Please type your *county* name (e.g., Nairobi, Nakuru, Mombasa)."}
        sess["last_county"] = county.title()
        sess["last_eta"] = delivery_eta_text(county)
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    # --- Edit Model (capacity) ---
    if sess.get("state") == "edit_model":
        m = re.search(r"([0-9]{2,5})", low)
        if not m:
            return {"text": "Please type just the *capacity number* (e.g., 204, 528, 1056)."}
        cap = int(m.group(1))
        p = find_by_capacity(cap)
        if not p:
            return {"text": "I couldn't find that capacity. Try 204, 264, 528, 1056, 5280 etc."}
        # update product
        sess["last_product"] = p
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    # --------------------------------
    # CONFIRM order -> send email
    # --------------------------------
    if low.strip() == "confirm" and sess.get("state") == "await_confirm":
        p = sess.get("last_product") or {}
        county = sess.get("last_county", "-")
        eta = sess.get("last_eta", delivery_eta_text(county))
        subject = "ORDER CONFIRMED — " + p.get("name","Model") + " for " + sess.get("customer_name","Customer")
        body = (
            "New order confirmation from WhatsApp bot\n\n"
            "Customer Name: " + sess.get("customer_name","") + "\n"
            "Customer Phone: " + sess.get("customer_phone","") + "\n"
            "County: " + county + "\n"
            "Model: " + p.get("name","") + "\n"
            "Capacity: " + str(p.get("capacity","")) + "\n"
            "Price: " + ksh(p.get("price",0)) + "\n"
            "Delivery ETA: " + eta + "\n"
            "Payment: " + PAYMENT_NOTE + "\n"
            "Timestamp: " + datetime.utcnow().isoformat() + "Z\n"
        )
        ok = send_email(subject, body)
        sess["state"] = None
        if ok:
            return {"text": "✅ Order confirmed! Our team will contact you shortly to finalize delivery. Thank you for choosing Neochicks."}
        else:
            return {"text": "✅ Order confirmed! (Heads up: email notification failed, but we have your details. A rep will reach out shortly.)"}

    # -------------------------------
    # Stateless county detection (now leads into name capture)
    # -------------------------------
    c_guess = guess_county(low)
    if c_guess:
        eta = delivery_eta_text(c_guess)
        sess["last_county"] = c_guess.title()
        sess["last_eta"] = eta
        sess["state"] = "await_name"
        return {"text": "📍 " + c_guess.title() + " → Typical delivery " + eta + ". " + PAYMENT_NOTE + ".\nGreat! Please share your *full name* for the pro-forma."}

    # -------------------------------
    # Default / fallback reply
    # -------------------------------
    return {"text": "Got it! Tap *Prices/Capacities*, *Delivery Terms*, *Incubator issues*, or *Talk to an Agent*.", "buttons": MENU_BUTTONS}

# =========================
# Flask routes
# =========================
@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.get("/webhook")
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403

@app.post("/webhook")
def webhook():
    data = request.get_json(force=True, silent=True) or {}
    try:
        entry   = (data.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value   = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return "no message", 200

        msg = messages[0]
        from_wa = msg.get("from")
        text = ""

        if msg.get("type") == "text":
            text = msg.get("text", {}).get("body", "")
        elif msg.get("type") == "interactive":
            inter = msg.get("interactive", {})
            if inter.get("type") == "button_reply":
                text = inter.get("button_reply", {}).get("title", "")
            elif inter.get("type") == "list_reply":
                text = inter.get("list_reply", {}).get("title", "")

        reply = brain_reply(text, from_wa)

        if reply.get("text"):
            send_text(from_wa, reply["text"])
        if reply.get("buttons"):
            send_buttons(from_wa, reply["buttons"])
        if reply.get("mediaUrl"):
            send_image(from_wa, reply["mediaUrl"], reply.get("caption", ""))

        return "ok", 200
    except Exception as e:
        print("Webhook error:", e, "payload:", json.dumps(data)[:1000])
        return "error", 200

@app.get("/testmail")
def testmail():
    ok = send_email("Neochicks Test Email", "It works! ✅")
    return ("OOOKAY" if ok else "FAIL"), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 3000)))
