from flask import Flask, request, jsonify
import os, json, requests

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "changeme")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GRAPH_BASE = "https://graph.facebook.com/v20.0"

def headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

def send_text(to: str, body: str):
    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(url, headers=headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

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
        # Extract sender and message text (buttons/text supported)
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return "no message", 200

        msg = messages[0]
        from_wa = msg.get("from")  # phone number in international format
        text = ""
        if msg.get("type") == "text":
            text = msg.get("text", {}).get("body", "")
        elif msg.get("type") == "interactive":
            inter = msg.get("interactive", {})
            if inter.get("type") == "button_reply":
                text = inter.get("button_reply", {}).get("title", "")
            elif inter.get("type") == "list_reply":
                text = inter.get("list_reply", {}).get("title", "")

        # Simple auto-reply
        reply = "Karibu Neochicks! \n"
        "Type *menu*, *prices*, *delivery*, or *agent*."
        t = (text or "").strip().lower()
        if t in {"hi", "hello", "menu", ""}:
            reply = ("Karibu Neochicks Poultry Ltd! \n"
                     "I am your 24Hours assistant \n"
                     "Please reply by typing one of the following: \n"
                     "• Incubator Prices\n• Incubator Capacities\n• Delivery Terms\n• Incubator Repair\n• Agent\n\n"
                     "This line is chat-only. For calls, use: 0707 787884")
        elif "incubator prices" in t:
            reply = "We have different Capacities of Eggs Incubators Between 64 Eggs up t0 5280 Eggs. Tell me your target capacity & county for a quote."
        elif "delivery terms" in t:
            reply = "We offer free deliver nationwide. You Pay on delivery. Delivery done within 24 Hours."
            elif "incubator repair" in t:
            reply = "Kindly click on this page and see if it helps in your issue: http://neochickspoultry.com/troubleshoot \n"
                  "if you do not get help from there, call our technician on: 0793585968"
        elif "agent" in t:
            reply = "Connecting you to a human agent… You can also call or whatsapp 0707 787884."

        send_text(from_wa, reply)
        return "ok", 200
    except Exception as e:
        print("Webhook error:", e, "payload:", json.dumps(data)[:1000])
        # Return 200 so Meta doesn’t retry storm
        return "error", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 3000)))
