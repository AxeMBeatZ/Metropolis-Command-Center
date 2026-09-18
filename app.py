"""
Metropolis AI Command Center - local server and rules API.

Runs the prototype without any external packages:

    python app.py            # then open http://localhost:8000
    python app.py --port 5000

Endpoints
    GET  /                       serves index.html (the prototype UI)
    GET  /api/cases              all synthetic cases
    GET  /api/cases/<case_id>    a single case
    POST /api/analyze            runs the rules engine on submitted values
    GET  /api/health             liveness check

Safety note: all data here is synthetic. The rules engine only compares
values against supplied reference ranges. It does not diagnose, and no
output is releasable without an explicit human approval step.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# Synthetic case data (mirrors the dataset used by the browser prototype)
# --------------------------------------------------------------------------

CASES = [
    {
        "id": "MTR-001",
        "patient": "DEMO-P001",
        "test": "Complete Blood Count",
        "department": "Pathology",
        "priority": "High",
        "status": "Pending Review",
        "reviewer": "Dr. Demo Reviewer",
        "results": [
            {"analyte": "Hemoglobin", "value": 10.2, "unit": "g/dL", "low": 12.0, "high": 16.0},
            {"analyte": "RDW", "value": 15.4, "unit": "%", "low": 11.5, "high": 14.5},
            {"analyte": "WBC", "value": 7.1, "unit": "10^3/uL", "low": 4.0, "high": 11.0},
            {"analyte": "Platelets", "value": 240, "unit": "10^3/uL", "low": 150, "high": 410},
        ],
    },
    {
        "id": "MTR-002",
        "patient": "DEMO-P002",
        "test": "Liver Function Test",
        "department": "Biochemistry",
        "priority": "Medium",
        "status": "AI Ready",
        "reviewer": "Dr. R. Shah",
        "results": [
            {"analyte": "ALT", "value": 58, "unit": "U/L", "low": 7, "high": 56},
            {"analyte": "AST", "value": 31, "unit": "U/L", "low": 10, "high": 40},
            {"analyte": "Bilirubin", "value": 0.9, "unit": "mg/dL", "low": 0.2, "high": 1.2},
        ],
    },
    {
        "id": "MTR-003",
        "patient": "DEMO-P003",
        "test": "Thyroid Profile",
        "department": "Pathology",
        "priority": "Normal",
        "status": "AI Ready",
        "reviewer": "Dr. Demo Reviewer",
        "results": [
            {"analyte": "TSH", "value": 2.4, "unit": "mIU/L", "low": 0.4, "high": 4.0},
            {"analyte": "Free T4", "value": 1.2, "unit": "ng/dL", "low": 0.8, "high": 1.8},
        ],
    },
    {
        "id": "MTR-005",
        "patient": "DEMO-P005",
        "test": "HbA1c",
        "department": "Biochemistry",
        "priority": "High",
        "status": "Approved",
        "reviewer": "Dr. M. Iyer",
        "results": [
            {"analyte": "HbA1c", "value": 7.1, "unit": "%", "low": 4.0, "high": 5.6},
        ],
    },
    {
        "id": "MTR-008",
        "patient": "DEMO-P008",
        "test": "Electrolyte Panel",
        "department": "Emergency",
        "priority": "Critical",
        "status": "Pending Review",
        "reviewer": "Dr. Demo Reviewer",
        "results": [
            {"analyte": "Potassium", "value": 2.9, "unit": "mmol/L", "low": 3.5, "high": 5.1},
            {"analyte": "Sodium", "value": 138, "unit": "mmol/L", "low": 135, "high": 145},
        ],
    },
]

# --------------------------------------------------------------------------
# Rules engine
# --------------------------------------------------------------------------

# How far outside the reference range a value has to fall before the flag is
# escalated. Expressed as a fraction of the range width.
SEVERITY_BANDS = [(1.00, "Critical"), (0.35, "High"), (0.0, "Medium")]


def severity_for(value: float, low: float, high: float) -> str:
    """Grade how far a value sits outside its supplied reference range."""
    width = high - low
    if width <= 0:
        return "Medium"
    if value < low:
        deviation = (low - value) / width
    else:
        deviation = (value - high) / width
    for threshold, label in SEVERITY_BANDS:
        if deviation >= threshold:
            return label
    return "Medium"


def flag_results(results: list[dict]) -> list[dict]:
    """Compare each result against its supplied reference range."""
    flags = []
    for row in results:
        value, low, high = row.get("value"), row.get("low"), row.get("high")
        if value is None or low is None or high is None:
            continue
        if low <= value <= high:
            continue
        direction = "Below" if value < low else "Above"
        flags.append(
            {
                "analyte": row.get("analyte", "Unknown"),
                "value": f"{value} {row.get('unit', '')}".strip(),
                "reference": f"{low}-{high} {row.get('unit', '')}".strip(),
                "severity": severity_for(value, low, high),
                "note": f"{direction} supplied reference range",
            }
        )
    return flags


# --------------------------------------------------------------------------
# RTOCF prompt (Role, Task, Output, Constraints, Format)
#
# This is the final V3 prompt from the case's Stage 1, Section 4 prompt-
# engineering exercise. It is the prompt that WOULD be sent to a generative
# model to produce the Doctor Summary / Patient Summary / Flags for Review.
#
# This prototype's own draft_summaries()/analyze() below use a deterministic
# rules engine instead of calling a model, so the demo runs offline with no
# API key and no per-call cost. This function exists so the RTOCF prompt is
# a real, runnable artifact in the codebase rather than only living in the
# write-up: it assembles the exact prompt from a case's data, which could be
# passed to any chat-completion API (e.g. the Anthropic Messages API) as the
# user message to get a generated, rather than rule-based, draft.
# --------------------------------------------------------------------------

RTOCF_TEMPLATE = """ROLE:
You are an AI clinical-communication assistant supporting a diagnostics
lab's report communication workflow. You are not a diagnosing clinician;
your outputs are always reviewed and approved by a licensed pathologist/
radiologist before release.

TASK:
Given the structured diagnostic report data below, (1) draft a plain-
language summary of the findings for the referring doctor, (2) draft a
simpler, jargon-free summary for the patient, and (3) flag any values
that fall outside the normal reference range as "requires priority
clinician review."

OUTPUT:
Three distinct sections: "Doctor Summary," "Patient Summary," and
"Flags for Review" (a bulleted list; state "No abnormal flags" if none).

CONSTRAINTS:
- Do not state a diagnosis, prognosis, or treatment recommendation.
- Do not omit any abnormal value from the Flags section.
- Use neutral, non-alarming language in the Patient Summary.
- If any data field is missing or ambiguous, state this explicitly
  rather than guessing.
- End every output with: "This summary is AI-generated and requires
  clinician review and approval before release."

FORMAT:
Markdown with the three headers above. Doctor Summary: max 100 words.
Patient Summary: max 80 words, 6th-grade reading level. Flags: bullet list.

REPORT DATA:
{report_data}"""


def build_rtocf_prompt(case: dict) -> str:
    """Fill the RTOCF template with one case's structured report data.

    Returns the exact prompt text that would be sent as the user message
    to a generative model. Report data is serialized as JSON so it stays
    machine-parseable for the model while remaining human-readable here.
    """
    report_data = json.dumps(
        {
            "case_id": case.get("id"),
            "test": case.get("test"),
            "department": case.get("department"),
            "results": case.get("results", []),
        },
        indent=2,
    )
    return RTOCF_TEMPLATE.format(report_data=report_data)


def draft_summaries(case: dict, flags: list[dict]) -> dict:
    """Produce the two draft summaries. Both remain unreleasable until approved."""
    if not flags:
        return {
            "doctor": (
                "All reported values are within their supplied reference ranges. "
                "This is a structured observation only and requires clinician "
                "review before communication."
            ),
            "patient": (
                "The values listed in this report are within the laboratory's "
                "reference ranges. A qualified clinician will review the complete report."
            ),
        }

    listed = ", ".join(f"{f['analyte']} ({f['value']})" for f in flags)
    return {
        "doctor": (
            f"{len(flags)} value(s) fall outside the supplied reference ranges: {listed}. "
            "These are range comparisons only and require clinician review in the "
            "context of the full report."
        ),
        "patient": (
            "Some values in this report are outside the laboratory's reference range. "
            "A qualified clinician will review the results and determine any next steps."
        ),
    }


def analyze(case: dict) -> dict:
    """Run the full draft-and-flag step for one case."""
    flags = flag_results(case.get("results", []))
    severities = [f["severity"] for f in flags]
    if "Critical" in severities:
        routing = "Priority reviewer queue"
    elif flags:
        routing = "Standard reviewer queue"
    else:
        routing = "Routine verification queue"

    return {
        "case_id": case.get("id"),
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
        "flags": flags,
        "flag_count": len(flags),
        "suggested_routing": routing,
        "summaries": draft_summaries(case, flags),
        "release_state": "BLOCKED_PENDING_HUMAN_APPROVAL",
        "disclaimer": (
            "AI-generated draft. A qualified clinician must review and explicitly "
            "approve this content before any release."
        ),
    }


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def log_message(self, fmt, *args):  # quieter console
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/api/health":
            return self.send_json({"status": "ok", "cases": len(CASES)})

        if path == "/api/cases":
            return self.send_json({"count": len(CASES), "cases": CASES})

        if path.startswith("/api/cases/"):
            case_id = path.rsplit("/", 1)[-1].upper()
            case = next((c for c in CASES if c["id"] == case_id), None)
            if case is None:
                return self.send_json({"error": f"Case {case_id} not found"}, 404)
            return self.send_json({"case": case, "analysis": analyze(case)})

        if path.startswith("/api/prompt/"):
            case_id = path.rsplit("/", 1)[-1].upper()
            case = next((c for c in CASES if c["id"] == case_id), None)
            if case is None:
                return self.send_json({"error": f"Case {case_id} not found"}, 404)
            return self.send_json({"case_id": case_id, "rtocf_prompt": build_rtocf_prompt(case)})

        if path.startswith("/api/"):
            return self.send_json({"error": "Unknown endpoint"}, 404)

        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path != "/api/analyze":
            return self.send_json({"error": "Unknown endpoint"}, 404)

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "Request body must be valid JSON"}, 400)

        results = payload.get("results")
        if not isinstance(results, list):
            return self.send_json({"error": "Expected a 'results' list"}, 400)

        case = {"id": payload.get("case_id", "AD-HOC"), "results": results}
        return self.send_json(analyze(case))


def demo_run() -> None:
    """Print a console walkthrough without starting the server."""
    for case in CASES:
        result = analyze(case)
        print(f"\n{case['id']} - {case['test']} ({case['priority']})")
        print(f"  Flags: {result['flag_count']} | Routing: {result['suggested_routing']}")
        for flag in result["flags"]:
            print(f"   - {flag['analyte']}: {flag['value']} vs {flag['reference']} [{flag['severity']}]")
        print(f"  Release state: {result['release_state']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Metropolis AI Command Center server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--demo", action="store_true", help="run the rules engine in the console and exit")
    parser.add_argument("--prompt", metavar="CASE_ID", help="print the filled RTOCF prompt for a case and exit")
    args = parser.parse_args()

    if args.prompt:
        case = next((c for c in CASES if c["id"] == args.prompt.upper()), None)
        if case is None:
            print(f"Case {args.prompt} not found. Known cases: {', '.join(c['id'] for c in CASES)}")
            return
        print(build_rtocf_prompt(case))
        return

    if args.demo:
        return demo_run()

    server = HTTPServer((args.host, args.port), Handler)
    print(f"Metropolis AI Command Center running at http://localhost:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
