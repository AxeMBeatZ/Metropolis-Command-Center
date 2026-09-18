# Metropolis AI Command Center

A prototype for the Metropolis Healthcare diagnostic-report communication workflow:
AI drafting and flagging, a mandatory human approval gate, simulated multi-channel
communication, and an audit trail.

All cases and recipients are synthetic. Communication delivery is a UI simulation only.

## Contents

| File | Purpose |
|---|---|
| `index.html` | The full interactive prototype. No build, server, or dependencies. |
| `app.py` | Python server + rules engine. Serves the prototype and exposes the flagging logic as a JSON API. |

## Option 1 — GitHub Pages (static, free)

1. Create a new GitHub repository.
2. Upload the contents of this folder to the repository root.
3. **Settings → Pages** → *Deploy from a branch* → `main` → `/ (root)`.
4. GitHub publishes at `https://<username>.github.io/<repository-name>/`.

GitHub Pages serves `index.html` only. `app.py` still lives in the repo for review,
it simply is not executed there.

## Option 2 — Run the Python app

Standard library only. Python 3.9+.

```bash
python app.py              # http://localhost:8000
python app.py --port 5000
python app.py --demo       # runs the rules engine in the console, no server
```

To deploy the Python version, use a host that runs Python processes
(Render, Railway, PythonAnywhere, Hugging Face Spaces). The app reads the
`PORT` environment variable, so no code change is needed.

## API

| Method | Endpoint | Returns |
|---|---|---|
| GET | `/api/health` | Liveness check |
| GET | `/api/cases` | All synthetic cases |
| GET | `/api/cases/<case_id>` | One case plus its analysis |
| POST | `/api/analyze` | Runs the rules engine on submitted values |

```bash
curl -X POST localhost:8000/api/analyze \
  -d '{"case_id":"TEST","results":[{"analyte":"Potassium","value":2.6,"unit":"mmol/L","low":3.5,"high":5.1}]}'
```

## How the rules engine works

`app.py` compares each reported value against its supplied reference range and grades
how far outside it falls, as a fraction of the range width:

| Deviation | Severity |
|---|---|
| ≥ 1.00 × range width | Critical |
| ≥ 0.35 × range width | High |
| Anything else out of range | Medium |

Flag severity then drives a suggested reviewer queue. Every response carries
`release_state: BLOCKED_PENDING_HUMAN_APPROVAL` — the engine drafts and flags,
it never diagnoses, approves, or releases.
