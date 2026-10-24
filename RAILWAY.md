# Deploy to Railway

Railway runs the **engine itself** — a long-lived container, not a serverless
function. Nothing here needs Vercel.

## Option A — from GitHub (recommended)

1. Push this repo to GitHub as **`omniscience-terminal`**.
2. Railway → **New Project → Deploy from GitHub repo** → pick `omniscience-terminal`.
3. Railway detects the `Dockerfile` and builds it. `railway.json` already sets:
   - builder `DOCKERFILE`
   - start command `bash run.sh`
   - healthcheck `/api/health` (120 s grace — first boot trains the models)
   - restart policy `ALWAYS`, replicas `1`
4. **Add a volume** — this is the one step that is easy to miss:
   Service → **New Volume**, mount path **`/app/data`**.
   Without it the SQLite trade history, journal and trained weights are wiped on
   every deploy.
5. Expose the port. Railway injects `PORT`; `run.sh` binds `0.0.0.0:$PORT`, so
   leave it alone. For a public URL: *Settings → Networking → Generate Domain*.

## Option B — Railway CLI, no GitHub

```bash
npm i -g @railway/cli
railway login
railway init                      # create project "omniscience-terminal"
railway up                        # builds the Dockerfile and deploys
railway volume add -m /app/data   # persist state
railway domain                    # public URL
```

## Environment variables (all optional)

| Variable | Default | Notes |
|---|---|---|
| `PORT` | injected by Railway | do not set manually |
| `OMNI_MODE` | `PAPER` | `PAPER` or `PROP` |
| `OMNI_CAPITAL` | `1000` | sizing equity |
| `OMNI_RISK_PCT` | `0.01` | 1% per trade |
| `OMNI_FOCUS` | `BTC-USDT-SWAP` | instrument with full depth + ML |
| `OMNI_ML_1M_DAYS` | `30` | days of 1m bars trained at boot |
| `OMNI_DB` | `/app/data/omniscience.db` | must live on the volume |

No exchange API keys are required: the terminal reads public market data only.

## Resource sizing

* **RAM**: 1 GB is comfortable (Python + numpy + scikit-learn, 12 LSTM models).
  512 MB also runs — models train slightly slower.
* **Boot**: 45–90 s. The container serves the UI immediately; the healthcheck
  passes as soon as FastAPI is up, and the ML panel shows training progress.
* **CPU**: 1 vCPU is enough. A GA evolution pass takes a few hundred ms.
* **Replicas must stay at 1.** The engine is stateful (open websockets, broker
  positions, in-memory models); scaling out would create competing brokers and
  duplicate paper trades.

## Verify a deploy

```bash
curl https://<your-app>.up.railway.app/api/health
# {"ok":true,"uptime":...,"clients":0,"ml_ready":true,"wire":true}

curl https://<your-app>.up.railway.app/api/snapshot | head -c 400
```

Open the domain: the terminal links to the engine on the same origin
automatically. The link gate only appears if the engine is unreachable.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Deploy builds, then "Application failed to respond" | No `PORT` binding — do not override `PORT`; `run.sh` already reads it |
| Healthcheck times out at 60 s | Keep `healthcheckTimeout: 120`; first boot pulls 30 days of 1m bars and trains |
| Trades/journal vanish after redeploy | Volume not mounted at `/app/data` |
| `429` from the calendar feed | Expected; the FOMC shield keeps working and the UI marks the calendar `DEGRADED` |
| Container restarts repeatedly | Check logs for `OSError: [Errno 30] Read-only file system` → `OMNI_DB` must point inside `/app/data` |
