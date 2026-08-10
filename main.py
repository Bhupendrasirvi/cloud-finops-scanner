#!/usr/bin/env python3
"""
FastAPI Server for AWS FinOps Waste Scanner.

Ponytail Note:
- HTML served from dashboard.html (sibling file) — edit UI without touching Python.
- Creds stored in-memory per session cookie with 60-min TTL.
- Upgrade path: AWS STS AssumeRole (no key storage) + Redis for session persistence.
"""

import time
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime
from typing import List, Optional

SESSION_TTL_SECONDS = 3600  # Keys auto-expire after 60 minutes

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import boto3
import uvicorn
import stripe

import aws_waste_scanner


class AWSCreds(BaseModel):
    access_key: str
    secret_key: str
    region: str = "us-east-1"


# ponytail: in-memory session store keyed by cookie. Ceiling: single-process only.
# Upgrade path: swap dict for Redis with TTL, key by user ID.
_SESSIONS: dict[str, tuple] = {}  # sid -> (AWSCreds, created_at_unix)


def _get_creds(request) -> AWSCreds | None:
    """Return valid session creds or None. Raises 401 if expired."""
    sid = request.cookies.get("session_id", "default")
    entry = _SESSIONS.get(sid)
    if not entry:
        return None
    creds, created_at = entry
    if time.time() - created_at > SESSION_TTL_SECONDS:
        del _SESSIONS[sid]
        raise HTTPException(401, "Session expired. Please re-enter your AWS credentials.")
    return creds

app = FastAPI(title="AWS FinOps Waste Scanner API", version="1.0.0")

_DASHBOARD = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

DEMO_FINDINGS = [
    {"category": "Unattached EBS Volume",  "resource_id": "vol-0a1b2c3d4e5f6g",   "region": "us-east-1", "details": "100 GB (gp3) - Unattached for 14 days",        "monthly_waste_usd": 8.00},
    {"category": "Idle EC2 Instance",      "resource_id": "i-0987654321fedcba",    "region": "us-east-1", "details": "Type: t3.large | Avg CPU (7d): 1.2%",          "monthly_waste_usd": 70.00},
    {"category": "Idle EC2 Instance",      "resource_id": "i-0123456789abcdef",    "region": "us-west-2", "details": "Type: m5.xlarge | Avg CPU (7d): 0.8%",         "monthly_waste_usd": 140.00},
    {"category": "Idle RDS Database",      "resource_id": "staging-db-postgres",   "region": "us-east-1", "details": "Class: db.r5.large | Connections (7d): 0",     "monthly_waste_usd": 210.00},
    {"category": "Unassociated Elastic IP","resource_id": "eipalloc-01a2b3c4",     "region": "us-east-1", "details": "Public IP: 54.210.12.99",                       "monthly_waste_usd": 3.60},
]

# In-memory state (upgrade path: swap for DB row per tenant)
SCAN_CACHE = {"last_scanned_at": None, "total_waste_usd": 0.0, "findings": []}


def _cache(findings: list) -> dict:
    global SCAN_CACHE
    SCAN_CACHE = {
        "last_scanned_at": datetime.utcnow().isoformat() + "Z",
        "total_waste_usd": round(sum(f["monthly_waste_usd"] for f in findings), 2),
        "findings": findings,
    }
    return SCAN_CACHE


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    return _DASHBOARD


@app.get("/api/findings")
def get_findings():
    return SCAN_CACHE


@app.post("/api/connect")
def api_connect(creds: AWSCreds, request: Request):
    """Save AWS credentials for this browser session (60-min TTL)."""
    sid = request.cookies.get("session_id", "default")
    _SESSIONS[sid] = (creds, time.time())
    return {"status": "saved", "message": "Credentials saved. Session expires in 60 minutes."}


@app.post("/api/test-connection")
def api_test(request: Request):
    """Validate stored credentials with a quick STS GetCallerIdentity call."""
    creds = _get_creds(request)
    if not creds:
        raise HTTPException(400, "No credentials saved. Fill in the AWS Credentials form first.")
    try:
        identity = boto3.client(
            "sts",
            aws_access_key_id=creds.access_key,
            aws_secret_access_key=creds.secret_key,
            region_name=creds.region,
        ).get_caller_identity()
        return {"status": "ok", "message": f"Connected as: {identity['Arn']}"}
    except Exception as e:
        raise HTTPException(403, str(e))


@app.post("/api/scan")
def trigger_scan(
    request: Request,
    regions: Optional[List[str]] = Query(None),
    role_arn: Optional[str] = None,
    external_id: Optional[str] = None,
    days: int = 7,
):
    """Run live scan. Uses session creds if set, otherwise falls back to environment / AWS CLI config."""
    creds = _get_creds(request)
    scan_regions = regions or ([creds.region] if creds else ["us-east-1"])
    try:
        findings = aws_waste_scanner.run_scanner(
            regions=scan_regions,
            role_arn=role_arn,
            external_id=external_id,
            days=days,
            aws_creds=creds,
        )
    except aws_waste_scanner.NoCredentialsError:
        raise HTTPException(400, "No AWS credentials found. Click 'Configure AWS' or set up 'aws configure'.")
    except Exception as e:
        raise HTTPException(500, f"AWS Scan Error: {e}")
    return _cache(findings)


@app.post("/api/demo-scan")
def trigger_demo_scan():
    return _cache(DEMO_FINDINGS)


# ---------------------------------------------------------------------------
# Sleep Mode — 1-click stop for idle EC2 / RDS resources
# ---------------------------------------------------------------------------

class SleepRequest(BaseModel):
    resource_id: str
    region: str
    category: str          # "Idle EC2 Instance" | "Idle RDS Database"
    monthly_waste_usd: float

# ponytail: in-memory log. Upgrade path: append to DynamoDB table per tenant.
SLEEP_LOG: list[dict] = []


_DEMO_IDS = {f["resource_id"] for f in DEMO_FINDINGS}


@app.post("/api/sleep")
def sleep_resource(req: SleepRequest, request: Request):
    """Stop an idle EC2 instance or RDS database to immediately save money."""
    # Demo mode: fake IDs don't exist in AWS — simulate success so the UI demo works.
    if req.resource_id in _DEMO_IDS:
        entry = {
            "resource_id": req.resource_id,
            "category": req.category,
            "region": req.region,
            "monthly_savings_usd": req.monthly_waste_usd,
            "actioned_at": datetime.utcnow().isoformat() + "Z",
            "action": "stopped (demo)",
        }
        SLEEP_LOG.append(entry)
        return {"status": "ok", "message": f"{req.resource_id} stopped (demo mode).", "entry": entry}

    creds = _get_creds(request)

    def _boto(service):
        kwargs = dict(region_name=req.region)
        if creds:
            kwargs.update(aws_access_key_id=creds.access_key,
                          aws_secret_access_key=creds.secret_key)
        return boto3.client(service, **kwargs)

    try:
        if "EC2" in req.category:
            _boto("ec2").stop_instances(InstanceIds=[req.resource_id])
            action = "stopped"
        elif "RDS" in req.category:
            _boto("rds").stop_db_instance(DBInstanceIdentifier=req.resource_id)
            action = "stopped"
        else:
            raise HTTPException(400, f"Sleep not supported for category: {req.category}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to sleep {req.resource_id}: {e}")

    entry = {
        "resource_id": req.resource_id,
        "category": req.category,
        "region": req.region,
        "monthly_savings_usd": req.monthly_waste_usd,
        "actioned_at": datetime.utcnow().isoformat() + "Z",
        "action": action,
    }
    SLEEP_LOG.append(entry)
    return {"status": "ok", "message": f"{req.resource_id} {action}.", "entry": entry}


@app.get("/api/sleep-log")
def get_sleep_log():
    """Return all actioned sleep events with total savings."""
    total = round(sum(e["monthly_savings_usd"] for e in SLEEP_LOG), 2)
    return {"total_monthly_savings_usd": total, "events": SLEEP_LOG}


# ---------------------------------------------------------------------------
# ROI Email Report — stdlib smtplib, zero extra deps
# Configure via env vars: SMTP_USER, SMTP_PASS, SMTP_HOST, SMTP_PORT
# ---------------------------------------------------------------------------

class ReportRequest(BaseModel):
    email: str


def _build_html_report() -> str:
    findings = SCAN_CACHE.get("findings", [])
    total_waste = SCAN_CACHE.get("total_waste_usd", 0.0)
    scanned_at = SCAN_CACHE.get("last_scanned_at", "Never")
    sleep_total = round(sum(e["monthly_savings_usd"] for e in SLEEP_LOG), 2)

    rows = "".join(
        f"<tr><td style='padding:8px;border-bottom:1px solid #374151;'>{f['category']}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #374151;font-family:monospace'>{f['resource_id']}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #374151;'>{f['region']}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #374151;color:#ef4444;font-weight:bold;'>${f['monthly_waste_usd']:.2f}</td></tr>"
        for f in findings
    ) or "<tr><td colspan='4' style='padding:16px;text-align:center;color:#9ca3af;'>No findings yet. Run a scan first.</td></tr>"

    return f"""
    <html><body style='font-family:sans-serif;background:#111827;color:#f9fafb;padding:32px;'>
    <h1 style='color:#60a5fa;'>⚡ AWS Cloud FinOps — Monthly ROI Report</h1>
    <p style='color:#9ca3af;'>Scan time: {scanned_at}</p>

    <div style='display:flex;gap:24px;margin:24px 0;'>
        <div style='background:#1f2937;padding:20px;border-radius:8px;min-width:160px;'>
            <p style='color:#9ca3af;margin:0;font-size:12px;'>ESTIMATED MONTHLY WASTE</p>
            <p style='color:#ef4444;font-size:32px;font-weight:bold;margin:8px 0;'>${total_waste:.2f}</p>
        </div>
        <div style='background:#1f2937;padding:20px;border-radius:8px;min-width:160px;'>
            <p style='color:#9ca3af;margin:0;font-size:12px;'>SAVINGS ACTIVATED (SLEEP MODE)</p>
            <p style='color:#10b981;font-size:32px;font-weight:bold;margin:8px 0;'>${sleep_total:.2f}</p>
        </div>
        <div style='background:#1f2937;padding:20px;border-radius:8px;min-width:160px;'>
            <p style='color:#9ca3af;margin:0;font-size:12px;'>IDLE RESOURCES FOUND</p>
            <p style='color:#fbbf24;font-size:32px;font-weight:bold;margin:8px 0;'>{len(findings)}</p>
        </div>
    </div>

    <h2 style='color:#e5e7eb;'>Wasted Spend Breakdown</h2>
    <table style='width:100%;border-collapse:collapse;background:#1f2937;border-radius:8px;'>
        <thead><tr style='background:#374151;color:#9ca3af;font-size:12px;text-transform:uppercase;'>
            <th style='padding:10px 8px;text-align:left;'>Category</th>
            <th style='padding:10px 8px;text-align:left;'>Resource ID</th>
            <th style='padding:10px 8px;text-align:left;'>Region</th>
            <th style='padding:10px 8px;text-align:left;'>Monthly Waste</th>
        </tr></thead>
        <tbody>{rows}</tbody>
    </table>

    <p style='color:#6b7280;font-size:12px;margin-top:32px;'>
        Sent by AWS Cloud FinOps Scanner &mdash; <a href='http://localhost:8000' style='color:#60a5fa;'>Open Dashboard</a>
    </p>
    </body></html>
    """


@app.post("/api/send-report")
def send_report(req: ReportRequest):
    """Send the current scan results as an HTML email to the given address."""
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))

    if not smtp_user or not smtp_pass:
        raise HTTPException(
            400,
            "SMTP not configured. Set SMTP_USER and SMTP_PASS environment variables."
        )

    if not SCAN_CACHE.get("last_scanned_at"):
        raise HTTPException(400, "No scan data yet. Run a scan before sending a report.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"AWS FinOps Report — ${SCAN_CACHE['total_waste_usd']:.2f}/mo wasted detected"
    msg["From"] = smtp_user
    msg["To"] = req.email
    msg.attach(MIMEText(_build_html_report(), "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, req.email, msg.as_string())
    except TimeoutError:
        raise HTTPException(504, "SMTP connection timed out. Check SMTP_HOST and SMTP_PORT env vars.")
    except Exception as e:
        raise HTTPException(500, f"Failed to send email: {e}")

    return {"status": "sent", "message": f"Report emailed to {req.email}"}


# ---------------------------------------------------------------------------
# Stripe Checkout Integration ($49/mo Pro Tier)
# ---------------------------------------------------------------------------

@app.post("/api/create-checkout-session")
def create_checkout_session(request: Request):
    """Create a Stripe Checkout session or return demo redirect if key not set."""
    api_key = os.getenv("STRIPE_SECRET_KEY")
    price_id = os.getenv("STRIPE_PRICE_ID")
    base_url = str(request.base_url).rstrip("/")

    if not api_key:
        # Demo / Test Mode redirect when Stripe API key is not configured
        return {"url": f"{base_url}/?success=true&mode=demo"}

    stripe.api_key = api_key
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }] if price_id else [{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': 'Cloud FinOps Pro ($49/mo)',
                        'description': 'Unlimited automated waste scanning & 1-click sleep mode',
                    },
                    'unit_amount': 4900,  # $49.00 USD
                    'recurring': {'interval': 'month'},
                },
                'quantity': 1,
            }],
            mode='subscription',
            success_url=f"{base_url}/?success=true",
            cancel_url=f"{base_url}/?canceled=true",
        )
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(500, f"Stripe Checkout Error: {e}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
