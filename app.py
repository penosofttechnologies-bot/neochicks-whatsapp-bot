"""
Neochicks WhatsApp Bot (DB-free, robust PDF delivery)

- No DB: stores order details in memory and writes PDFs to /tmp. 
- Full catalog preserved.
- EDIT flow (name/phone/county/model) included.
- Sends invoice PDFs to WhatsApp by uploading media first (media_id), with link/text fallbacks.
- Uses RENDER_EXTERNAL_URL when available to build absolute links.

Render tips:
- Set WEB_CONCURRENCY=1
- Scale → Instance Count = 1 (no autoscaling)
"""

import os
import io
import re
import json
import logging
import csv, gzip, base64
from datetime import datetime, timedelta
from collections import Counter, defaultdict, deque

import requests
from flask import Flask, request, jsonify, send_file, abort, render_template_string
from fpdf import FPDF  # pip install fpdf==1.7.2

# -------------------------
# App + logging
# -------------------------
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO)

# -------------------------
# Config (env vars)
# -------------------------
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "changeme")
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GRAPH_BASE      = "https://graph.facebook.com/v20.0"

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
SENDGRID_FROM    = os.getenv("SENDGRID_FROM", "")
SALES_EMAIL      = os.getenv("SALES_EMAIL", SENDGRID_FROM)

BUSINESS_NAME = "Neochicks Poultry Ltd."
CALL_LINE     = "0707787884"
PAYMENT_NOTE  = "Pay on delivery"
AFTER_HOURS_NOTE = "We are currently off till early morning."

INVOICE_TTL_MIN = int(os.getenv("INVOICE_TTL_MIN", "1440"))  # minutes
EXTERNAL_BASE   = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
LOGO_URL = os.getenv("LOGO_URL", "")           # optional
SIGNATURE_URL = os.getenv("SIGNATURE_URL", "") # optional

# ---- Logging & storage paths (persistent on Render Disk if mounted at /data) ----
_DATA = "/data" if os.path.isdir("/data") else "/tmp"
AUDIT_PATH = os.path.join(_DATA, "wa_audit.jsonl.gz")   # masked analytics log (no PDFs/images)
LEADS_CSV  = os.path.join(_DATA, "wa_leads.csv")        # raw phone leads for follow-ups

# -------------------------
# In-memory store (temporary)
# -------------------------
INVOICES = {}  # { order_id: order_dict }

def _cleanup_invoices(now: datetime | None = None):
    now = now or datetime.utcnow()
    drop = []
    for oid, o in INVOICES.items():
        try:
            created = datetime.fromisoformat(o.get("created_at_utc", "").replace("Z", ""))
        except Exception:
            created = now
        if (now - created) > timedelta(minutes=INVOICE_TTL_MIN):
            drop.append(oid)
    for oid in drop:
        INVOICES.pop(oid, None)

# -------------------------
# Utilities, catalog, helpers
# -------------------------
COUNTIES = {
    "baringo","bomet","bungoma","busia","elgeyo marakwet","embu","garissa","homa bay","isiolo",
    "kajiado","kakamega","kericho","kiambu","kilifi","kirinyaga","kisii","kisumu","kitui",
    "kwale","laikipia","lamu","machakos","makueni","mandera","marsabit","meru","migori","mombasa",
    "murang'a","muranga","nairobi","nakuru","nandi","narok","nyamira","nyandarua","nyeri",
    "samburu","siaya","taita taveta","tana river","tharaka nithi","trans nzoia","turkana",
    "uasin gishu","vihiga","wajir","west pokot"
}

def guess_county(text: str):
    cleaned = re.sub(r"[^a-z ]", "", (text or "").lower()).strip()
    if not cleaned:
        return None
    if cleaned in COUNTIES:
        return cleaned
    if cleaned.endswith(" county"):
        c = cleaned[:-7].strip()
        if c in COUNTIES:
            return c
    parts = cleaned.split()
    if len(parts) in (2, 3):
        joined = " ".join(parts)
        if joined in COUNTIES:
            return joined
    return None

def ksh(n: int) -> str:
    try:
        return f"KSh{int(n):,}"
    except Exception:
        return f"KSh{n}"

def is_after_hours():
    eat_hour = (datetime.utcnow().hour + 3) % 24
    return not (6 <= eat_hour < 23)

def delivery_eta_text(county: str) -> str:
    key = (county or "").strip().lower().split()[0] if county else ""
    return "same day" if key == "nairobi" else "24 hours"
MENU_BUTTONS = [
    "Incubator Prices 💰📦",
    "Delivery Terms 🚚",
    "Talk to an Agent 👩🏽‍💼",
    "Incubator issues 🛠️"
]
def main_menu_text(after_note: str = "") -> str:
    """
    Bold + emoji classic numbered menu for first interaction and 'back to menu'.
    """
    return (
        "🐣 Karibu *Neochicks Ltd.*\n"
        "The leading dealer in Poultry Farming Services.\n"
        "Please choose what you are interested in:\n\n"
        "1️⃣ *Incubators* 🌡️\n" 
        "2️⃣ *Chicks* 🐥\n"
        "3️⃣ *Fertile Eggs* 🥚\n"
        "4️⃣ *Cages & Equipment* 🪺\n\n"
        "Reply with one of the *numbers above* and I will guide you🙏.\n"
        f"☎️ {CALL_LINE}" + after_note
    )
def incubator_text() -> str:
    return (
        "🔥 *MODERN AUTOMATIC EGGS INCUBATORS*\n\n"
        "We supply high-quality, highly efficient digital automatic incubators with:\n"
        "✔ Automatic turning, temperature control and humidity control\n"
        "✔ High hatch rates\n"
        "✔ 1-year warranty\n"
        "✔ *FREE* Fertile Eggs\n"
        "✔ *FREE* Backup Generators\n"
        "✔ *FREE* delivery countrywide\n\n"
        "To view the full price list with Photos, send the word: *PRICES*\n\n"
        f"To speak to us directly, call {CALL_LINE}.\n"
        "Website: https://neochickspoultry.com/eggs-incubators/"
    )
def fertile_eggs_text() -> str:
    return (
        "We supply quality *fertile eggs for incubation* 🥚\n\n"
        "*Improved Kienyeji Fertile Eggs*\n"
        "• (Sasso, Kari, Kenbro, Kuroiler and Rainbow Rooster)\n"
        "• (1 tray (30 eggs) → *Ksh900*)\n\n"
        "If you like, I can share *photos of our Different breeds of mature chickens*.\n"
        "Simply type: *PHOTOS*\n\n"
        f"For more information on delivery, availability, pictures etc,\n"
        f"please call us on: {CALL_LINE}\n"
        "You can also visit our website:\n"
        "https://neochickspoultry.com/kienyeji-farming/"
    )
def chicks_info_text() -> str:
    return (
        "We deal with quality chicks at different ages.\n"
        "*Improved Kienyeji chicks*\n"
        "(Sasso, Kari, Kenbro and Kuroiler breeds)\n"
        "3 days → *Ksh100*\n"
        "1 week → *Ksh130*\n"
        "2 weeks → *Ksh160*\n"
        "3 weeks → *Ksh200*\n"
        "4 weeks → *Ksh230*\n\n"
        "*LAYERS CHICKS*\n"
        "1 DAY OLD → *Ksh160*\n"
        "1 Week OLD → *Ksh190*\n"
        "2 Weeks OLD → *Ksh230*\n"
        "3 Weeks OLD → *Ksh260*\n"
        "1 Month OLD → *Ksh300*\n\n"
        "2 MONTHS OLD → *Ksh450*\n"
        "3 MONTHS OLD → *Ksh550*\n"
        "4 MONTHS OLD → *Ksh750*\n"
        "5 MONTHS OLD → *Ksh850*\n\n"
        "If you like, I can share the *photos of different ages of layers chicks*.\n\n"
        "Simply type: *PHOTOS*\n\n"
        "For more information on delivery, availability, more pictures etc,\n"
        f"please call us on: {CALL_LINE}\n"
        "You can also visit our website:\n"
        "https://neochickspoultry.com/kienyeji-farming/"
    )
def cages_text() -> str:
    return (
        "We have high quality, modern galvanized layers cages fitted with automated nipple drinking system and feeding troughs.\n\n" 
        "The 128 birds cages goes at ksh38,000\n"
        "The 256 birds cages goes at ksh76,000\n"
        "The 384 birds cages goes at ksh114,000\n"
        "The 512 birds cages goes at ksh152,000\n"
        "The 640 birds cages goes at ksh190,000\n"
        "And so on...in multiples of 128 Birds\n\n"
        "If you like, I can share the *photos of different ages of chicks*.\n\n"
        "Simply type: *PHOTOS*\n\n"
        "For more information, *Call 0707 787884.*"
    )

CATALOG = [
    {"name":"56 Eggs","capacity":56,"price":13000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2018/12/56-Eggs-solar-electric-incubator-1-600x449.png"},
    {"name":"64 Eggs","capacity":64,"price":14000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/64-Eggs-solar-electric-incubator-e1630976080329-600x450.jpg"},
    {"name":"112 Eggs","capacity":104,"price":19000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/104-Eggs-Incubator-1.png"},
    {"name":"128 Eggs","capacity":128,"price":20000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/128-Eggs-solar-incubator-2.png"},
    {"name":"192 Eggs","capacity":192,"price":28000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/192-egg-incubator-1-600x600.jpg"},
    {"name":"204 Eggs","capacity":204,"price":30000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2025/07/204-eggs-incubator-600x650.jpg"},
    {"name":"256 Eggs","capacity":256,"price":33000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2023/01/256-eggs-large-photo-600x676.jpeg"},
    {"name":"264 Eggs","capacity":264,"price":45000,"solar":False,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/264-Eggs--incubator-1.jpg"},
    {"name":"300 Eggs","capacity":300,"price":52000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/300-Eggs-solar-incubator.jpg"},
    {"name":"350 Eggs","capacity":350,"price":54000,"solar":True,"free_gen":False,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/300-Eggs-solar-incubator.jpg"},
    {"name":"528 Eggs","capacity":528,"price":63000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/528-Eggs--Incubator-1-600x425.jpg"},
    {"name":"616 Eggs","capacity":616,"price":66000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2022/01/528-inc-600x800.png"},
    {"name":"1056 Eggs","capacity":1056,"price":80000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1056-full-front-view.jpg"},
    {"name":"1232 Eggs","capacity":1232,"price":90000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1232-Eggs--incubator.jpg"},
    {"name":"1584 Eggs","capacity":1584,"price":115000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/1584-Eggs-Incubator.jpg"},
    {"name":"2112 Eggs","capacity":2112,"price":120000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/2112-Eggs-Incubator.png"},
    {"name":"3520 Eggs","capacity":3520,"price":180000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/5280Incubator.jpg"},
    {"name":"5280Eggs","capacity":5280,"price":240000,"solar":False,"free_gen":True,"image":"https://neochickspoultry.com/wp-content/uploads/2021/09/5280-Eggs-Incubator.png"},
]

def product_line(p: dict) -> str:
    tag = "(Solar/Electric)" if p.get("solar") else ""
    gen = " + *Generator*" if p.get("free_gen") else ""
    return f"- {p['name']}{tag}→{ksh(p['price'])}{gen}"

def price_page_text(page: int = 1, per_page: int = 20) -> str:
    items = sorted(CATALOG, key=lambda x: x["capacity"])
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    chunk = items[start : start + per_page]
    lines = [product_line(p) for p in chunk]

    footer = (
               "\n-------------------\nPlease type the *capacity that you want* (e.g. 64, 528 etc) and I will give you its details 🙏"
   )
    return "🐣 *Capacities with Prices*\n" + "\n".join(lines) + footer

def find_by_capacity(cap: int):
    items = sorted(CATALOG, key=lambda x: x["capacity"])
    for p in items:
        if p["capacity"] >= cap:
            return p
    return items[-1] if items else None

# -------------------------
# PDF generation
# -------------------------
def _fetch_to_tmp(url: str, basename: str) -> str | None:
    """Download a small image to /tmp and return its path (or None on failure)."""
    if not url:
        return None
    try:
        ext = ".png"
        if "." in url.split("/")[-1]:
            ext = "." + url.split("/")[-1].split(".")[-1].lower()
            if len(ext) > 5:  # overly long or querystringy -> default to png
                ext = ".png"
        path = f"/tmp/{basename}{ext}"
        if not os.path.exists(path):
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
        return path
    except Exception:
        app.logger.exception("Failed to fetch image: %s", url)
        return None

def _latin1(s: str) -> str:
    return (s or "").encode("latin-1", "replace").decode("latin-1")

def _draw_item_row(pdf, desc, qty, unit_price, amount,
    desc_w, qty_w, unit_w, amt_w, line_h=8):
    """
    Draws a table row where the first cell (Description) can wrap.
    Ensures the entire row height matches the tallest wrapped cell
    and moves the cursor to the start of the next row cleanly.
    """
    # Starting positions
    x0 = pdf.get_x()
    y0 = pdf.get_y()

    # 1) Description (can wrap). This advances Y downward.
    pdf.multi_cell(desc_w, line_h, _latin1(desc), border=1, align="L")

    # Height used by description
    y_after_desc = pdf.get_y()
    row_bottom = max(y0 + line_h, y_after_desc)  # minimum one line high

    # 2) Other cells must start back at the top of the row (y0)
    pdf.set_xy(x0 + desc_w, y0)
    pdf.cell(qty_w,  row_bottom - y0, _latin1(str(qty)),   border=1, align="C")
    pdf.cell(unit_w, row_bottom - y0, _latin1(unit_price), border=1, align="R")
    pdf.cell(amt_w,  row_bottom - y0, _latin1(amount),     border=1, align="R")

    # 3) Move to the beginning of the next line (left margin), at row_bottom
    pdf.set_xy(pdf.l_margin, row_bottom)
    pdf.ln(6)  # extra spacing after the row

def _eat_from_utc_iso(iso_str: str) -> str:
    """
    Convert an ISO UTC string like '2025-10-29T07:32:39Z' to EAT (UTC+3)
    and format as 'YYYY-MM-DD HH:MM'.
    """
    if not iso_str:
        return ""
    s = iso_str.strip().replace("Z", "")
    try:
        dt_utc = datetime.fromisoformat(s)
    except Exception:
        return iso_str  # fallback, show original if parsing fails
    dt_eat = dt_utc + timedelta(hours=3)
    return dt_eat.strftime("%Y-%m-%d")

def generate_invoice_pdf(order: dict) -> bytes:
    """
    Neochicks formal invoice (1-page tuned)
    Depends on: _latin1, _fetch_to_tmp, _eat_from_utc_iso, ksh, BUSINESS_NAME,
                PAYMENT_NOTE, CALL_LINE, LOGO_URL, SIGNATURE_URL
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    # --- Header band ---
    pdf.set_fill_color(240, 248, 240)  # very light green band
    pdf.rect(0, 0, 210, 25, "F")

    # Logo (optional)
    logo_path = _fetch_to_tmp(LOGO_URL, "neochicks_logo") if LOGO_URL else None
    if logo_path:
        try:
            pdf.image(logo_path, x=15, y=6, w=28)
        except Exception:
            pass

    # Business name + invoice meta
    left_after_logo = 15 + (30 if logo_path else 0) + 2
    pdf.set_xy(left_after_logo, 7)
    pdf.set_font("Arial", "B", 15)
    pdf.cell(0, 7, _latin1(BUSINESS_NAME), ln=1)

    pdf.set_x(left_after_logo)
    pdf.set_font("Arial", "", 11)
    pdf.cell(0, 6, _latin1("Pro-Forma Invoice"), ln=1)

    # Meta lines
    pdf.ln(2)
    eat_display = _eat_from_utc_iso(order.get('created_at_utc', ''))
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, _latin1(f"Invoice No: {order.get('id','')}"), ln=1)
    pdf.cell(0, 6, _latin1(f"Date (EAT, UTC+3): {eat_display}"), ln=1)
    pdf.ln(3)

    # Divider
    pdf.set_draw_color(200, 200, 200)
    x1, y = 15, pdf.get_y()
    pdf.line(x1, y, 195, y)
    pdf.ln(5)

    # Bill To
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, _latin1("Bill To"), ln=1)
    pdf.set_font("Arial", "", 11)
    pdf.cell(0, 6, _latin1(f"Name:  {order.get('customer_name','')}"), ln=1)
    pdf.cell(0, 6, _latin1(f"Phone: {order.get('customer_phone','')}"), ln=1)
    pdf.cell(0, 6, _latin1(f"County: {order.get('county','')}"), ln=1)
    pdf.ln(6)

    # --- Items table (fits exactly into content width) ---
    content_w = pdf.w - pdf.l_margin - pdf.r_margin  # ~180 on A4 with 15mm margins
    desc_w, qty_w, unit_w, amt_w = 95, 25, 30, 30    # sum = 180

    # Header row
    pdf.set_font("Arial", "B", 11)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(desc_w, 8, _latin1("Description"), border=1, ln=0, align="L", fill=True)
    pdf.cell(qty_w,  8, _latin1("Qty"),         border=1, ln=0, align="C", fill=True)
    pdf.cell(unit_w, 8, _latin1("Unit Price"),  border=1, ln=0, align="R", fill=True)
    pdf.cell(amt_w,  8, _latin1("Amount"),      border=1, ln=1, align="R", fill=True)

    # Single item
    model  = order.get("model", "")
    cap    = int(order.get("capacity", 0) or 0)
    price  = int(order.get("price", 0) or 0)
    qty    = 1
    amount = price * qty

    # Look up flags from your CATALOG using capacity (fallback gracefully)
    catalog_item = next((p for p in CATALOG if int(p.get("capacity", -1)) == cap), None)
    is_solar = bool(catalog_item and catalog_item.get("solar"))
    has_free_gen = bool(catalog_item and catalog_item.get("free_gen"))

    # Build the full model label exactly as you wanted
    if is_solar:
        model_full = f"{cap} Eggs  Incubator (Solar / Electric)"
    elif has_free_gen:
        model_full = f"{cap} Eggs Automatic Incubator (Free Backup Generator)"
    else:
        # Neutral fallback for models that are neither flagged solar nor free_gen
        model_full = f"{cap} Eggs Automatic Incubator"

    # ASCII-safe hyphen to avoid '?' with core fonts; keep your final PAYMENT_NOTE
    desc = (
        f"{model_full} | FREE Delivery \n"
        f"- Delivery: {order.get('eta','24 hours')} | {PAYMENT_NOTE}"
    )

    # Draw row with wrapped description and aligned numeric cells
    pdf.set_font("Arial", "", 11)
    x0 = pdf.get_x()
    y0 = pdf.get_y()
    line_h = 8

    pdf.multi_cell(desc_w, line_h, _latin1(desc), border=1, align="L")
    y_after = pdf.get_y()
    row_bottom = max(y0 + line_h, y_after)

    pdf.set_xy(x0 + desc_w, y0)
    pdf.cell(qty_w,  row_bottom - y0, _latin1(str(qty)),   border=1, align="C")
    pdf.cell(unit_w, row_bottom - y0, _latin1(ksh(price)), border=1, align="R")
    pdf.cell(amt_w,  row_bottom - y0, _latin1(ksh(amount)),border=1, align="R")
    pdf.set_xy(pdf.l_margin, row_bottom)
    pdf.ln(6)

    # Totals rows (each on its own line)
    def totals_row(label: str, value: str, bold=False):
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Arial", "B" if bold else "", 11)
        pdf.cell(desc_w + qty_w + unit_w, 8, _latin1(label), border=0, ln=0, align="R")
        pdf.cell(amt_w, 8, _latin1(value), border=1, ln=1, align="R")

    totals_row("Subtotal", ksh(amount), bold=False)
    totals_row("Total",    ksh(amount), bold=True)
    pdf.ln(6)

    # --- Notes (tight but readable) ---
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 7, _latin1("Notes"), ln=1)
    pdf.set_font("Arial", "", 10)
    pdf.multi_cell(0, 6, _latin1(
        "1) Prices exclude optional solar packages.\n"
        "2) Pay on delivery. Please keep your phone on for delivery coordination.\n"
        "3) Includes setup guidance and 12-month warranty.\n"
        f"4) For assistance call {CALL_LINE}."
    ))
    pdf.ln(4)

    # --- Signature / Stamp block with dynamic height clamp so footer stays on page 1 ---
    FOOTER_H = 16                # reserved footer height
    GAP_BEFORE_FOOTER = 4        # spacing above footer
    min_sig_h = 10               # minimum signature block height
    max_sig_h = 22               # maximum signature block height

    page_h = pdf.h
    footer_top = page_h - pdf.b_margin - FOOTER_H
    cur_y = pdf.get_y()

    # If we're too low, nudge up a bit so we can still fit the footer
    if cur_y > footer_top - min_sig_h:
        pdf.set_y(max(pdf.t_margin, footer_top - min_sig_h))

    cur_y = pdf.get_y()
    remaining = footer_top - GAP_BEFORE_FOOTER - cur_y
    sig_h = max(min_sig_h, min(max_sig_h, remaining))

    # Title for signature
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 7, _latin1("Authorized Signature / Stamp"), ln=1)
    pdf.set_font("Arial", "", 10)

    block_top_y = pdf.get_y()
    sig_path = _fetch_to_tmp(SIGNATURE_URL, "neochicks_signature") if SIGNATURE_URL else None
    if sig_path and sig_h > (min_sig_h + 2):
        try:
            y_sig = block_top_y + 2
            img_h = max(8, sig_h - 6)
            pdf.image(sig_path, x=pdf.get_x(), y=y_sig, h=img_h)
        except Exception:
            pass

    # Move to end of signature block and draw the signature line
    pdf.set_y(block_top_y + sig_h)
    pdf.set_draw_color(160, 160, 160)
    x_line = pdf.get_x()
    pdf.line(x_line, pdf.get_y(), x_line + 60, pdf.get_y())
    pdf.ln(4)

    # --- Footer pinned to bottom of this page ---
    pdf.set_y(footer_top)
    pdf.set_font("Arial", "I", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 6, _latin1("Thank you for choosing Neochicks Poultry Ltd."), ln=1, align="C")
    pdf.set_text_color(0, 0, 0)

    # Return bytes
    return pdf.output(dest="S").encode("latin1")

# -------------------------
# Email (SendGrid)
# -------------------------
def send_email(subject: str, body: str) -> bool:
    if not (SENDGRID_API_KEY and SENDGRID_FROM and SALES_EMAIL):
        app.logger.info("Email not sent—missing SENDGRID_API_KEY/SENDGRID_FROM/SALES_EMAIL")
        return False
    try:
        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": SALES_EMAIL}]}],
                "from": {"email": SENDGRID_FROM, "name": "Neochicks Bot"},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            },
            timeout=20,
        )
        return r.status_code in (200, 202)
    except Exception:
        app.logger.exception("SendGrid exception")
        return False

def send_email_with_attachments(subject: str, body: str, attachments: list[tuple[str, str]]) -> bool:
    """
    SendGrid email with multiple attachments.
    attachments: list of tuples (filename, filepath)
    """
    if not (SENDGRID_API_KEY and SENDGRID_FROM and SALES_EMAIL):
        app.logger.info("Email not sent—missing SENDGRID_API_KEY/SENDGRID_FROM/SALES_EMAIL")
        return False
    try:
        atts = []
        for name, path in attachments or []:
            if not (name and path and os.path.exists(path)):
                continue
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            mime = "application/gzip" if name.endswith(".gz") else "text/csv"
            atts.append({
                "content": b64,
                "type": mime,
                "filename": name,
                "disposition": "attachment",
            })

        payload = {
            "personalizations": [{"to": [{"email": SALES_EMAIL}]}],
            "from": {"email": SENDGRID_FROM, "name": "Neochicks Bot"},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        }
        if atts:
            payload["attachments"] = atts

        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        return r.status_code in (200, 202)
    except Exception:
        app.logger.exception("SendGrid attachments exception")
        return False

# -------------------------
# WhatsApp helpers
# -------------------------
def _wa_headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

def send_text(to: str, body: str):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_document(to: str, link: str, filename: str, caption: str = ""):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {"link": link, "filename": filename, "caption": caption},
    }
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_buttons(to: str, titles, prompt_text="Pick one:"):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    buttons = [{"type": "reply", "reply": {"id": f"b{i+1}", "title": t[:20]}} for i, t in enumerate(titles[:3])]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {"type": "button", "body": {"text": prompt_text}, "action": {"buttons": buttons}},
    }
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_image(to: str, link: str, caption: str = ""):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"link": link, "caption": caption}}
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# WhatsApp: Media Upload Helpers
def upload_media_pdf(pdf_bytes: bytes, filename: str = "invoice.pdf") -> str | None:
    """
    Upload a PDF to WhatsApp and return media_id, or None on failure.
    """
    try:
        url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/media"
        files = {"file": (filename, pdf_bytes, "application/pdf")}
        data = {"messaging_product": "whatsapp"}
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        r = requests.post(url, headers=headers, data=data, files=files, timeout=60)
        r.raise_for_status()
        return r.json().get("id")
    except Exception:
        app.logger.exception("Media upload failed")
        return None

def send_document_by_id(to: str, media_id: str, filename: str, caption: str = ""):
    """
    Send an already-uploaded document (by media_id) to a WhatsApp user.
    """
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {"id": media_id, "filename": filename, "caption": caption},
    }
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# -------------------------
# Session store
# -------------------------
SESS = {}  # mapping phone -> session dict

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
        "🧾 *Your Order Details:*\n\n"
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

def new_order_id():
    ts = datetime.utcnow().strftime("%y%m%d%H%M%S")
    return f"NEO-{ts}"

# -------------------------
# Logging helpers (audit + leads)
# -------------------------
def _audit_write(event: dict):
    """
    Append one masked JSON record per line to a gzipped file.
    Keeps phones masked to avoid PII in analytics. Small text only (no PDFs/images).
    """
    try:
        ev = dict(event or {})
        ev["ts_utc"] = datetime.utcnow().isoformat() + "Z"

        def _mask(v: str):
            if not v: return v
            d = re.sub(r"\D", "", v)
            return "***" + d[-3:] if len(d) >= 3 else "***"

        for k in ("from","to","customer_phone","wa_from"):
            if k in ev and isinstance(ev[k], str):
                ev[k] = _mask(ev[k])

        line = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
        with gzip.open(AUDIT_PATH, "ab") as fh:
            fh.write(line)
    except Exception:
        app.logger.exception("audit write failed")

def _leads_add(wa_from: str, name: str, phone: str, county: str, intent: str, last_text: str):
    """
    Append raw leads with real phone numbers for follow-ups.
    CSV is easy to open in Excel or import to a CRM.
    """
    try:
        is_new = not os.path.exists(LEADS_CSV)
        with open(LEADS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["ts_utc","wa_from","customer_name","customer_phone","county","intent","last_text"])
            w.writerow([
                datetime.utcnow().isoformat() + "Z",
                wa_from or "",
                (name or "").strip(),
                (phone or "").strip(),
                (county or "").strip(),
                intent,
                (last_text or "")[:200],
            ])
    except Exception:
        app.logger.exception("leads write failed")

# -------------------------
# Brain / router
# -------------------------
def brain_reply(text: str, from_wa: str = "") -> dict:
    t = (text or "").strip()
    low = t.lower()
    words = re.findall(r"[a-z]+", low)
    sess = SESS.setdefault(from_wa, {"state": None, "page": 1})
    print("DEBUG STATE BEFORE:", sess)

    digits = re.sub(r"[^0-9]", "", low)


    # -------------------------
    # CANCEL flow
    # -------------------------
    if any(k in low for k in ["cancel", "stop", "abort", "start over", "back to menu", "main menu", "menu"]) and \
       sess.get("state") in {
           "await_name", "await_phone", "await_confirm",
           "edit_menu", "edit_name", "edit_phone", "edit_county", "edit_model",
           "cancel_confirm", "await_county"
       }:
        if sess.get("state") != "cancel_confirm":
            sess["prev_state"] = sess.get("state")
            sess["state"] = "cancel_confirm"
            return {"text": "Are you sure you want to cancel this order? Reply *YES* to confirm, or *NO* to continue."}

    if sess.get("state") == "cancel_confirm":
        if low in {"yes", "y", "confirm", "ok"}:
            # Reset session and go back to main menu
            SESS[from_wa] = {"state": None, "page": 1}
            return {"text": "❌ Order cancelled. You’re back at the main menu.\n\n" + main_menu_text()}
        if low in {"no", "n", "back"}:
            sess["state"] = sess.get("prev_state") or None
            prev_state = sess.get("prev_state")
            if prev_state in {"await_confirm", "edit_menu", "edit_name", "edit_phone", "edit_county", "edit_model"}:
                return {"text": "Okay — resuming your order.\n\n" + build_proforma_text(sess)}
            return {"text": "Okay — continue."}

    after_note = ("\n\n⏰ " + AFTER_HOURS_NOTE) if is_after_hours() else ""

    # -------------------------
    # GLOBAL JUMP SHORTCUTS
    # Allow jumping to main product menus from most states
    # (We avoid interrupting active order/pro-forma/edit flows.)
    # -------------------------
    _non_interrupt_states = {
        "await_name", "await_phone", "await_confirm",
        "edit_menu", "edit_name", "edit_phone", "edit_county", "edit_model",
        "cancel_confirm", "await_county"
    }
    if sess.get("state") not in _non_interrupt_states:
        
        incubator_phrases = [
            "eggs incubator",
            "egg incubators",
            "eggs incubators"
        ]
        eggs_phrases = [
            "fertile eggs",
            "fertilised eggs",
            "fertilized eggs",
            "kienyeji eggs",
            "eggs for incubation",
            "incubation eggs",
        ]

        cages_phrases = [
            "cage",
            "cages",
            "battery cage",
            "layers cage"
        ]
        
        #incubators global jump
        if digits == "1" or any(p in low for p in incubator_phrases):
            sess["state"] = "prices"
            return {"text": incubator_text()}
            
        #fertile eggs global jump
        if digits == "3" or any(p in low for p in eggs_phrases):
            sess["state"] = "eggs_menu"
            return {"text": fertile_eggs_text()}
            
        # CHICKS GLOBAL JUMP
        is_chicks = bool(re.search(r"\bchicks?\b", low))
        
        if digits == "2" or is_chicks:
            sess["state"] = "chicks_menu"
            return {"text": chicks_info_text()}

        # CAGES GLOBAL JUMP
        if digits == "4" or any(p in low for p in cages_phrases):
            sess["state"] = "cages_menu"
            return {"text": cages_text()}


    # -------------------------
    # MAIN MENU (first interaction)
    # -------------------------
    if low in {"", "hi", "hello", "start", "want", "incubator", "need an incubator", "hi neochicks", "good morning", "good afternoon"} and not sess.get("state"):
        return {"text": main_menu_text(after_note)}

    # -------------------------
    # CHICKS FLOW ENTRY (option 2 OR any text mentioning 'chick')

    # -------------------------
    # TOP-LEVEL NUMERIC HANDLING (1,3,4) when idle
    # 1 = Incubators (prices flow)
    # 2 handled ABOVE by chicks flow
    # -------------------------
        # TOP-LEVEL NUMBERED MAIN MENU (idle)
    if not sess.get("state"):
        # digits was defined at top of brain_reply: digits = re.sub(r"[^0-9]", "", low)

        # 1️⃣ Incubators
        if digits == "1":
            sess["state"] = "prices"
            sess["page"] = 1
            return {"text": price_page_text(page=1)}

        # 2️⃣ Chicks → enter chicks_menu state
        is_chicks = bool(re.search(r"\bchicks?\b", low))

        if digits == "2" or is_chicks:
            sess["state"] = "chicks_menu"
            return {"text": chicks_info_text()}
                # CHICKS PHOTOS (stateful: only when in chicks_menu)
    if sess.get("state") == "chicks_menu":
        if "photo" in low or "photos" in low:
            # 1) Text first
            send_text(from_wa, "📸 *Here are the photos of chicks at different ages:* 🐥")

            # 2) Images one by one
            send_image(
                from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/Day-Old-Kienyeji.jpg",
                "3 Days Old Kienyeji Chicks 🐥"
            )

            send_image(
                from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/One-week-old.jpg",
                "1 Week Old Chicks 🐥"
            )

            send_image(
                from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/two-weeks-old-kienyeji.jpg",
                "2 Weeks Old Chicks 🐥"
            )

            send_image(
                from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/3-weeks-old.jpg",
                "3 Weeks Old Chicks 🐥"
            )

            send_image(
                from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/one-month-old-kienyeji.jpg",
                "4 Weeks Old Chicks 🐥"
            )

            send_image(
                from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/Day-old-layers.jpg",
                "Day-old Layers 🐥"
            )

            send_image(
                from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/mature-layers.jpg",
                "Mature Layers 🐔\n\n"
                "For more information on delivery, availability, or more pictures,\n"
                f"please call us on: {CALL_LINE}\n\n"
                "You can also *order chicks online* using the link below:\n"
                "https://neochickspoultry.com/chicks-booking/"
            )
           
            SESS[from_wa] = {"state": None, "page": 1}
            
            return { }
            
        # allow exiting the chicks flow
        if low in {"menu", "main menu", "back"}:
            SESS[from_wa] = {"state": None, "page": 1}
            return {"text": main_menu_text(after_note)}



        # 3️⃣ Fertile eggs
        eggs_phrases = [
            "fertile eggs",
            "fertilised eggs",
            "fertilized eggs",
            "kienyeji eggs",
            "eggs for incubation",
        ]
        is_eggs = any(phrase in low for phrase in eggs_phrases)

        if digits == "3" or is_eggs:
            sess["state"] = "eggs_menu"
            return {"text": fertile_eggs_text()}
    
        # FERTILE EGGS PHOTOS (after entering eggs_menu)
    if sess.get("state") == "eggs_menu":
        if ("photo" in low or "photos" in low):
            send_text(from_wa, "📸 *Here are the Photos of our Mature Laying Chicken:*\n\n")
            send_image(from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/Kari-scaled.jpg",
                "Our Kari Breed"
            )
        
            send_image(from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/Kenbro-scaled.jpg",
                "Our Kenbro Breed"
            )
        
            send_image(from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/Kuroilers.jpg",
                "Our Kuroilers Breed"
            )
        
            send_image(from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/Rainbow-rooster.jpg",
                "Our Rainbow Rooster Breed\n\n"
                "📸For more information on eggs delivery, availability etc,\n"
                 f"please call us on: {CALL_LINE}\n\n"
                 "You can also visit our website:\n"
                 "https://neochickspoultry.com/kienyeji-farming/"
            )
                # ✅ CLEAR STATE AFTER PHOTOS
            SESS[from_wa] = {"state": None, "page": 1}
            return {}
        if low in {"menu", "main menu", "back"}:
            SESS[from_wa] = {"state": None, "page": 1}
            return {"text": main_menu_text()}

        
        # 4️⃣ Cages & equipment
        cages_phrases = [
            "cage",
            "cages",
            "battery cage",
            "layers cage"
        ]
        is_cages = any(phrase in low for phrase in cages_phrases)
        if digits == "4" or is_cages:
            sess["state"] = "cages_menu"
            return {"text": cages_text()
            }
    if sess.get("state") == "cages_menu":
        if ("photo" in low or "photos" in low):
            send_text(from_wa, "📸 *Here are some Photos of our Layers Cages:*\n\n")
            send_image(from_wa,
                "https://neochickspoultry.com/wp-content/uploads/2025/11/WhatsApp-Image-2025-11-23-at-3.32.11-AM1.jpeg",
                "Battery Cage System 1")
            send_image(from_wa,
                       "https://neochickspoultry.com/wp-content/uploads/2025/11/WhatsApp-Image-2025-11-23-at-3.32.11-AM.jpeg",
                "Battery cages system 2")
            send_image(from_wa,
                      "https://neochickspoultry.com/wp-content/uploads/2025/11/cage-with-chicken.jpg",
                "Battery cages system 3\n\n"
                "📸For more information on Layers Cages, availability, Delivery etc,\n"
                 f"please call us on: {CALL_LINE}\n\n"
                 "You can also visit our website:\n"
                 "https://neochickspoultry.com/poultry-cages/"                
            )
                # ✅ CLEAR STATE AFTER PHOTOS
            SESS[from_wa] = {"state": None, "page": 1}
            return {}            
    # -------------------------
    # AGENT (explicit, matches button title + free text variants)
    # -------------------------
    if any(kw in low for kw in {
        "talk to an agent", "speak to an agent", "agent", "human", "representative",
        "talk to a rep", "customer care", "customer support"
    }):
        SESS[from_wa] = {"state": None, "page": 1}
        return {"text": "👩🏽‍💼 Connecting you to a Neochicks rep… You can also call " + CALL_LINE + "."}

    # -------------------------
    # INCUBATOR ISSUES (explicit match + heuristics)
    # -------------------------
    if ("incubator issues" in low) or any(k in low for k in [
        "troubleshoot", "hatch rate", "problem", "fault", "issue", "issues", "help with incubator"
    ]):
        sess["state"] = None
        return {
            "text": (
                "🛠️ Quick checks for better hatching:\n"
                "1) Temperature 37.8°C (±0.2)\n"
                "2) Humidity 55–60% set / ~65% at hatch\n"
                "3) Turning 3–5×/day (auto OK)\n"
                "4) Candle day 7 & 14; remove clears\n"
                "5) Ventilation okay (no drafts)\n"
                "6) Disinfect after each hatch\n\n"
                f"For urgent help, call {CALL_LINE}."
            )
        }

    # -------------------------
    # INCUBATOR PRICES FLOW
    # -------------------------
    if any(k in low for k in ["capacities", "capacity", "capacities with prices", "prices", "price", "bei", "gharama"]):
        sess["state"] = "prices"
        sess["page"] = 1
        return {"text": price_page_text(page=1)}

    if sess.get("state") == "prices" and low in {"next", "more"}:
        sess["page"] += 1
        return {"text": price_page_text(page=sess["page"])}

    if sess.get("state") == "prices" and low in {"back", "prev", "previous"}:
        sess["page"] = max(1, sess["page"] - 1)
        return {"text": price_page_text(page=sess["page"])}

    if sess.get("state") == "prices":
        m = re.search(r"([0-9]{2,5})", low)
        if m:
            cap = int(m.group(1))
            p = find_by_capacity(cap)
            if p:
                extra = " (Solar)" if p["solar"] else ""
                gen = "\n🎁 Includes *Free Backup Generator*" if p["free_gen"] else ""
                out = {
                    "text": (
                        "📦 *" + p["name"] + "*" + extra +
                        "\nCapacity: " + str(p["capacity"]) + " eggs\nPrice: " + ksh(p["price"]) + gen
                    )
                }
                if p.get("image"):
                    out.update({
                        "mediaUrl": p["image"],
                        "caption": (
                            p["name"] + " — " + ksh(p["price"]) +
                            "\n\n -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  - \n"
                            "Reply with your *county* and I will tell you how long it takes to deliver there 🙏"
                            + PAYMENT_NOTE + "."
                        )
                    })
                sess["last_product"] = p
                return out

    # -------------------------
    # DELIVERY → COUNTY → NAME → PHONE → PRO-FORMA
    # -------------------------
    if ("delivery" in low) or ("deliver" in low) or ("delivery terms" in low):
        return {"text": "🚚 Delivery terms: Nairobi → same day; other counties → 24 hours. " + PAYMENT_NOTE}

    if sess.get("state") == "await_county":
        county = re.sub(r"[^a-z ]", "", low).strip()
        if not county:
            return {"text": "Please type your *county* name (e.g., Nairobi, Nakuru, Mombasa)."}
        eta = delivery_eta_text(county)
        sess["last_county"] = county.title()
        sess["last_eta"] = eta
        sess["state"] = "await_name"
        return {
            "text": (
                f"📍 {county.title()} → Typical delivery {eta}. {PAYMENT_NOTE}.\n"
                "Great! Please share your *full name* for the pro-forma."
            )
        }

    if sess.get("state") == "await_name":
        name = t.strip()
        if len(name) < 2:
            return {"text": "Please type your *full name* (e.g., Jane Wanjiku)."}
        sess["customer_name"] = name
        sess["state"] = "await_phone"
        return {"text": "Thanks! Now your *phone number* (for delivery coordination):"}

    if sess.get("state") == "await_phone":
        phone = re.sub(r"[^0-9+ ]", "", t)
        if len(re.sub(r"\D", "", phone)) < 9:
            return {"text": "That phone seems short. Please type a valid phone (e.g., 07XX... or +2547...)."}
        sess["customer_phone"] = phone
        _leads_add(
            wa_from=from_wa,
            name=sess.get("customer_name", ""),
            phone=phone,
            county=sess.get("last_county", ""),
            intent="new_phone",
            last_text=t,
        )
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    # -------------------------
    # EDIT FLOW
    # -------------------------
    if sess.get("state") == "await_confirm" and "edit" in low:
        sess["state"] = "edit_menu"
        return {
            "text": (
                "What would you like to change?\n"
                "1) Name\n2) Phone\n3) County\n4) Model (capacity)\n\n"
                "Reply with *1, 2, 3,* or *4*.\n"
                "Or type *CANCEL* to discard and go back to the main menu."
            )
        }

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

    if sess.get("state") == "edit_name":
        name = (t or "").strip()
        if len(name) < 2:
            return {"text": "That looks too short. Please type your *full name* (e.g., Jane Wanjiku)."}
        sess["customer_name"] = name
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    if sess.get("state") == "edit_phone":
        phone = re.sub(r"[^0-9+ ]", "", (t or ""))
        if len(re.sub(r"\D", "", phone)) < 9:
            return {"text": "That phone seems short. Please type a valid phone (e.g., 07XX... or +2547...)."}
        sess["customer_phone"] = phone
        _leads_add(
            wa_from=from_wa,
            name=sess.get("customer_name", ""),
            phone=phone,
            county=sess.get("last_county", ""),
            intent="edit_phone",
            last_text=t,
        )
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    if sess.get("state") == "edit_county":
        county_raw = (t or "").strip()
        county = re.sub(r"[^a-z ]", "", county_raw.lower()).strip()
        if not county:
            return {"text": "Please type your *county* name (e.g., Nairobi, Nakuru, Mombasa)."}
        sess["last_county"] = county.title()
        sess["last_eta"] = delivery_eta_text(county)
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    if sess.get("state") == "edit_model":
        m = re.search(r"([0-9]{2,5})", low)
        if not m:
            return {"text": "Please type just the *capacity number* (e.g., 204, 528, 1056)."}
        cap = int(m.group(1))
        p = find_by_capacity(cap)
        if not p:
            return {"text": "I couldn't find that capacity. Try 204, 264, 528, 1056, 5280 etc."}
        sess["last_product"] = p
        sess["state"] = "await_confirm"
        return {"text": build_proforma_text(sess)}

    # -------------------------
    # CONFIRM (same logic as your original)
    # -------------------------
    if sess.get("state") == "await_confirm" and re.fullmatch(r"(?i)\s*confirm\s*", t):
        p = sess.get("last_product") or {}
        county = sess.get("last_county", "-")
        eta = sess.get("last_eta", delivery_eta_text(county))
        order_id = new_order_id()
        created_at = datetime.utcnow()

        order = {
            "id": order_id,
            "wa_from": from_wa,
            "customer_name": sess.get("customer_name", ""),
            "customer_phone": sess.get("customer_phone", ""),
            "county": county,
            "model": p.get("name", ""),
            "capacity": int(p.get("capacity") or 0),
            "price": int(p.get("price") or 0),
            "eta": eta,
            "created_at_utc": created_at.isoformat() + "Z",
        }

        # Notify by email
        subject = f"ORDER CONFIRMED — {order['model']} for {order['customer_name']} ({order_id})"
        body = (
            f"New order confirmation from WhatsApp bot\n\n"
            f"Order ID: {order_id}\n"
            f"Customer Name: {order['customer_name']}\n"
            f"Customer Phone: {order['customer_phone']}\n"
            f"County: {county}\n"
            f"Model: {order['model']}\n"
            f"Capacity: {order['capacity']}\n"
            f"Price: {ksh(order['price'])}\n"
            f"Delivery ETA: {eta}\n"
            f"Payment: {PAYMENT_NOTE}\n"
            f"Timestamp: {created_at.isoformat()}Z\n"
        )
        send_email(subject, body)

        # Generate & store PDF
        INVOICES[order_id] = order
        pdf_bytes = b""
        try:
            pdf_bytes = generate_invoice_pdf(order)
            pdf_path = f"/tmp/{order_id}.pdf"
            with open(pdf_path, "wb") as fh:
                fh.write(pdf_bytes)
            app.logger.info("[invoice] wrote %s (size=%d)", pdf_path, len(pdf_bytes))
        except Exception:
            app.logger.exception("Failed to write invoice PDF to /tmp")

        _cleanup_invoices()

        # WhatsApp: send via media upload (fallback to link/text)
        media_id = upload_media_pdf(pdf_bytes or b"", f"{order_id}.pdf")
        if media_id:
            try:
                send_document_by_id(from_wa, media_id, f"{order_id}.pdf", "Your pro-forma invoice")
            except Exception:
                app.logger.exception("WhatsApp send by media_id failed; falling back to link")
                base = EXTERNAL_BASE or (request.url_root or "").rstrip("/")
                pdf_url = f"{base}/invoice/{order_id}.pdf"
                try:
                    send_document(from_wa, pdf_url, f"{order_id}.pdf", "Your pro-forma invoice")
                except Exception:
                    app.logger.exception("WhatsApp link send failed; falling back to text")
                    try:
                        send_text(from_wa, "Here is your pro-forma invoice: " + pdf_url)
                    except Exception:
                        app.logger.exception("Fallback text send failed")
        else:
            base = EXTERNAL_BASE or (request.url_root or "").rstrip("/")
            pdf_url = f"{base}/invoice/{order_id}.pdf"
            try:
                send_document(from_wa, pdf_url, f"{order_id}.pdf", "Your pro-forma invoice")
            except Exception:
                app.logger.exception("WhatsApp link send failed; falling back to text")
                try:
                    send_text(from_wa, "Here is your pro-forma invoice: " + pdf_url)
                except Exception:
                    app.logger.exception("Fallback text send failed")

        _leads_add(
            wa_from=from_wa,
            name=order["customer_name"],
            phone=order["customer_phone"],
            county=order["county"],
            intent="confirmed",
            last_text=order["model"],
        )

        SESS[from_wa] = {"state": None, "page": 1}
        return {"text": "✅ *Order confirmed!*\nI’ve sent your pro-forma invoice. Our team will contact you shortly to finalize delivery. Thank you for choosing Neochicks."}

    # -------------------------
    # County guess (stateless helper)
    # -------------------------
    c_guess = guess_county(low)
    if c_guess:
        eta = delivery_eta_text(c_guess)
        sess["last_county"] = c_guess.title()
        sess["last_eta"] = eta
        sess["state"] = "await_name"
        return {
            "text": (
                f"📍 {c_guess.title()} → Typical delivery {eta}. {PAYMENT_NOTE}.\n"
                "Great! Please share your *full name* for the pro-forma."
            )
        }

    # -------------------------
    
# Fallback → show main menu again
    SESS[from_wa] = {"state": None, "page": 1}
    print("DEBUG RESETTING STATE...")

    return {"text": "I didn’t quite get that.\n\n" + main_menu_text(after_note)}



# -------------------------
# Routes
# -------------------------
@app.get("/")
def index():
    return (
        "<h2>Neochicks WhatsApp Bot (DB-free)</h2>"
        "<p>Status: <a href='/health'>/health</a></p>"
        "<p>Webhook: /webhook (Meta will call this)</p>"
        "<p>Invoice sample: /invoice/&lt;ORDER_ID&gt;.pdf (after confirmation)</p>"
    ), 200

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

        # ---- audit incoming (masked) ----
        _audit_write({
            "direction": "in",
            "raw_type": msg.get("type"),
            "from": from_wa,
            "text": text,
            "state": SESS.get(from_wa, {}).get("state"),
        })

        reply = brain_reply(text, from_wa)

        # ---- audit outgoing (masked) ----
        _audit_write({
            "direction": "out",
            "to": from_wa,
            "text": reply.get("text"),
            "buttons": reply.get("buttons"),
            "mediaUrl": reply.get("mediaUrl"),
            "caption": reply.get("caption"),
            "state_after": SESS.get(from_wa, {}).get("state"),
        })

        if reply.get("text"):
            try:
                send_text(from_wa, reply["text"])
            except Exception:
                app.logger.exception("Failed to send text reply")
        if reply.get("buttons"):
            try:
                send_buttons(from_wa, reply["buttons"])
            except Exception:
                app.logger.exception("Failed to send buttons")
        if reply.get("mediaUrl"):
            try:
                send_image(from_wa, reply["mediaUrl"], reply.get("caption", ""))
            except Exception:
                app.logger.exception("Failed to send image")
        return "ok", 200
    except Exception:
        app.logger.exception("Webhook error")
        return "error", 200

@app.get("/invoice/<order_id>.pdf")
def invoice(order_id):
    # 1) Serve cached file if present
    tmp_path = f"/tmp/{order_id}.pdf"
    try:
        if os.path.exists(tmp_path):
            app.logger.info("[invoice] serving cached file %s", tmp_path)
            return send_file(tmp_path, mimetype="application/pdf", as_attachment=False, download_name=f"{order_id}.pdf")
    except Exception:
        app.logger.exception("Error reading cached invoice file")

    # 2) Fallback: re-render from in-memory order
    order = INVOICES.get(order_id)
    if not order:
        app.logger.info("Invoice not found: %s", order_id)
        abort(404)

    pdf_bytes = generate_invoice_pdf(order)
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False, download_name=f"{order_id}.pdf")

@app.get("/testmail")
def testmail():
    ok = send_email("Neochicks Test Email", "It works! ✅")
    return ("OK" if ok else "FAIL"), 200

# ---- Daily logs email endpoint (for Render Cron) ----
@app.get("/send_daily_logs")
def send_daily_logs():
    try:
        attachments = []
        if os.path.exists(AUDIT_PATH):
            attachments.append(("wa_audit.jsonl.gz", AUDIT_PATH))
        if os.path.exists(LEADS_CSV):
            attachments.append(("wa_leads.csv", LEADS_CSV))

        if not attachments:
            return "No logs to send", 200

        subject = f"Neochicks Daily Logs — {datetime.utcnow().strftime('%Y-%m-%d')}"
        body = "Daily WhatsApp audit (masked) and leads (raw phones) attached."

        ok = send_email_with_attachments(subject, body, attachments)
        if ok:
            # Optional cleanup: comment these if you prefer to keep accumulating
            # os.remove(AUDIT_PATH)
            # os.remove(LEADS_CSV)
            return "OK", 200
        return "Email send failed", 500
    except Exception:
        app.logger.exception("send_daily_logs failed")
        return "error", 500

# Dashboard reporting
# ==========================================================
# Reporting Dashboard (reads wa_audit.jsonl.gz + wa_leads.csv)
# ==========================================================

def _parse_iso_utc(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        return None

def _to_eat_str(dt_utc: datetime | None):
    if not dt_utc:
        return ""
    dt_eat = dt_utc + timedelta(hours=3)
    return dt_eat.strftime("%Y-%m-%d %H:%M")

def _first_existing(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None

def read_audit(max_items=50000):
    """
    Read masked audit logs from gz jsonl.
    Searches both /data and /tmp to avoid path mismatch.
    """
    path = _first_existing(
        AUDIT_PATH,
        "/data/wa_audit.jsonl.gz",
        "/tmp/wa_audit.jsonl.gz"
    )
    if not path:
        return []

    events = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

        if len(events) > max_items:
            events = events[-max_items:]
        return events
    except Exception:
        app.logger.exception("Failed reading audit")
        return []

def read_leads():
    """
    Read raw leads CSV.
    Searches both /data and /tmp to avoid path mismatch.
    """
    path = _first_existing(
        LEADS_CSV,
        "/data/wa_leads.csv",
        "/tmp/wa_leads.csv"
    )
    if not path:
        return []

    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(r)
        return rows
    except Exception:
        app.logger.exception("Failed reading leads")
        return []


def build_summary(days: int = 30, recent_n: int = 50):
    audit = read_audit()
    leads = read_leads()

    audit_path = _first_existing(AUDIT_PATH, "/data/wa_audit.jsonl.gz", "/tmp/wa_audit.jsonl.gz") or AUDIT_PATH
    leads_path = _first_existing(LEADS_CSV, "/data/wa_leads.csv", "/tmp/wa_leads.csv") or LEADS_CSV

    now_utc = datetime.utcnow()
    start_date = (now_utc - timedelta(days=days-1)).date()

    # ---- Audit metrics ----
    in_count = 0
    out_count = 0
    daily_msgs = defaultdict(lambda: {"in": 0, "out": 0, "total": 0})

    for ev in audit:
        dt = _parse_iso_utc(ev.get("ts_utc"))
        if not dt:
            continue
        day = dt.date()
        if day < start_date:
            continue

        direction = (ev.get("direction") or "").lower()
        if direction == "in":
            in_count += 1
            daily_msgs[str(day)]["in"] += 1
        elif direction == "out":
            out_count += 1
            daily_msgs[str(day)]["out"] += 1

        daily_msgs[str(day)]["total"] += 1

    # Fill trend days
    msg_trend = []
    for i in range(days):
        d = (now_utc - timedelta(days=days-1-i)).date()
        key = str(d)
        msg_trend.append({
            "day": key,
            "in": daily_msgs[key]["in"],
            "out": daily_msgs[key]["out"],
            "total": daily_msgs[key]["total"],
        })

    # ---- Leads metrics ----
    leads_daily = defaultdict(int)
    intents = Counter()
    counties = Counter()

    for r in leads:
        dt = _parse_iso_utc(r.get("ts_utc"))
        if not dt:
            continue
        day = dt.date()
        if day >= start_date:
            leads_daily[str(day)] += 1

        intent = (r.get("intent") or "").strip().lower()
        if intent:
            intents[intent] += 1

        county = (r.get("county") or "").strip().title()
        if county:
            counties[county] += 1

    leads_trend = []
    for i in range(days):
        d = (now_utc - timedelta(days=days-1-i)).date()
        key = str(d)
        leads_trend.append({"day": key, "count": leads_daily.get(key, 0)})

    top_counties = [{"county": c, "count": n} for c, n in counties.most_common(10)]
    intent_breakdown = [{"intent": k, "count": v} for k, v in intents.most_common()]

    confirmed_orders = intents.get("confirmed", 0)

    # ---- Recent tables ----
    recent_audit = []
    for ev in audit[-recent_n:][::-1]:
        dt = _parse_iso_utc(ev.get("ts_utc"))
        recent_audit.append({
            "time_eat": _to_eat_str(dt),
            "direction": ev.get("direction"),
            "text": (ev.get("text") or "")[:220],
            "state": ev.get("state") or ev.get("state_after") or "",
            "raw_type": ev.get("raw_type") or "",
        })

    recent_leads = []
    for r in leads[-recent_n:][::-1]:
        dt = _parse_iso_utc(r.get("ts_utc"))
        recent_leads.append({
            "time_eat": _to_eat_str(dt),
            "name": r.get("customer_name") or "",
            "phone": r.get("customer_phone") or "",
            "county": (r.get("county") or "").title(),
            "intent": r.get("intent") or "",
            "last_text": (r.get("last_text") or "")[:220],
        })

    today_key = str(now_utc.date())
    summary = {
        "kpis": {
            "messages_in": in_count,
            "messages_out": out_count,
            "messages_total": in_count + out_count,
            "leads_total": len(leads),
            "leads_today": leads_daily.get(today_key, 0),
            "confirmed_orders": confirmed_orders,
        },
        "msg_trend": msg_trend,
        "leads_trend": leads_trend,
        "top_counties": top_counties,
        "intent_breakdown": intent_breakdown,
        "recent_audit": recent_audit,
        "recent_leads": recent_leads,
        "paths": {"audit": AUDIT_PATH, "leads": LEADS_CSV},
    }
    return summary


@app.get("/api/summary")
def api_summary():
    return jsonify(build_summary(days=30, recent_n=50))


@app.get("/download/audit")
def download_audit():
    if not os.path.exists(AUDIT_PATH):
        return "Audit file not found", 404
    return send_file(AUDIT_PATH, as_attachment=True, download_name="wa_audit.jsonl.gz")


@app.get("/download/leads")
def download_leads():
    if not os.path.exists(LEADS_CSV):
        return "Leads file not found", 404
    return send_file(LEADS_CSV, as_attachment=True, download_name="wa_leads.csv")

@app.get("/debug/files")
def debug_files():
    dirs = {"/data": [], "/tmp": []}
    for d in dirs:
        try:
            dirs[d] = os.listdir(d)
        except Exception as e:
            dirs[d] = [f"ERROR: {e}"]
    return jsonify(dirs)

@app.get("/dashboard")
def dashboard():
    data = build_summary(days=30, recent_n=50)

    html = """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title>Neochicks Logs Dashboard</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body class="bg-slate-50 text-slate-900">
      <div class="max-w-7xl mx-auto p-4 md:p-8">
        <div class="flex items-center justify-between mb-6">
          <div>
            <h1 class="text-2xl md:text-3xl font-bold">Neochicks Logs Dashboard</h1>
            <div class="text-sm text-slate-500">
              Audit: {{paths.audit}} | Leads: {{paths.leads}}
            </div>
          </div>
          <div class="flex gap-2">
            <a href="/download/audit" class="px-3 py-2 bg-white rounded-xl shadow text-sm hover:bg-slate-100">Download Audit</a>
            <a href="/download/leads" class="px-3 py-2 bg-white rounded-xl shadow text-sm hover:bg-slate-100">Download Leads</a>
          </div>
        </div>

        <!-- KPI cards -->
        <div class="grid grid-cols-1 md:grid-cols-6 gap-3 mb-6">
          {% set k = kpis %}
          <div class="bg-white rounded-2xl shadow p-4">
            <div class="text-slate-500 text-xs">Inbound Msgs (30d)</div>
            <div class="text-2xl font-bold mt-1">{{k.messages_in}}</div>
          </div>
          <div class="bg-white rounded-2xl shadow p-4">
            <div class="text-slate-500 text-xs">Outbound Msgs (30d)</div>
            <div class="text-2xl font-bold mt-1">{{k.messages_out}}</div>
          </div>
          <div class="bg-white rounded-2xl shadow p-4">
            <div class="text-slate-500 text-xs">Total Msgs (30d)</div>
            <div class="text-2xl font-bold mt-1">{{k.messages_total}}</div>
          </div>
          <div class="bg-white rounded-2xl shadow p-4">
            <div class="text-slate-500 text-xs">Leads Total</div>
            <div class="text-2xl font-bold mt-1">{{k.leads_total}}</div>
          </div>
          <div class="bg-white rounded-2xl shadow p-4">
            <div class="text-slate-500 text-xs">Leads Today</div>
            <div class="text-2xl font-bold mt-1">{{k.leads_today}}</div>
          </div>
          <div class="bg-white rounded-2xl shadow p-4">
            <div class="text-slate-500 text-xs">Confirmed Orders</div>
            <div class="text-2xl font-bold mt-1">{{k.confirmed_orders}}</div>
          </div>
        </div>

        <!-- Charts row -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
          <div class="bg-white rounded-2xl shadow p-4">
            <h2 class="font-semibold mb-2">Messages Trend (Last 30 Days)</h2>
            <canvas id="msgChart" height="140"></canvas>
          </div>
          <div class="bg-white rounded-2xl shadow p-4">
            <h2 class="font-semibold mb-2">Leads Trend (Last 30 Days)</h2>
            <canvas id="leadsChart" height="140"></canvas>
          </div>
        </div>

        <!-- Breakdown row -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
          <div class="bg-white rounded-2xl shadow p-4">
            <h2 class="font-semibold mb-2">Top Counties (Leads)</h2>
            <canvas id="countyChart" height="160"></canvas>
          </div>
          <div class="bg-white rounded-2xl shadow p-4">
            <h2 class="font-semibold mb-2">Intent Breakdown</h2>
            <canvas id="intentChart" height="160"></canvas>
          </div>
        </div>

        <!-- Recent tables -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="bg-white rounded-2xl shadow p-4">
            <h2 class="font-semibold mb-3">Recent Audit (masked)</h2>
            <div class="overflow-auto max-h-[480px]">
              <table class="min-w-full text-sm">
                <thead>
                  <tr class="text-left border-b">
                    <th class="py-2 pr-3">Time (EAT)</th>
                    <th class="py-2 pr-3">Dir</th>
                    <th class="py-2 pr-3">State</th>
                    <th class="py-2">Text</th>
                  </tr>
                </thead>
                <tbody>
                  {% for r in recent_audit %}
                  <tr class="border-b last:border-0 align-top">
                    <td class="py-2 pr-3 whitespace-nowrap">{{r.time_eat}}</td>
                    <td class="py-2 pr-3 font-medium">{{r.direction}}</td>
                    <td class="py-2 pr-3 text-slate-600">{{r.state}}</td>
                    <td class="py-2">{{r.text}}</td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>

          <div class="bg-white rounded-2xl shadow p-4">
            <h2 class="font-semibold mb-3">Recent Leads (real phones)</h2>
            <div class="overflow-auto max-h-[480px]">
              <table class="min-w-full text-sm">
                <thead>
                  <tr class="text-left border-b">
                    <th class="py-2 pr-3">Time (EAT)</th>
                    <th class="py-2 pr-3">Name</th>
                    <th class="py-2 pr-3">Phone</th>
                    <th class="py-2 pr-3">County</th>
                    <th class="py-2 pr-3">Intent</th>
                    <th class="py-2">Last Text</th>
                  </tr>
                </thead>
                <tbody>
                  {% for r in recent_leads %}
                  <tr class="border-b last:border-0 align-top">
                    <td class="py-2 pr-3 whitespace-nowrap">{{r.time_eat}}</td>
                    <td class="py-2 pr-3">{{r.name}}</td>
                    <td class="py-2 pr-3 whitespace-nowrap">{{r.phone}}</td>
                    <td class="py-2 pr-3">{{r.county}}</td>
                    <td class="py-2 pr-3 font-medium">{{r.intent}}</td>
                    <td class="py-2">{{r.last_text}}</td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <script>
        const msgTrend = {{msg_trend | tojson}};
        const leadsTrend = {{leads_trend | tojson}};
        const counties = {{top_counties | tojson}};
        const intents = {{intent_breakdown | tojson}};

        // Messages chart (in/out/total)
        new Chart(document.getElementById('msgChart'), {
          type: 'line',
          data: {
            labels: msgTrend.map(x => x.day),
            datasets: [
              { label: 'Inbound', data: msgTrend.map(x => x.in), tension: 0.25 },
              { label: 'Outbound', data: msgTrend.map(x => x.out), tension: 0.25 },
              { label: 'Total', data: msgTrend.map(x => x.total), tension: 0.25 }
            ]
          },
          options: {
            responsive: true,
            scales: { y: { beginAtZero: true }, x: { ticks: { maxTicksLimit: 8 } } }
          }
        });

        // Leads chart
        new Chart(document.getElementById('leadsChart'), {
          type: 'line',
          data: {
            labels: leadsTrend.map(x => x.day),
            datasets: [{ label: 'Leads/day', data: leadsTrend.map(x => x.count), tension: 0.25 }]
          },
          options: {
            responsive: true,
            plugins: { legend: { display: true } },
            scales: { y: { beginAtZero: true }, x: { ticks: { maxTicksLimit: 8 } } }
          }
        });

        // Counties chart
        new Chart(document.getElementById('countyChart'), {
          type: 'bar',
          data: {
            labels: counties.map(x => x.county),
            datasets: [{ label: 'Leads', data: counties.map(x => x.count) }]
          },
          options: { responsive: true, scales: { y: { beginAtZero: true } } }
        });

        // Intent breakdown chart
        new Chart(document.getElementById('intentChart'), {
          type: 'bar',
          data: {
            labels: intents.map(x => x.intent),
            datasets: [{ label: 'Count', data: intents.map(x => x.count) }]
          },
          options: { responsive: true, scales: { y: { beginAtZero: true } } }
        });
      </script>
    </body>
    </html>
    """

    return render_template_string(
        html,
        kpis=data["kpis"],
        msg_trend=data["msg_trend"],
        leads_trend=data["leads_trend"],
        top_counties=data["top_counties"],
        intent_breakdown=data["intent_breakdown"],
        recent_audit=data["recent_audit"],
        recent_leads=data["recent_leads"],
        paths=data["paths"],
    )

# -------------------------
# Run (local only)
# -------------------------
if __name__ == "__main__":
    # In production, use gunicorn with WEB_CONCURRENCY=1
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 3000)))

@app.get("/testpdf")
def testpdf():
    """Quickly preview a sample invoice PDF without going through WhatsApp."""
    sample_order = {
        "id": "TEST-ORDER-123",
        "customer_name": "Jane Wanjiku",
        "customer_phone": "+254712345678",
        "county": "Nairobi",
        "model": "264 Eggs Automatic Incubator",
        "capacity": 264,
        "price": 45000,
        "eta": "same day",
        "created_at_utc": datetime.utcnow().isoformat() + "Z",
    }
    pdf_bytes = generate_invoice_pdf(sample_order)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="test_invoice.pdf"
    )
