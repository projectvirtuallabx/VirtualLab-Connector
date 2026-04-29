"""
VirtualLab MeshCentral Connector Service
=========================================
Replaces Node.js logic for generating MeshCentral RDP links and emailing them.

Can run as:
  - CLI: python meshcentral_connector.py --json '{"bookingId": ..., ...}'
  - Web service: python meshcentral_connector.py --serve

Environment Variables (required for all modes):
  MESHCTRL_PATH       Path to meshctrl binary (default: meshctrl)
  MESHCENTRAL_URL     MeshCentral server URL (e.g. https://mesh.example.com)
  MESHCENTRAL_USER    MeshCentral login username
  MESHCENTRAL_PASS    MeshCentral login password

  SMTP_HOST           SMTP server hostname
  SMTP_PORT           SMTP server port (default: 587)
  SMTP_USE_TLS        Use STARTTLS (default: true)

Optional:
  BACKEND_CALLBACK_URL  If set, POST results back to this URL after processing
  SERVICE_PORT          Port for the web service (default: 8000)
  SERVICE_HOST          Host to bind the web service (default: 0.0.0.0)
  SECRET_TOKEN          Bearer token for authenticating incoming requests
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

import requests  # pip install requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("virtuallab.connector")


# ---------------------------------------------------------------------------
# Hardcoded sender email credentials
# ---------------------------------------------------------------------------

SENDER_EMAIL    = "virtuallabx26@gmail.com"       # <-- change this
SENDER_APP_PASS = "ibgk hiqx oqdc pqcg"    # <-- change this (Gmail app password)


# ---------------------------------------------------------------------------
# Lab Name -> MeshCentral Node ID mapping
# Only lab names listed here are accepted. All others are rejected.
# ---------------------------------------------------------------------------

LAB_NODE_MAP: dict[str, str] = {
    "FROST Lab":          "33aDw0y$Poy63kz8w2dCEyk2TR1VN0wexLM2PTFwq9FCyIg6mqjms24Mlv4F9cwb",
}


# ---------------------------------------------------------------------------
# Enums - mirrors ConnectorTaskType from the Prisma schema
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
    start:           str          # ISO 8601
    end:             str          # ISO 8601
    meshNodeId:      str
    durationMinutes: int
    shareId:         Optional[str] = None
    taskType:        str = ConnectorTaskType.GENERATE_RDP   # extensibility hook


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
# Config helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_required(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return val


# ---------------------------------------------------------------------------
# MeshCentral helpers
# ---------------------------------------------------------------------------

def _run_meshctrl(cmd: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=r"D:\\mesh",
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
    """Extract the first http(s) URL from a block of text."""
    match = re.search(r"https?://\S+", text)
    return match.group(0).rstrip(".,;)'\"\\") if match else None


def _extract_share_id(text: str) -> Optional[str]:
    """
    Extract share identifier from meshctrl output if present.
    Accepts patterns like:
      ShareID: xxxx
      ID: xxxx
      id: xxxx
    """
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("shareid:"):
            return line.split(":", 1)[1].strip()
        if re.match(r"^id\s*:", line, flags=re.IGNORECASE):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# MeshCentral RDP link generation
# ---------------------------------------------------------------------------

def generate_rdp_link(payload: BookingPayload) -> tuple[Optional[str], Optional[str], str, str]:
    """
    Runs `meshctrl devicesharing` and extracts the RDP URL from its output.

    Returns:
        (rdp_link_or_None, share_id_or_None, stdout, stderr)
    """
    MESHCTRL_PATH = r"D:\\mesh\\node_modules\\meshcentral\\meshctrl.js"

    utc_time = datetime.fromisoformat(payload.start.replace("Z", "+00:00"))
    local_time = utc_time.astimezone()
    start_str = local_time.strftime("%Y-%m-%dT%H:%M:%S")

    cmd = [
        "node",
        MESHCTRL_PATH,
        "devicesharing",
        "--url", "wss://mesh.virtuallabx.com",
        "--loginuser", "admin",
        "--loginpass", "admin",
        "--id", payload.meshNodeId,
        "--add", "guest",
        "--type", "desktop",
        "--start", start_str,
        "--duration", str(payload.durationMinutes),
    ]

    log.info("Running meshctrl for bookingId=%s nodeId=%s", payload.bookingId, payload.meshNodeId)

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

    if share_id:
        log.info("Extracted shareId for bookingId=%s", payload.bookingId)
    else:
        log.warning("No shareId found in meshctrl output for bookingId=%s", payload.bookingId)

    return rdp_link, share_id, stdout, stderr


def revoke_rdp_link(payload: BookingPayload) -> tuple[bool, str, str]:
    """
    Revoke an existing MeshCentral device share using its share identifier.

    Returns:
        (success, stdout, stderr)
    """
    MESHCTRL_PATH = r"D:\\mesh\\node_modules\\meshcentral\\meshctrl.js"

    if not payload.shareId:
        err = "Missing shareId for BOOKING_DELETE task."
        log.error(err)
        return False, "", err

    cmd = [
        "node",
        MESHCTRL_PATH,
        "devicesharing",
        "--url", "wss://mesh.virtuallabx.com",
        "--loginuser", "admin",
        "--loginpass", "admin",
        "--id", payload.meshNodeId,
        "--remove", payload.shareId,
    ]

    log.info(
        "Revoking share for bookingId=%s nodeId=%s shareId=%s",
        payload.bookingId,
        payload.meshNodeId,
        payload.shareId,
    )

    returncode, stdout, stderr = _run_meshctrl(cmd)
    output = stdout + stderr
    log.info("MeshCtrl Revoke Output: %s", output)

    success = returncode == 0 and ("ok" in output.lower() or stderr == "")
    if success:
        log.info("Revoked share for bookingId=%s", payload.bookingId)
    else:
        log.warning("Failed to revoke share for bookingId=%s", payload.bookingId)

    return success, stdout, stderr


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(payload: BookingPayload, rdp_link: str) -> Optional[str]:
    """
    Send the RDP link to the user via SMTP.
    Uses hardcoded SENDER_EMAIL and SENDER_APP_PASS.
    Falls back to env vars SMTP_HOST, SMTP_PORT, SMTP_USE_TLS if set.

    Returns:
        None on success, error string on failure.
    """
    smtp_host = _env("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(_env("SMTP_PORT", "587"))
    use_tls   = _env("SMTP_USE_TLS", "true").lower() not in ("false", "0", "no")

    # Always use the hardcoded sender credentials
    smtp_from = SENDER_EMAIL
    smtp_user = SENDER_EMAIL
    smtp_pass = SENDER_APP_PASS

    subject = f"Your VirtualLab RDP Link - {payload.labName}"

    text_body = f"""\\\\
Hello,

Your remote desktop session for {payload.labName} is ready.

  RDP Link  : {rdp_link}
  Booking ID: {payload.bookingId}
  Start     : {payload.start}
  End       : {payload.end}

Click the link above (or paste it into your RDP client) to connect.
The link will expire at the end of your booking window.

- The VirtualLab Team
"""

    html_body = f"""\\\\
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
    msg["From"]    = smtp_from
    msg["To"]      = payload.userEmail
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if use_tls:
                server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [payload.userEmail], msg.as_string())
        log.info("Email sent to %s for bookingId=%s", payload.userEmail, payload.bookingId)
        return None
    except Exception as exc:
        err = f"Email failed: {exc}"
        log.error(err)
        return err


# ---------------------------------------------------------------------------
# Callback to backend
# ---------------------------------------------------------------------------

def post_result_to_backend(result: ConnectorResult) -> None:
    """
    Optionally POST the result back to the Node.js backend.
    Set BACKEND_CALLBACK_URL to enable.
    """
    url = _env("BACKEND_CALLBACK_URL")
    if not url:
        return

    token   = _env("SECRET_TOKEN")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(url, json=asdict(result), headers=headers, timeout=15)
        resp.raise_for_status()
        log.info("Posted result to backend callback (status %s)", resp.status_code)
    except Exception as exc:
        log.error("Failed to post result to backend: %s", exc)


# ---------------------------------------------------------------------------
# Task dispatcher - extensibility hook for future ConnectorTaskTypes
# ---------------------------------------------------------------------------

def dispatch_task(payload: BookingPayload) -> ConnectorResult:
    """
    Route the task to the appropriate handler based on taskType.
    Currently implements GENERATE_RDP; others are stubs.
    """
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
        return ConnectorResult(
            bookingId=payload.bookingId,
            rdpLink=None,
            success=False,
            error=f"Unknown taskType: {payload.taskType}",
            stdout="",
            stderr="",
            shareId=payload.shareId,
        )


def _handle_generate_rdp(payload: BookingPayload) -> ConnectorResult:
    rdp_link, share_id, stdout, stderr = generate_rdp_link(payload)

    email_error: Optional[str] = None
    if rdp_link:
        email_error = send_email(payload, rdp_link)
    else:
        log.warning("Skipping email - no RDP link generated for bookingId=%s", payload.bookingId)

    success = rdp_link is not None and email_error is None
    error_parts = []
    if not rdp_link:
        error_parts.append("Failed to generate RDP link.")
    if email_error:
        error_parts.append(email_error)

    result = ConnectorResult(
        bookingId=payload.bookingId,
        rdpLink=rdp_link,
        success=success,
        error="; ".join(error_parts) if error_parts else None,
        stdout=stdout,
        stderr=stderr,
        shareId=share_id,
    )

    post_result_to_backend(result)
    return result


def _handle_booking_delete(payload: BookingPayload) -> ConnectorResult:
    success, stdout, stderr = revoke_rdp_link(payload)

    error = None if success else "Failed to revoke RDP link."
    result = ConnectorResult(
        bookingId=payload.bookingId,
        rdpLink=None,
        success=success,
        error=error,
        stdout=stdout,
        stderr=stderr,
        shareId=payload.shareId,
    )

    post_result_to_backend(result)
    return result


def _handle_file_transfer(payload: BookingPayload) -> ConnectorResult:
    # TODO: implement file transfer logic
    log.info("FILE_TRANSFER task received for bookingId=%s (not yet implemented)", payload.bookingId)
    return ConnectorResult(
        bookingId=payload.bookingId,
        rdpLink=None,
        success=False,
        error="FILE_TRANSFER not yet implemented.",
        stdout="",
        stderr="",
        shareId=payload.shareId,
    )


def _handle_status_update(payload: BookingPayload) -> ConnectorResult:
    # TODO: implement status update logic
    log.info("STATUS_UPDATE task received for bookingId=%s (not yet implemented)", payload.bookingId)
    return ConnectorResult(
        bookingId=payload.bookingId,
        rdpLink=None,
        success=False,
        error="STATUS_UPDATE not yet implemented.",
        stdout="",
        stderr="",
        shareId=payload.shareId,
    )


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def parse_payload(data: dict) -> BookingPayload:
    required = ["bookingId", "userId", "userEmail", "labName",
                 "start", "end", "durationMinutes"]
    missing = [f for f in required if f not in data]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    lab_name = data["labName"]

    # Validate lab name and resolve node ID from the hardcoded map
    if lab_name not in LAB_NODE_MAP:
        accepted = ", ".join(f'"{k}"' for k in LAB_NODE_MAP)
        raise ValueError(
            f"Unknown lab name: '{lab_name}'. Accepted labs: {accepted}"
        )

    mesh_node_id = LAB_NODE_MAP[lab_name]
    log.info("Resolved labName='%s' -> meshNodeId='%s'", lab_name, mesh_node_id)

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
        taskType=data.get("taskType", ConnectorTaskType.GENERATE_RDP),
    )


# ---------------------------------------------------------------------------
# CLI entry point
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
# Web service entry point (Flask)
# ---------------------------------------------------------------------------

def run_server(host: str, port: int) -> None:
    try:
        from flask import Flask, request, jsonify  # pip install flask
    except ImportError:
        log.error("Flask not installed. Run: pip install flask")
        sys.exit(1)

    app = Flask("virtuallab.connector")
    secret_token = _env("SECRET_TOKEN")

    def _check_auth() -> Optional[tuple]:
        if not secret_token:
            return None  # auth disabled
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {secret_token}":
            return jsonify({"error": "Unauthorized"}), 401
        return None

    @app.post("/connector/generate-rdp")
    def endpoint_generate_rdp():
        auth_err = _check_auth()
        if auth_err:
            return auth_err
        try:
            payload = parse_payload(request.get_json(force=True))
        except (ValueError, Exception) as exc:
            return jsonify({"error": str(exc)}), 400
        result = dispatch_task(payload)
        return jsonify(asdict(result)), 200 if result.success else 500

    @app.post("/connector/task")
    def endpoint_task():
        """Generic endpoint - taskType in the payload selects the handler."""
        auth_err = _check_auth()
        if auth_err:
            return auth_err
        try:
            payload = parse_payload(request.get_json(force=True))
        except (ValueError, Exception) as exc:
            return jsonify({"error": str(exc)}), 400
        result = dispatch_task(payload)
        return jsonify(asdict(result)), 200 if result.success else 500

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    log.info("Starting VirtualLab Connector Service on %s:%s", host, port)
    app.run(host=host, port=port)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def poll_backend():
    backend_url = os.environ.get("BACKEND_POLL_URL")
    token       = os.environ.get("SECRET_TOKEN")

    if not backend_url:
        raise ValueError("BACKEND_POLL_URL not set")

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    while True:
        try:
            print("Polling backend...")
            resp = requests.get(backend_url, headers=headers, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                print("Response from backend:", data)

                if not data:
                    print("No task available")
                else:
                    print("Task received:", data)
                    log.info("Received task from backend")
                    payload = parse_payload(data)
                    result  = dispatch_task(payload)
                    log.info("Processed bookingId=%s", result.bookingId)

        except Exception as e:
            log.error("Polling error: %s", e)

        time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="VirtualLab MeshCentral Connector - generates RDP links and emails them."
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="Run as a Flask web service instead of a one-shot CLI command."
    )
    parser.add_argument(
        "--json", metavar="JSON",
        help="Booking payload as a JSON string (CLI mode only)."
    )
    parser.add_argument(
        "--host", default=_env("SERVICE_HOST", "0.0.0.0"),
        help="Host for the web service (default: 0.0.0.0)."
    )
    parser.add_argument(
        "--port", type=int, default=int(_env("SERVICE_PORT", "8000")),
        help="Port for the web service (default: 0.0.0.0)."
    )
    args = parser.parse_args()

    if args.serve:
        run_server(args.host, args.port)
    elif args.json:
        run_cli(args)
    else:
        poll_backend()


if __name__ == "__main__":
    main()