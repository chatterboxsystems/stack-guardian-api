# Stack Guardian Status API

Railway-hosted Flask API that receives status POSTs from Watchtower on the VPS and serves them over HTTPS to the dashboard.

## Flow

```
VPS Watchtower (every 15 min)
  → POST /status (with JSON payload)
  → Railway Flask API stores in memory
  → Dashboard polls GET /status every 60s over HTTPS
```

## Endpoints

- `GET /status` — current system status
- `POST /status` — Watchtower posts here after each run
- `GET /health` — liveness check
- `GET /history` — last 48h snapshots

## Environment Variables (set in Railway)

| Variable | Purpose | Required |
|---|---|---|
| `WATCHTOWER_SECRET` | Auth token for POST /status | Optional but recommended |
| `PORT` | Auto-set by Railway | Auto |

## Watchtower Config

After deploying, add these to the VPS `watchtower.py`:

```python
RAILWAY_API_URL = "https://your-service.up.railway.app"
WATCHTOWER_SECRET = "your_secret_here"  # match Railway env var
```

Watchtower POSTs the status JSON after every run.
