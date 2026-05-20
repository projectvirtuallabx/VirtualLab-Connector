"""
VirtualLab MeshCentral Connector Service
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import json
import logging
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass, asdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("virtuallab.connector")


# ---------------------------------------------------------------------------
# Hardcoded config
# ---------------------------------------------------------------------------

SENDER_EMAIL    = "virtuallabx26@gmail.com"
SENDER_APP_PASS = "ibgk hiqx oqdc pqcg"

BACKEND_POLL_URL     = "https://vlab-backend-dl07.onrender.com/connector/poll"
BACKEND_CALLBACK_URL = "https://vlab-backend-dl07.onrender.com/connector/callback"
SECRET_TOKEN         = "supersecret123"

MESHCTRL_PATH    = r"D:\mesh\node_modules\meshcentral\meshctrl.js"
MESHCENTRAL_URL  = "wss://mesh.virtuallabx.com"
MESHCENTRAL_USER = "admin"
MESHCENTRAL_PASS = "admin"
MESHCTRL_CWD     = r"D:\mesh"

LAB_NODE_MAP: dict[str, str] = {
    "FROST Lab": "33aDw0y$Poy63kz8w2dCEyk2TR1VN0wexLM2PTFwq9FCyIg6mqjms24Mlv4F9cwb",
}

SMTP_HOST    = "smtp.gmail.com"
SMTP_PORT    = 587
SMTP_USE_TLS = True


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ConnectorTaskType(str, Enum):
    BOOKING_CREATE = "BOOKING_CREATE"
    BOOKING_DELETE = "BOOKING_DELETE"
    FILE_TRANSFER  = "FILE_TRANSFER"
    GENERATE_RDP   = "GENERATE_RDP"
    STATUS_UPDATE  = "STATUS_UPDATE"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BookingPayload:
    bookingId:       str
    userId:          str
    userEmail:       str
    labName:         str
    start:           str
    end:             str
    meshNodeId:      str
    durationMinutes: int
    shareId:         Optional[str] = None
    callbackUrl:     Optional[str] = None
    taskType:        str = ConnectorTaskType.GENERATE_RDP


@dataclass
class ConnectorResult:
    bookingId: str
    rdpLink:   Optional[str]
    success:   bool
    error:     Optional[str]
    stdout:    str
    stderr:    str
    shareId:   Optional[str] = None


# ---------------------------------------------------------------------------
# MeshCentral helpers
# ---------------------------------------------------------------------------

def _run_meshctrl(cmd: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=MESHCTRL_CWD,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        err = f"Failed to launch Node.js or meshctrl.js: {exc}"
        log.error(err)
        return 1, "", err
    except subprocess.TimeoutExpired:
        err = "meshctrl command timed out after 60 seconds."
        log.error(err)
        return 1, "", err
    except Exception as exc:
        err = f"Error running meshctrl: {exc}"
        log.error(err)
        return 1, "", err


def _extract_url(text: str) -> Optional[str]:
    match = re.search(r"https?://\S+", text)
    return match.group(0).rstrip(".,;)'\"\\") if match else None


def _extract_share_id(text: str) -> Optional[str]:
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("shareid:"):
            return line.split(":", 1)[1].strip()
        if re.match(r"^id\s*:", line, flags=re.IGNORECASE):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# RDP generation
# ---------------------------------------------------------------------------

def generate_rdp_link(payload: BookingPayload) -> tuple[Optional[str], Optional[str], str, str]:
    utc_time   = datetime.fromisoformat(payload.start.replace("Z", "+00:00"))
    local_time = utc_time.astimezone()
    start_str  = local_time.strftime("%Y-%m-%dT%H:%M:%S")

    mesh_node_id = LAB_NODE_MAP.get(payload.labName, payload.meshNodeId)

    cmd = [
        "node",
        MESHCTRL_PATH,
        "devicesharing",
        "--url",       MESHCENTRAL_URL,
        "--loginuser", MESHCENTRAL_USER,
        "--loginpass", MESHCENTRAL_PASS,
        "--id",        mesh_node_id,
        "--add",       "guest",
        "--type",      "desktop",
        "--start",     start_str,
        "--duration",  str(payload.durationMinutes),
    ]

    log.info("Running meshctrl for bookingId=%s nodeId=%s", payload.bookingId, mesh_node_id)
    returncode, stdout, stderr = _run_meshctrl(cmd)
    output = stdout + stderr
    log.info("MeshCtrl Output: %s", output)

    rdp_link = None
    for line in output.splitlines():
        if line.startswith("URL:"):
            rdp_link = line.replace("URL:", "").strip()
            break

    if not rdp_link:
        rdp_link = _extract_url(output)

    share_id = _extract_share_id(output)

    if rdp_link:
        log.info("Extracted RDP link for bookingId=%s", payload.bookingId)
    else:
        log.warning("No URL found in meshctrl output for bookingId=%s", payload.bookingId)

    return rdp_link, share_id, stdout, stderr


def revoke_rdp_link(payload: BookingPayload) -> tuple[bool, str, str]:
    if not payload.shareId:
        err = "Missing shareId for BOOKING_DELETE task."
        log.error(err)
        return False, "", err

    mesh_node_id = LAB_NODE_MAP.get(payload.labName, payload.meshNodeId)

    cmd = [
        "node",
        MESHCTRL_PATH,
        "devicesharing",
        "--url",       MESHCENTRAL_URL,
        "--loginuser", MESHCENTRAL_USER,
        "--loginpass", MESHCENTRAL_PASS,
        "--id",        mesh_node_id,
        "--remove",    payload.shareId,
    ]

    log.info("Revoking share for bookingId=%s shareId=%s", payload.bookingId, payload.shareId)
    returncode, stdout, stderr = _run_meshctrl(cmd)
    output = stdout + stderr
    log.info("MeshCtrl Revoke Output: %s", output)

    success = returncode == 0 and ("ok" in output.lower() or stderr == "")
    return success, stdout, stderr


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(payload: BookingPayload, rdp_link: str) -> Optional[str]:
    subject = f"Your VirtualLab RDP Link - {payload.labName}"

    text_body = f"""
Hello,

Your remote desktop session for {payload.labName} is ready.

  RDP Link  : {rdp_link}
  Booking ID: {payload.bookingId}
  Start     : {payload.start}
  End       : {payload.end}

Click the link above to connect. It will expire at the end of your booking window.

- The VirtualLab Team
"""

    html_body = f"""
<html><body style="font-family:Arial,sans-serif;color:#333;max-width:600px;margin:auto">
  <h2 style="color:#1a73e8">Your VirtualLab RDP Link</h2>
  <p>Your remote desktop session for <strong>{payload.labName}</strong> is ready.</p>
  <table style="border-collapse:collapse;width:100%">
    <tr><td style="padding:6px;font-weight:bold">RDP Link</td>
        <td style="padding:6px"><a href="{rdp_link}">{rdp_link}</a></td></tr>
    <tr><td style="padding:6px;font-weight:bold">Booking ID</td>
        <td style="padding:6px">{payload.bookingId}</td></tr>
    <tr><td style="padding:6px;font-weight:bold">Start</td>
        <td style="padding:6px">{payload.start}</td></tr>
    <tr><td style="padding:6px;font-weight:bold">End</td>
        <td style="padding:6px">{payload.end}</td></tr>
  </table>
  <p>Click the link above to connect. It expires at the end of your booking window.</p>
  <p style="color:#888;font-size:12px">- The VirtualLab Team</p>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = payload.userEmail
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            if SMTP_USE_TLS:
                server.starttls()
            server.login(SENDER_EMAIL, SENDER_APP_PASS)
            server.sendmail(SENDER_EMAIL, [payload.userEmail], msg.as_string())
        log.info("Email sent to %s for bookingId=%s", payload.userEmail, payload.bookingId)
        return None
    except Exception as exc:
        err = f"Email failed: {exc}"
        log.error(err)
        return err


# ---------------------------------------------------------------------------
# Backend callback
# ---------------------------------------------------------------------------

def post_result_to_backend(result: ConnectorResult, callback_url: Optional[str] = None) -> bool:
    url     = callback_url or BACKEND_CALLBACK_URL
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {SECRET_TOKEN}",
    }

    try:
        resp = requests.post(url, json=asdict(result), headers=headers, timeout=15)
        resp.raise_for_status()
        log.info(
            "Posted result to backend for bookingId=%s (status=%s rdpLink=%s)",
            result.bookingId, resp.status_code, "set" if result.rdpLink else "none",
        )
        return True
    except Exception as exc:
        log.error("Failed to post result for bookingId=%s: %s", result.bookingId, exc)
        return False


# ---------------------------------------------------------------------------
# Task handlers
# ---------------------------------------------------------------------------

def dispatch_task(payload: BookingPayload) -> ConnectorResult:
    task_type = ConnectorTaskType(payload.taskType)

    if task_type in (ConnectorTaskType.GENERATE_RDP, ConnectorTaskType.BOOKING_CREATE):
        return _handle_generate_rdp(payload)
    elif task_type == ConnectorTaskType.BOOKING_DELETE:
        return _handle_booking_delete(payload)
    elif task_type == ConnectorTaskType.FILE_TRANSFER:
        return _handle_file_transfer(payload)
    elif task_type == ConnectorTaskType.STATUS_UPDATE:
        return _handle_status_update(payload)
    else:
        result = ConnectorResult(
            bookingId=payload.bookingId,
            rdpLink=None,
            success=False,
            error=f"Unknown taskType: {payload.taskType}",
            stdout="", stderr="",
            shareId=payload.shareId,
        )
        post_result_to_backend(result, callback_url=payload.callbackUrl)
        return result


def _handle_generate_rdp(payload: BookingPayload) -> ConnectorResult:
    rdp_link, share_id, stdout, stderr = generate_rdp_link(payload)

    email_error: Optional[str] = None
    if rdp_link:
        email_error = send_email(payload, rdp_link)
    else:
        log.warning("Skipping email — no RDP link for bookingId=%s", payload.bookingId)

    error_parts = []
    if not rdp_link:
        error_parts.append("Failed to generate RDP link.")
    if email_error:
        error_parts.append(email_error)

    result = ConnectorResult(
        bookingId=payload.bookingId,
        rdpLink=rdp_link,
        success=rdp_link is not None,
        error="; ".join(error_parts) if error_parts else None,
        stdout=stdout,
        stderr=stderr,
        shareId=share_id,
    )
    post_result_to_backend(result, callback_url=payload.callbackUrl)
    return result


def _handle_booking_delete(payload: BookingPayload) -> ConnectorResult:
    success, stdout, stderr = revoke_rdp_link(payload)
    result = ConnectorResult(
        bookingId=payload.bookingId,
        rdpLink=None,
        success=success,
        error=None if success else "Failed to revoke RDP link.",
        stdout=stdout,
        stderr=stderr,
        shareId=payload.shareId,
    )
    post_result_to_backend(result, callback_url=payload.callbackUrl)
    return result


def _handle_file_transfer(payload: BookingPayload) -> ConnectorResult:
    log.info("FILE_TRANSFER not yet implemented for bookingId=%s", payload.bookingId)
    result = ConnectorResult(
        bookingId=payload.bookingId, rdpLink=None, success=False,
        error="FILE_TRANSFER not yet implemented.",
        stdout="", stderr="", shareId=payload.shareId,
    )
    post_result_to_backend(result, callback_url=payload.callbackUrl)
    return result


def _handle_status_update(payload: BookingPayload) -> ConnectorResult:
    log.info("STATUS_UPDATE not yet implemented for bookingId=%s", payload.bookingId)
    result = ConnectorResult(
        bookingId=payload.bookingId, rdpLink=None, success=False,
        error="STATUS_UPDATE not yet implemented.",
        stdout="", stderr="", shareId=payload.shareId,
    )
    post_result_to_backend(result, callback_url=payload.callbackUrl)
    return result


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def parse_payload(data: dict) -> BookingPayload:
    required = ["bookingId", "userId", "userEmail", "labName", "start", "end", "durationMinutes"]
    missing  = [f for f in required if f not in data]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    lab_name = data["labName"]

    if lab_name in LAB_NODE_MAP:
        mesh_node_id = LAB_NODE_MAP[lab_name]
        log.info("Resolved labName='%s' -> meshNodeId from map", lab_name)
    elif data.get("meshNodeId"):
        mesh_node_id = data["meshNodeId"]
        log.warning("labName='%s' not in LAB_NODE_MAP — using meshNodeId from payload", lab_name)
    else:
        accepted = ", ".join(f'"{k}"' for k in LAB_NODE_MAP)
        raise ValueError(f"Unknown lab '{lab_name}' and no meshNodeId in payload. Accepted: {accepted}")

    return BookingPayload(
        bookingId=data["bookingId"],
        userId=data["userId"],
        userEmail=data["userEmail"],
        labName=lab_name,
        start=data["start"],
        end=data["end"],
        meshNodeId=mesh_node_id,
        durationMinutes=int(data["durationMinutes"]),
        shareId=data.get("shareId"),
        callbackUrl=data.get("callbackUrl"),
        taskType=data.get("taskType", ConnectorTaskType.GENERATE_RDP),
    )


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def poll_backend() -> None:
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {SECRET_TOKEN}",
    }

    log.info("Starting polling loop -> %s", BACKEND_POLL_URL)

    while True:
        try:
            log.info("Polling backend...")
            resp = requests.get(BACKEND_POLL_URL, headers=headers, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                if not data:
                    log.info("No pending task.")
                else:
                    log.info("Task received: bookingId=%s taskType=%s",
                             data.get("bookingId", "?"), data.get("taskType", "?"))
                    try:
                        payload = parse_payload(data)
                        result  = dispatch_task(payload)
                        log.info(
                            "Task done: bookingId=%s success=%s rdpLink=%s",
                            result.bookingId, result.success,
                            "set" if result.rdpLink else "none",
                        )
                    except ValueError as exc:
                        log.error("Payload error: %s", exc)

            elif resp.status_code == 401:
                log.error("Unauthorized — SECRET_TOKEN mismatch with backend")
            else:
                log.warning("Unexpected status %s from backend", resp.status_code)

        except Exception as exc:
            log.error("Polling error: %s", exc)

        time.sleep(5)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_cli(args) -> None:
    if args.json:
        data = json.loads(args.json)
    elif not sys.stdin.isatty():
        data = json.load(sys.stdin)
    else:
        log.error("Provide booking JSON via --json '...' or stdin.")
        sys.exit(1)

    try:
        payload = parse_payload(data)
    except ValueError as exc:
        log.error("Invalid payload: %s", exc)
        sys.exit(1)

    result = dispatch_task(payload)
    print(json.dumps(asdict(result), indent=2))
    sys.exit(0 if result.success else 1)


# ---------------------------------------------------------------------------
# Flask server
# ---------------------------------------------------------------------------

def run_server(host: str, port: int) -> None:
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        log.error("Flask not installed. Run: pip install flask")
        sys.exit(1)

    app = Flask("virtuallab.connector")

    def _check_auth() -> Optional[tuple]:
        if request.headers.get("Authorization") != f"Bearer {SECRET_TOKEN}":
            return jsonify({"error": "Unauthorized"}), 401
        return None

    @app.post("/connector/generate-rdp")
    def endpoint_generate_rdp():
        auth_err = _check_auth()
        if auth_err:
            return auth_err
        try:
            payload = parse_payload(request.get_json(force=True))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        result = dispatch_task(payload)
        return jsonify(asdict(result)), 200 if result.success else 500

    @app.post("/connector/task")
    def endpoint_task():
        auth_err = _check_auth()
        if auth_err:
            return auth_err
        try:
            payload = parse_payload(request.get_json(force=True))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        result = dispatch_task(payload)
        return jsonify(asdict(result)), 200 if result.success else 500

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    log.info("Starting connector service on %s:%s", host, port)
    app.run(host=host, port=port)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="VirtualLab MeshCentral Connector")
    parser.add_argument("--serve", action="store_true", help="Run as Flask web service.")
    parser.add_argument("--json",  metavar="JSON",      help="Booking payload as JSON string.")
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--port",  type=int, default=8000)
    args = parser.parse_args()

    if args.serve:
        run_server(args.host, args.port)
    elif args.json:
        run_cli(args)
    else:
        poll_backend()


if __name__ == "__main__":
    main()
