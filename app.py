from flask import Flask, jsonify, request
from flask_cors import CORS
import json
import os
import sqlite3
import signal
import atexit
import logging
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Persistent storage configuration (deployed 2026-06-12 04:50 UTC)
# Railway env var for volume mount: /var/lib/stack-guardian/data
# Local fallback: ~/.stack-guardian (user-writable)
DB_PATH = os.environ.get('PERSISTENT_DATA_PATH', os.path.expanduser('~/.stack-guardian'))
DB_FILE = os.path.join(DB_PATH, 'stack-guardian.db')

# Ensure the directory exists
os.makedirs(DB_PATH, exist_ok=True)

# In-memory cache (for fast access within same session)
_status = None
_history = []
_agent_activity = {
    "current_agent": None,
    "completed_agents": []
}
MAX_HISTORY = 96  # 48 hours at 30min intervals
MAX_AGENT_HISTORY = 10  # Keep last 10 completed agents

# Optional auth token — set WATCHTOWER_SECRET env var in Railway
SECRET = os.environ.get('WATCHTOWER_SECRET', '')


# ─── Initialization Hook ──────────────────────────────────────────────────────
# Load persisted data on startup (before first request)
# This runs on both local (python app.py) and Railway (Gunicorn) startup
_initialized = False

def init_app():
    """Initialize app with persisted data from SQLite."""
    global _status, _history, _initialized
    if _initialized:
        return

    init_db()
    _status = load_persisted_state()
    _history = load_persisted_history()

    if _status:
        logger.info(f"✓ Recovered state: {_status.get('overall_status')} at {_status.get('last_updated')}")
    else:
        logger.info("⚠ No persisted state found (waiting for first Watchtower run)")
    if _history:
        logger.info(f"✓ Recovered {len(_history)} history snapshots")

    _initialized = True


# ─── Database Functions ──────────────────────────────────────────────────────
def init_db():
    """Initialize SQLite database on startup."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time TEXT NOT NULL,
            overall_status TEXT NOT NULL
        )''')
        conn.commit()
        conn.close()
        logger.info(f"Database initialized at {DB_FILE}")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")


def load_persisted_state():
    """Load most recent status from disk."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT data FROM status ORDER BY id DESC LIMIT 1')
        row = c.fetchone()
        conn.close()
        if row:
            logger.info("Loaded persisted status from disk")
            return json.loads(row[0])
    except Exception as e:
        logger.warning(f"Could not load persisted state: {e}")
    return None


def load_persisted_history():
    """Load history snapshots from disk."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT snapshot_time, overall_status FROM history ORDER BY id DESC LIMIT ?', (MAX_HISTORY,))
        rows = c.fetchall()
        conn.close()
        if rows:
            logger.info(f"Loaded {len(rows)} history snapshots from disk")
            return [{"timestamp": row[0], "overall_status": row[1]} for row in reversed(rows)]
    except Exception as e:
        logger.warning(f"Could not load persisted history: {e}")
    return []


def save_state(data):
    """Persist status to disk."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('INSERT INTO status (data, timestamp) VALUES (?, ?)',
                  (json.dumps(data), datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
        logger.info(f"Status persisted to disk: {data.get('overall_status')}")
    except Exception as e:
        logger.error(f"Failed to save status: {e}")


def save_history_snapshot(timestamp, overall_status):
    """Persist history snapshot to disk."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('INSERT INTO history (snapshot_time, overall_status) VALUES (?, ?)',
                  (timestamp, overall_status))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save history snapshot: {e}")


def graceful_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT gracefully."""
    logger.info(f"Signal {signum} received, shutting down gracefully...")
    if _status:
        save_state(_status)
        logger.info("Final state persisted on shutdown")
    exit(0)


def on_exit():
    """Called by atexit before process exits."""
    if _status:
        save_state(_status)
        logger.info("atexit: Final state persisted")


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
            "message": "Waiting for first Watchtower report...",
            "agent_status": {
                "current_agent": _agent_activity["current_agent"],
                "completed_agents": _agent_activity["completed_agents"][:3]
            }
        })
    # Always include current agent tracking in response
    response = dict(_status)
    response["agent_status"] = {
        "current_agent": _agent_activity["current_agent"],
        "completed_agents": _agent_activity["completed_agents"][:3]
    }
    return jsonify(response)


@app.route('/status', methods=['POST'])
def post_status():
    global _status, _history, _agent_activity

    if not verify_secret(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "invalid JSON"}), 400

    # Always preserve agent activity metadata when updating status
    # This ensures both Watchtower updates and agent updates coexist
    old_agent_activity = _status.get("agent_activity") if _status else None

    # Distinguish between agent activity updates (from bot.py) and full status updates (from watchtower)
    if "agent_activity" in data and "checks" not in data:
        # Agent activity update - merge into existing status without overwriting checks/incidents
        if _status:
            # Preserve existing checks, incidents, overall_status
            _status["agent_activity"] = data["agent_activity"]
            _status["last_updated"] = data.get("last_updated", datetime.now(timezone.utc).isoformat())
            logger.info("Merged agent_activity into existing status (preserved service checks)")
        else:
            # No existing status yet, create minimal one
            _status = {
                "overall_status": "UNKNOWN",
                "checks": {},
                "incidents": [],
                "last_updated": data.get("last_updated", datetime.now(timezone.utc).isoformat()),
                "run_count_today": 0,
                "agent_activity": data["agent_activity"]
            }
            logger.info("Created status with agent_activity (no existing status)")
    else:
        # Full status update from Watchtower - replace entire status but preserve agent activity
        _status = data
        if old_agent_activity:
            _status["agent_activity"] = old_agent_activity
            logger.info("Replaced full status from Watchtower (preserved agent_activity)")
        else:
            logger.info("Replaced full status from Watchtower")

    # Handle agent activity tracking
    if "agent_activity" in data:
        agent_data = data["agent_activity"]
        if agent_data.get("status") == "running":
            _agent_activity["current_agent"] = agent_data
            logger.info(f"Agent running: {agent_data.get('agent_name')}")
        elif agent_data.get("status") == "completed":
            _agent_activity["current_agent"] = None
            _agent_activity["completed_agents"].insert(0, agent_data)
            # Keep only last 10
            if len(_agent_activity["completed_agents"]) > MAX_AGENT_HISTORY:
                _agent_activity["completed_agents"] = _agent_activity["completed_agents"][:MAX_AGENT_HISTORY]
            logger.info(f"Agent completed: {agent_data.get('agent_name')}")

    # Persist merged status to disk immediately
    save_state(_status)

    # Append snapshot to history only if this is a full Watchtower update (has checks)
    if "checks" in data:
        snapshot = {
            "timestamp": data.get("last_updated", datetime.now(timezone.utc).isoformat()),
            "overall_status": data.get("overall_status", "UNKNOWN")
        }
        _history.append(snapshot)
        save_history_snapshot(snapshot["timestamp"], snapshot["overall_status"])

        # Keep only last 96 snapshots in memory
        if len(_history) > MAX_HISTORY:
            _history = _history[-MAX_HISTORY:]
    else:
        # Agent activity update - use timestamp from current status
        snapshot = {
            "timestamp": _status.get("last_updated", datetime.now(timezone.utc).isoformat()),
            "overall_status": _status.get("overall_status", "UNKNOWN")
        }

    return jsonify({"ok": True, "received": snapshot["timestamp"], "agent_activity": _agent_activity})


@app.route('/health', methods=['GET'])
def health():
    # Check if Infra (Claude Code) is active from latest status
    infra_active = False
    if _status and "checks" in _status:
        infra_check = _status["checks"].get("infra_process", {})
        infra_active = infra_check.get("status") == "GREEN"

    return jsonify({
        "status": "ok",
        "service": "stack-guardian-status-api",
        "has_data": _status is not None,
        "history_count": len(_history),
        "agent_status": {
            "infra": "ACTIVE" if infra_active else "INACTIVE",
            "current_agent": _agent_activity["current_agent"],
            "completed_agents": _agent_activity["completed_agents"][:3]  # Last 3
        }
    })


@app.route('/agents', methods=['GET'])
def get_agents():
    """Get current and recent agent activity."""
    return jsonify({
        "current_agent": _agent_activity["current_agent"],
        "completed_agents": _agent_activity["completed_agents"][:10],
        "infra_status": "ACTIVE" if _status and _status.get("checks", {}).get("infra_process", {}).get("status") == "GREEN" else "INACTIVE"
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


# Initialize app with persisted data (runs on both local and Railway startup)
init_app()

if __name__ == '__main__':
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    # Register atexit handler
    atexit.register(on_exit)

    port = int(os.environ.get('PORT', 5055))
    logger.info(f"Starting Stack Guardian API on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
