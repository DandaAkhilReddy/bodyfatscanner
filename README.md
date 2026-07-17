# 💪 Reddy-Fit Body Scanner

**Open the link → camera turns on → AI looks at you → instant body-fat estimate.**
Take a selfie and log your weight every day — each entry is a labeled data point, and the trend chart shows your progress over time.

**Live Demo:** _(deploy link goes here after Railway deploy)_

## How it works

1. **Camera on load** — front camera opens instantly (browser asks permission once).
2. **Pose detection in the browser** — MediaPipe Pose finds 33 body landmarks; the video never leaves your device for analysis.
3. **Body-fat estimate** — combines the peer-reviewed Deurenberg BMI equation (height, weight, age, sex) with a visual hip-to-shoulder ratio adjustment from the camera.
4. **Daily data points** — save selfie + weight + estimate to the backend. SQLite locally, mirrored to **Azure Blob Storage** when configured. Every day adds a labeled sample (image + weight) — a growing personal dataset for future model fine-tuning.
5. **Progress chart** — body fat % and weight plotted over time.

## Stack

| Layer | Tech |
|---|---|
| Pose AI | MediaPipe Pose (in-browser, offline-capable) |
| Frontend | Vanilla JS + Chart.js |
| Backend | FastAPI (Python 3.11) |
| Storage | SQLite + Azure Blob Storage (optional mirror) |
| Hosting | Railway (Docker) |

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# open http://localhost:8000
```

## Deploy to Railway

```bash
railway login
railway init            # create project "bodyfatscanner"
railway up              # builds Dockerfile, deploys
railway domain          # get the public URL
```

Optional Azure mirror — set variables in Railway:

```
AZURE_STORAGE_CONNECTION_STRING=<your connection string>
AZURE_BLOB_CONTAINER=selfies
```

Add a Railway **volume** mounted at `/app/data` so entries survive redeploys.

## API

- `GET /api/health` — status + entry count
- `POST /api/entries` — save a data point (JSON: date, weight, height, age, sex, bf%, image base64)
- `GET /api/entries` — full history
- `DELETE /api/entries/{id}`

## Disclaimer

Estimates are for personal fitness tracking and demo purposes only — this is not a medical device and not medical advice. For clinical accuracy use DEXA, hydrostatic weighing, or calipers.

---

Built by [Akhil Reddy Danda](https://dandaakhilreddy.com) — Project 11 in the daily-shipping series.
