from flask import Flask, request, jsonify
import os, requests

app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.get("/webhook")
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == os.getenv("VERIFY_TOKEN", "changeme"):
        return challenge, 200
    return "forbidden", 403

@app.post("/webhook")
def webhook():
    data = request.get_json()
    print("Incoming:", data)
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 3000)))
