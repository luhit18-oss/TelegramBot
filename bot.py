from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "PureMuse Safe Mode ✅", 200

@app.route("/healthz", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200
