from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "PureMuse Stage 1 ✅ (Flask + requests)", 200

@app.route("/ping", methods=["GET"])
def ping():
    try:
        r = requests.get("https://api.telegram.org")
        return jsonify({"ok": True, "status": r.status_code}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
