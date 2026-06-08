from flask import Flask, jsonify, request
from flask_cors import CORS
import json
import os
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)

# In-memory store — Watchtower POSTs here after every run
_status = None
_history = []
MAX_HISTORY = 96  # 48 hours at 30min intervals

# Optional auth token — set WATCHTOWER_SECRET env var in Railway
SECRET = os.environ.get('WATCHTOWER_SECRET', '')


def verify_secret(req):
    if not SECRET:
        return True  # no auth configured, allow all
    token = req.headers.get('X-Watchtower-Secret', '')
    return token == SECRET


@app.route('/status', methods=['GET'])
def get_status():
    if _status is None:
        return jsonify({
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "overall_status": "UNKNOWN",
            "checks": {},
            "incidents": [],
            "run_count_today": 0,
            "message": "Waiting for first Watchtower report..."
        })
    return jsonify(_status)


@app.route('/status', methods=['POST'])
def post_status():
    global _status, _history

    if not verify_secret(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "invalid JSON"}), 400

    _status = data

    # Append snapshot to history
    snapshot = {
        "timestamp": data.get("last_updated", datetime.now(timezone.utc).isoformat()),
        "overall_status": data.get("overall_status", "UNKNOWN")
    }
    _history.append(snapshot)

    # Keep only last 96 snapshots
    if len(_history) > MAX_HISTORY:
        _history = _history[-MAX_HISTORY:]

    return jsonify({"ok": True, "received": snapshot["timestamp"]})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "service": "stack-guardian-status-api",
        "has_data": _status is not None,
        "history_count": len(_history)
    })


@app.route('/history', methods=['GET'])
def get_history():
    return jsonify({"snapshots": _history})


@app.route('/', methods=['GET'])
def root():
    return jsonify({
        "service": "Stack Guardian Status API",
        "version": "1.0",
        "endpoints": ["/status", "/health", "/history"]
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5055))
    app.run(host='0.0.0.0', port=port)
