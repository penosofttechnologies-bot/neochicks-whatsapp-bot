"""
Neochicks WhatsApp Bot (patched version)

This Flask application integrates with the WhatsApp Business Cloud API to provide an
interactive bot for Neochicks Poultry Ltd. Customers can inquire about incubator
capacities and prices, check delivery terms, troubleshoot their incubators, and
place orders. When an order is confirmed the bot logs the order, sends an
email notification to the sales team, and delivers a pro‑forma invoice as a
PDF via WhatsApp.

This patched version improves reliability around order confirmation and pro‑forma
invoice generation:

* **Order confirmation** now uses a case‑insensitive regular expression to
  recognise variations of "CONFIRM". This resolves an issue where some users
  were not able to trigger confirmation if they typed "CONFIRM" with
  additional whitespace or differing case.
* **PDF delivery fallback**: if sending the PDF document through WhatsApp
  fails for any reason, the bot falls back to sending a text message with a
  direct download link to the PDF. This ensures customers always receive
  access to their pro‑forma invoice even if the media API encounters an
  intermittent error.

The rest of the application remains unchanged, including stateless county
detection and editable pro‑forma details.

"""

from flask import Flask, request, jsonify, send_file, abort
import os, json, re, requests, sqlite3, io
from datetime import datetime, timedelta
from fpdf import FPDF  # pip install fpdf==1.7.2

# =========================
# Config (env variables)
# =========================
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "changeme")
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GRAPH_BASE      = "https://graph.facebook.com/v20.0"

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
SENDGRID_FROM    = os.getenv("SENDGRID_FROM", "")
SALES_EMAIL      = os.getenv("SALES_EMAIL", SENDGRID_FROM)

ADMIN_TOKEN       = os.getenv("ADMIN_TOKEN", "")
FOLLOWUP_DELAY_MIN= int(os.getenv("FOLLOWUP_DELAY_MIN", "180"))
DB_PATH = os.getenv("DB_PATH", "/tmp/neochicks.db")

BUSINESS_NAME = "Neochicks Poultry Ltd."
CALL_LINE     = "0707787884"
PAYMENT_NOTE  = "Pay on delivery"
AFTER_HOURS_NOTE = "We are currently off till early morning."

# List of recognised counties for stateless detection
COUNTIES = {
    "baringo","bomet","bungoma","busia","elgeyo marakwet","embu","garissa","homa bay","isiolo",
    "kajiado","kakamega","kericho","kiambu","kilifi","kirinyaga","kisii","kisumu","kitui",
    "kwale","laikipia","lamu","machakos","makueni","mandera","marsabit","meru","migori","mombasa",
    "murang'a","muranga","nairobi","nakuru","nandi","narok","nyamira","nyandarua","nyeri",
    "samburu","siaya","taita taveta","tana river","tharaka nithi","trans nzoia","turkana",
    "uasin gishu","vihiga","wajir","west pokot"
}

def guess_county(text: str) -> str | None:
    """Guess a Kenyan county name from free‑form text.

    Removes non‑letter characters, normalises whitespace and attempts to match
    against a known list of counties. Returns the county name in lower case or
    ``None`` if no match is found.
    """
    cleaned = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if not cleaned:
        return None
    if cleaned in COUNTIES:
        return cleaned
    # handle trailing " county"
    if cleaned.endswith(" county"):
        c = cleaned[:-7].strip()
        if c in COUNTIES:
            return c
    # join multi‑word names
    parts = cleaned.split()
    if len(parts) in (2, 3):
        joined = " ".join(parts)
        if joined in COUNTIES:
            return joined
    return None
  

# =========================
# Flask
# =========================
app = Flask(__name__)
@app.get("/")
def index():
    return (
        "<h2>Neochicks WhatsApp Bot</h2>"
        "<p>Status: <a href='/health'>/health</a></p>"
        "<p>Webhook: /webhook (Meta will call this)</p>"
        "<p>Invoice sample: /invoice/&lt;ORDER_ID&gt;.pdf (after confirmation)</p>"
    ), 200


# =========================
# WhatsApp helpers
# =========================
def _wa_headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

def send_text(to: str, body: str):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}}
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_buttons(to: str, titles, prompt_text="Pick one:"):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    buttons = [{"type":"reply","reply":{"id":f"b{i+1}","title":t[:20]}} for i,t in enumerate(titles[:3])]
    payload = {"messaging_product":"whatsapp","to":to,"type":"interactive",
               "interactive":{"type":"button","body":{"text":prompt_text},"action":{"buttons":buttons}}}
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_image(to: str, link: str, caption: str = ""):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product":"whatsapp","to":to,"type":"image","image":{"link":link,"caption":caption}}
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_document(to: str, link: str, filename: str, caption: str = ""):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product":"whatsapp","to":to,"type":"document",
               "document":{"link":link,"filename":filename,"caption":caption}}
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# =========================
# Email (SendGrid HTTPS)
# =========================
def send_email(subject: str, body: str):
    """Send a plain text email via SendGrid.

    Returns ``True`` if the email was accepted by SendGrid, otherwise
    ``False``. Prints an error to the console if configuration is missing.
    """
    if not (SENDGRID_API_KEY and SENDGRID_FROM and SALES_EMAIL):
        print("Email not sent—missing SENDGRID_API_KEY/SENDGRID_FROM/SALES_EMAIL")
        return False
    try:
        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}","Content-Type":"application/json"},
            json={
                "personalizations": [{"to": [{"email": SALES_EMAIL}]}],
                "from": {"email": SENDGRID_FROM, "name": "Neochicks Bot"},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            },
            timeout=20,
        )
        return r.status_code in (200, 202)
    except Exception as e:
        print("SendGrid exception:", e)
        return False

# =========================
# DB (SQLite) — for order log & followups
# =========================
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
              id TEXT PRIMARY KEY,
              wa_from TEXT,
              customer_name TEXT,
              customer_phone TEXT,
              county TEXT,
              model TEXT,
              capacity INTEGER,
              price INTEGER,
              eta TEXT,
              created_at_utc TEXT,
              followup_due_utc TEXT,
              followup_sent INTEGER DEFAULT 0
            )
        """)
        conn.commit()

def new_order_id():
    ts = datetime.utcnow().strftime("%y%m%d%H%M%S")
    return f"NEO-{ts}"

def insert_order(row: dict):
    with db() as conn:
        conn.execute("""
            INSERT INTO orders (id, wa_from, customer_name, customer_phone, county, model, capacity, price, eta,
                                created_at_utc, followup_due_utc, followup_sent)
            VALUES (:id, :wa_from, :customer_name, :customer_phone, :county, :model, :capacity, :price, :eta,
                    :created_at_utc, :followup_due_utc, 0)
        """, row)
        conn.commit()

def due_followups(now_iso: str):
    with db() as conn:
        cur = conn.execute("""
            SELECT * FROM orders
            WHERE followup_sent=0 AND followup_due_utc <= ?
            ORDER BY followup_due_utc ASC
        """, (now_iso,))
        return [dict(r) for r in cur.fetchall()]

def mark_followup_sent(order_id: str):
    with db() as conn:
        conn.execute("UPDATE orders SET followup_sent=1 WHERE id=?", (order_id,))
        conn.commit()

def get_recent_orders(limit=50):
    with db() as conn:
        cur = conn.execute("SELECT * FROM orders ORDER BY created_at_utc DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

def get_order(order_id: str):
    with db() as conn:
        cur = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        r = cur.fetchone()
        return dict(r) if r else None

# =========================
# Catalog & utilities
# =========================
def ksh(n:int) -> str:
    return f"KSh {n:,.0f}"

def is_after_hours():
    # EAT = UTC+3
    eat_hour = (datetime.utcnow().hour + 3) % 24
    return not (6 <= eat_hour < 23)

MENU_BUTTONS = [
    "Prices/Capacities 💰📦",
    "Delivery Terms 🚚",
    "Incubator issues 🛠️",
    "Talk to an Agent 👩🏽‍💼"
]

CATALOG = [
    {"name":"56 Eggs","capacity":56,"price":13000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2018/12/56-Eggs-solar-electric-incubator-1-600x449.png"},
    {"name":"64 Eggs","capacity":64,"price":14000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/64-Eggs-solar-electric-incubator-e1630976080329-600x450.jpg"},
    {"name":"112 Eggs","capacity":104,"price":19000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/104-Eggs-Incubator-1.png"},
    {"name":"128 Eggs","capacity":128,"price":20000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/128-Eggs-solar-incubator-2.png"},
    {"name":"192 Eggs","capacity":192,"price":28000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/192-egg-incubator-1-600x600.jpg"},
    {"name":"204 Eggs","capacity":204,"price":30000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2025/07/204-eggs-incubator-600x650.jpg"},
    {"name":"256 Eggs","capacity":256,"price":33000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2023/01/256-eggs-large-photo-600x676.jpeg"},
    {"name":"264 Eggs","capacity":264,"price":45000,"solar":False,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/264-Eggs-automatic-incubator-1.jpg"},
    {"name":"300 Eggs","capacity":300,"price":52000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/300-Eggs-solar-incubator.jpg"},
    {"name":"350 Eggs","capacity":350,"price":54000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/300-Eggs-solar-incubator.jpg"},
    {"name":"352 Eggs","capacity":352,"price":54000,"solar":False,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/352-Eggs-automatic-incubator-1.jpg"},
    {"name":"528 Eggs","capacity":528,"price":63000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/528-Eggs-automatic-Incubator-1-600x425.jpg"},
    {"name":"616 Eggs","capacity":616,"price":66000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2022/01/528-inc-600x800.png"},
    {"name":"880 Eggs","capacity":880,"price":75000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/880-Eggs-incubator-2.jpg"},
    {"name":"1056 Eggs","capacity":1056,"price":80000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1056-full-front-view.jpg"},
    {"name":"1232 Eggs","capacity":1232,"price":90000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1232-Eggs-automatic-incubator.jpg"},
    {"name":"1584 Eggs","capacity":1584,"price":115000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1584-Eggs-Incubator.jpg"},
    {"name":"2112 Eggs","capacity":2112,"price":120000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/2112-Eggs-Incubator.png"},
    {"name":"4928 Eggs","capacity":4928,"price":230000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/5280Incubator.jpg"},
    {"name":"5280 Eggs","capacity":5280,"price":240000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/5280-Eggs-Incubator.png"},
]

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
        f"\n\nPage {page} of {pages}. "
        "Type *next* to see more, or type a *capacity that you have in mind* (e.g., 100, 200, 528, 1000 etc)."
    )
    return "🐣 *Capacities with Prices*\n" + "\n".join(lines) + footer

def find_by_capacity(cap:int):
    items = sorted(CATALOG, key=lambda x: x["capacity"])
    for p in items:
        if p["capacity"] >= cap:
            return p
    return items[-1] if items else None

# =========================
# Session + rules
# =========================
SESS = {}  # {phone: {...}}

def delivery_eta_text(county: str) -> str:
    key = (county or "").strip().lower().split()[0]
    return "same day" if key == "nairobi" else "24 hours"

WELCOME_TEXT = (
    "🐣 Karibu *Neochicks Ltd.*\n"
    "The leading incubators supplier in Kenya and East Africa.\n"
    "Click one of the options below and I will answer you:\n\n"
    "☎️ " + CALL_LINE
)

# =========================
# Pro‑forma PDF
# =========================
def generate_invoice_pdf(order: dict) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, BUSINESS_NAME, ln=1)
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 8, f"Pro-Forma Invoice  •  {order['id']}", ln=1)
    pdf.ln(2)
    pdf.cell(0, 8, f"Date (UTC): {order['created_at_utc']}", ln=1)
    pdf.ln(4)
    # Customer details
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Customer", ln=1)
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 7, f"Name: {order['customer_name']}", ln=1)
    pdf.cell(0, 7, f"Phone: {order['customer_phone']}", ln=1)
    pdf.cell(0, 7, f"County: {order['county']}", ln=1)
    pdf.ln(2)
    # Item details
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Item", ln=1)
    pdf.set_font("Arial", "", 12)
    pdf.multi_cell(0, 7, f"{order['model']}  ({order['capacity']} eggs)")
    pdf.cell(0, 7, f"Price: {ksh(order['price'])}", ln=1)
    pdf.cell(0, 7, f"Delivery: {order['eta']}  |  {PAYMENT_NOTE}", ln=1)
    pdf.ln(6)
    pdf.set_font("Arial", "", 11)
    pdf.multi_cell(0, 6, "Support: Setup guidance + 12-month warranty.")
    pdf.ln(8)
    pdf.set_font("Arial", "I", 10)
    pdf.multi_cell(0, 5, "This is a pro-forma invoice. For assistance call " + CALL_LINE + ".")
    out = pdf.output(dest="S").encode("latin1")
    return out

# =========================
# Pro‑forma text helper
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
        "If this looks correct, reply *CONFIRM* to place the order, or type *EDIT* to change details.\n"
        "Type *CANCEL* to discard and go back to the main menu."
    )

# =========================
# Brain / Router
# =========================
def brain_reply(text: str, from_wa: str = "") -> dict:
    """Main decision engine for incoming WhatsApp messages.

    This function maintains a per‑number session dictionary to track the current
    state of the conversation. Based on the incoming message and session
    context it returns a response dict with keys: ``text`` (mandatory), and
    optionally ``buttons`` (for interactive menu), ``mediaUrl`` (image link)
    and ``caption`` for image attachments. The HTTP webhook handler then
    dispatches these to the appropriate WhatsApp API calls.
    """
    t = (text or "").strip()
    low = t.lower()
    sess = SESS.setdefault(from_wa, {"state": None, "page": 1})

    # Cancel: confirm first (NEW)
    if any(k in low for k in ["cancel","stop","abort","start over","back to menu","main menu","menu"]) and \
       sess.get("state") in {"await_name","await_phone","await_confirm","edit_menu","edit_name","edit_phone","edit_county","edit_model","cancel_confirm"}:
        if sess.get("state") == "cancel_confirm":
            # fall through to handler below
            pass
        else:
            sess["prev_state"] = sess.get("state")
            sess["state"] = "cancel_confirm"
            return {"text": "Are you sure you want to cancel this order? Reply *YES* to confirm, or *NO* to continue."}

    if sess.get("state") == "cancel_confirm":
        if low in {"yes","y","confirm","ok"}:
            SESS[from_wa] = {"state": None, "page": 1}
            return {"text": "❌ Order cancelled. You’re back at the main menu.", "buttons": MENU_BUTTONS}
        if low in {"no","n","back"}:
            sess["state"] = sess.get("prev_state") or None
            prev_state = sess.get("prev_state")
            if prev_state in {"await_confirm","edit_menu","edit_name","edit_phone","edit_county","edit_model"}:
                return {"text": "Okay — resuming your order.\n\n" + build_proforma_text(sess)}
            return {"text": "Okay — continue."}

    after_note = ("\n\n⏰ " + AFTER_HOURS_NOTE) if is_after_hours() else ""

    # MENU
    if low in {"", "hi", "hello", "start", "want", "incubator", "need an incubator"} and not sess.get("state"):
        return {"text": WELCOME_TEXT + after_note, "buttons": MENU_BUTTONS}

    # AGENT
    if ("agent" in low) or ("talk to an agent" in low):
        SESS[from_wa] = {"state": None, "page": 1}
        return {"text": "👩🏽‍💼 Connecting you to a Neochicks rep… You can also call " + CALL_LINE + "."}

    # PRICES
    if any(k in low for k in ["capacities", "capacity", "capacities with prices", "prices", "price", "bei", "gharama"]):
        sess["state"] = "prices"; sess["page"] = 1
        return {"text": price_page_text(page=1)}
    if sess.get("state") == "prices" and low in {"next","more"}:
        sess["page"] += 1; return {"text": price_page_text(page=sess["page"])}
    if sess.get("state") == "prices" and low in {"back","prev","previous"}:
        sess["page"] = max(1, sess["page"]-1); return {"text": price_page_text(page=sess["page"])}
    if sess.get("state") == "prices":
        m = re.search(r"([0-9]{2,5})", low)
        if m:
            cap = int(m.group(1)); p = find_by_capacity(cap)
            if p:
                extra = " (Solar)" if p["solar"] else ""
                gen = "\n🎁 Includes *Free Backup Generator*" if p["free_gen"] else ""
                out = {"text": "📦 *"+p['name']+"*"+extra+"\nCapacity: "+str(p['capacity'])+" eggs\nPrice: "+ksh(p['price'])+gen}
                if p.get("image"):
                    out.update({"mediaUrl": p["image"], "caption": p['name'] + " — " + ksh(p['price']) + "\n\nReply with your *county* for delivery ETA and quote. " + PAYMENT_NOTE + "." })
                sess["last_product"] = p
                return out

    # DELIVERY → COUNTY → NAME → PHONE → PRO-FORMA
    if ("delivery" in low) or ("deliver" in low) or ("delivery terms" in low):
        sess["state"] = "await_county"
        return {"text": "🚚 Delivery terms: Nairobi → same day; other counties → 24 hours. " + PAYMENT_NOTE + ".\nWhich *county* are you in?"}

    if sess.get("state") == "await_county":
        county = re.sub(r"[^a-z ]", "", low).strip()
        if not county:
            return {"text": "Please type your *county* name (e.g., Nairobi, Nakuru, Mombasa)."}
        eta = delivery_eta_text(county)
        sess["last_county"] = county.title(); sess["last_eta"] = eta
        sess["state"] = "await_name"
        return {"text": f"📍 {county.title()} → Typical delivery {eta}. {PAYMENT_NOTE}.\nGreat! Please share your *full name* for the pro-forma."}

    if sess.get("state") == "await_name":
        name = t.strip()
        if len(name) < 2:
            return {"text": "Please type your *full name* (e.g., Jane Wanjiku)."}
        sess["customer_name"] = name; sess["state"] = "await_phone"
        return {"text": "Thanks! Now your *phone number* (for delivery coordination):"}

    if sess.get("state") == "await_phone":
        phone = re.sub(r"[^0-9+ ]", "", t)
        if len(re.sub(r"\D", "", phone)) < 9:
            return {"text": "That phone seems short. Please type a valid phone (e.g., 07XX... or +2547...)."}
        sess["customer_phone"] = phone; sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    # Troubleshoot & FAQs (kept)
    if any(k in low for k in ["troubleshoot","hatch rate","problem","fault","issue"]):
        sess["state"] = None
        return {"text": ("🛠️ Quick checks:\n1) Temp 37.8°C (±0.2)\n2) Humidity 55–60% set / 65% hatch\n3) Turning 3–5×/day (auto OK)\n4) Candle day 7 & 14; remove clears\n5) Ventilation okay (no drafts)\n6) Disinfection after hatching?\n\nDo you check all above? Call " + CALL_LINE + ".")}

    if re.search(r"warranty|guarantee", low):
        return {"text":"✅ 12-month warranty + free setup guidance. We also connect you to our technician from your nearest town."}
    if re.search(r"backup|inverter|power|solar", low):
        return {"text":"🔋 Solar panels + battery available (sized per model). We assist to outsource solar packages depending on your incubator power rating."}
    if re.search(r"sell.*chicks|\bchicks\b|kienyeji", low):
        return {"text":"🐥 Improved Kienyeji chicks available — 3 days old up to 2 months old. Call: 0793585968."}
    if re.search(r"payment|mpesa|cash", low):
        return {"text":"💳 Any mode of payment acceptable. " + PAYMENT_NOTE + "."}
    if re.search(r"include.*solar|price.*include.*solar|solar.*include", low):
        return {"text":"ℹ️ Prices do not include solar panels. We guide you to get the best solar/battery package for your incubator."}

    # YES → quote flow
    if low in {"yes","yeah","yep","ok","okay","sure","invoice","profoma","pro-forma","quote","quotation","recommendation"} and sess.get("state") in {"await_quote", None}:
        product = sess.get("last_product"); county = sess.get("last_county")
        if not product:
            sess["state"]="prices"
            return {"text":"Great! Tell me the capacity you want (e.g., 204 or 528) so I can prepare your quote.\n\n"+price_page_text(page=1)}
        if not county:
            sess["state"]="await_county"
            return {"text":"Which *county* are you in? (e.g., Nairobi, Nakuru, Mombasa)"}
        sess["state"]="await_name"; return {"text":"Perfect. Please share your *full name* for the pro-forma."}

    # EDIT flow
    if sess.get("state") == "await_confirm" and "edit" in low:
        sess["state"]="edit_menu"
        return {"text": ("What would you like to change?\n1) Name\n2) Phone\n3) County\n4) Model (capacity)\n\nReply with *1, 2, 3,* or *4*.\nOr type *CANCEL* to discard and go back to the main menu.")}

    if sess.get("state") == "edit_menu":
        choice = re.sub(r"[^0-9a-z ]","",low).strip()
        if choice in {"1","name"}:
            sess["state"]="edit_name";   return {"text":"Okay — please type the *correct full name*:"}
        if choice in {"2","phone"}:
            sess["state"]="edit_phone";  return {"text":"Okay — please type the *correct phone number* (07XX... or +2547...):"}
        if choice in {"3","county"}:
            sess["state"]="edit_county"; return {"text":"Okay — please type your *county* (e.g., Nairobi, Nakuru, Mombasa):"}
        if choice in {"4","model","capacity"}:
            sess["state"]="edit_model"; return {"text":"Type the *capacity number* you want (e.g., 204, 528, 1056):"}
        return {"text":"Please reply with *1, 2, 3,* or *4*."}

    if sess.get("state") == "edit_name":
        name = (t or "").strip()
        if len(name) < 2:
            return {"text":"That looks too short. Please type your *full name* (e.g., Jane Wanjiku)."}
        sess["customer_name"] = name; sess["state"]="await_confirm"; return {"text":build_proforma_text(sess)}

    if sess.get("state") == "edit_phone":
        phone = re.sub(r"[^0-9+ ]","",(t or ""))
        if len(re.sub(r"\D","",phone)) < 9:
            return {"text":"That phone seems short. Please type a valid phone (e.g., 07XX... or +2547...)."}
        sess["customer_phone"] = phone; sess["state"]="await_confirm"; return {"text":build_proforma_text(sess)}

    if sess.get("state") == "edit_county":
        county_raw=(t or "").strip(); county=re.sub(r"[^a-z ]","",county_raw.lower()).strip()
        if not county:
            return {"text":"Please type your *county* name (e.g., Nairobi, Nakuru, Mombasa)."}
        sess["last_county"] = county.title(); sess["last_eta"] = delivery_eta_text(county)
        sess["state"]="await_confirm"; return {"text":build_proforma_text(sess)}

    if sess.get("state") == "edit_model":
        m = re.search(r"([0-9]{2,5})", low)
        if not m:
            return {"text":"Please type just the *capacity number* (e.g., 204, 528, 1056)."}
        cap=int(m.group(1)); p=find_by_capacity(cap)
        if not p:
            return {"text":"I couldn't find that capacity. Try 204, 264, 528, 1056, 5280 etc."}
        sess["last_product"] = p; sess["state"]="await_confirm"; return {"text":build_proforma_text(sess)}

    # CONFIRM → email + DB insert + PDF link
    # Allow case-insensitive confirm with optional whitespace
    if sess.get("state") == "await_confirm" and re.fullmatch(r"(?i)\s*confirm\s*", t):
        p = sess.get("last_product") or {}
        county = sess.get("last_county","-")
        eta = sess.get("last_eta", delivery_eta_text(county))
        order_id = new_order_id()
        created_at = datetime.utcnow()
        follow_due = created_at + timedelta(minutes=FOLLOWUP_DELAY_MIN)

        # DB log
        insert_order({
            "id": order_id,
            "wa_from": from_wa,
            "customer_name": sess.get("customer_name",""),
            "customer_phone": sess.get("customer_phone",""),
            "county": county,
            "model": p.get("name",""),
            "capacity": int(p.get("capacity") or 0),
            "price": int(p.get("price") or 0),
            "eta": eta,
            "created_at_utc": created_at.isoformat()+"Z",
            "followup_due_utc": follow_due.isoformat()+"Z",
        })

        # Email notify
        subject = f"ORDER CONFIRMED — {p.get('name','Model')} for {sess.get('customer_name','Customer')} ({order_id})"
        body = (
            f"New order confirmation from WhatsApp bot\n\n"
            f"Order ID: {order_id}\n"
            f"Customer Name: {sess.get('customer_name','')}\n"
            f"Customer Phone: {sess.get('customer_phone','')}\n"
            f"County: {county}\n"
            f"Model: {p.get('name','')}\n"
            f"Capacity: {p.get('capacity','')}\n"
            f"Price: {ksh(p.get('price',0))}\n"
            f"Delivery ETA: {eta}\n"
            f"Payment: {PAYMENT_NOTE}\n"
            f"Timestamp: {created_at.isoformat()}Z\n"
        )
        send_email(subject, body)

        # Send back invoice link as a WhatsApp document (PDF served by our app)
        pdf_url = request.url_root.rstrip("/") + f"/invoice/{order_id}.pdf"
        try:
            # attempt to send the PDF document via WhatsApp
            send_document(from_wa, pdf_url, f"{order_id}.pdf", "Your pro-forma invoice")
        except Exception as e:
            # log and fall back to plain text link
            print("WhatsApp document send failed:", e)
            try:
                send_text(from_wa, "Here is your pro-forma invoice: " + pdf_url)
            except Exception as e2:
                print("Fallback text send failed:", e2)

        # reset session
        SESS[from_wa] = {"state": None, "page": 1}
        return {"text": "✅ Order confirmed! I’ve sent your pro-forma invoice. Our team will contact you shortly to finalize delivery. Thank you for choosing Neochicks."}

    # County guess (stateless)
    c_guess = guess_county(low)
    if c_guess:
        eta = delivery_eta_text(c_guess)
        sess["last_county"] = c_guess.title(); sess["last_eta"] = eta; sess["state"]="await_name"
        return {"text": f"📍 {c_guess.title()} → Typical delivery {eta}. {PAYMENT_NOTE}.\nGreat! Please share your *full name* for the pro-forma."}

    # Fallback
    return {"text":"Got it! Tap *Prices/Capacities*, *Delivery Terms*, *Incubator issues*, or *Talk to an Agent*.", "buttons": MENU_BUTTONS}

# =========================
# Routes
# =========================
@app.get("/health")
def health():
    return jsonify({"status":"ok"})

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
        if reply.get("text"):
            send_text(from_wa, reply["text"])
        if reply.get("buttons"):
            send_buttons(from_wa, reply["buttons"])
        if reply.get("mediaUrl"):
            send_image(from_wa, reply["mediaUrl"], reply.get("caption",""))
        return "ok", 200
    except Exception as e:
        print("Webhook error:", e, "payload:", json.dumps(data)[:1000])
        return "error", 200

# --- Admin: list recent orders (JSON) ---
@app.get("/orders")
def orders():
    if request.args.get("token") != ADMIN_TOKEN:
        return abort(403)
    return jsonify(get_recent_orders())

# --- Cron: send thank-you followups ---
@app.post("/send_followups")
def send_followups():
    if request.args.get("token") != ADMIN_TOKEN:
        return abort(403)
    now_iso = datetime.utcnow().isoformat() + "Z"
    due = due_followups(now_iso)
    sent = 0
    for o in due:
        try:
            msg = (
                "🙏 Asante for choosing Neochicks! "
                f"Your order *{o['id']}* ({o['model']} {o['capacity']} eggs) is noted.\n"
                "If you need any assistance before delivery, reply here or call " + CALL_LINE + "."
            )
            send_text(o["wa_from"], msg)
            mark_followup_sent(o["id"])
            sent += 1
        except Exception as e:
            print("Followup send failed for", o["id"], e)
    return jsonify({"processed": len(due), "sent": sent})

# --- Serve the PDF invoice ---
@app.get("/invoice/<order_id>.pdf")
def invoice(order_id):
    order = get_order(order_id)
    if not order:
        return abort(404)
    pdf_bytes = generate_invoice_pdf(order)
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=False, download_name=f"{order_id}.pdf")

@app.get("/testmail")
def testmail():
    ok = send_email("Neochicks Test Email", "It works! ✅")
    return ("OK" if ok else "FAIL"), 200

if __name__ == "__main__":
  @app.before_first_request
def _startup():
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 3000)))



