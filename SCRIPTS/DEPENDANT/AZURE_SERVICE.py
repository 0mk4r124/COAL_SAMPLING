"""
SCRIPTS/DEPENDANT/AZURE_SERVICE.py

Single Azure integration module:
  - azure_send_mail()            : send mail via Microsoft Graph (with attachments)
  - upload_pdf_to_container()    : upload secured PDF to Azure Blob Storage container
  - upload_image_to_blob()       : upload image to Blob Storage (kept from old code)
  - azure_upload_file()          : chunked OneDrive upload + public link (kept)

SECURITY - ALL credentials come from environment variables. The previous
tenant/client/secret and storage key were exposed in plaintext; ROTATE them:
  - Entra ID -> App registrations -> Certificates & secrets -> new client secret
  - Storage Account -> Access keys -> Rotate

Required env vars (Windows: setx NAME "value", then restart services):
  AZURE_TENANT_ID
  AZURE_CLIENT_ID
  AZURE_CLIENT_SECRET
  AZURE_SENDER_EMAIL              e.g. omkar.kumbhar@insightzz.com
  AZURE_BLOB_CONNECTION_STRING
  AZURE_BLOB_CONTAINER            default: insightzzwhatappcontainer
"""

import os
import base64
import time
import logging

import requests
from azure.identity import ClientSecretCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

logger = logging.getLogger(__name__)

# ---------------- CONFIG (env) ----------------
TENANT_ID     = os.getenv("AZURE_TENANT_ID")
CLIENT_ID     = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
SENDER_EMAIL  = os.getenv("AZURE_SENDER_EMAIL", "omkar.kumbhar@insightzz.com")

ONEDRIVE_FOLDER = "ANODE_BACKUPS"
GRAPH_SCOPE     = "https://graph.microsoft.com/.default"
GRAPH_BASE_URL  = "https://graph.microsoft.com/v1.0"

AZURE_CONNECTION_STRING = os.getenv("AZURE_BLOB_CONNECTION_STRING")
CONTAINER_NAME          = os.getenv("AZURE_BLOB_CONTAINER", "insightzzwhatappcontainer")
PDF_BLOB_PREFIX         = "vehicle_pdfs"
# -----------------------------------------------

_credential       = None
_container_client = None


def _get_credential():
    global _credential
    if _credential is None:
        if not (TENANT_ID and CLIENT_ID and CLIENT_SECRET):
            raise RuntimeError(
                "AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET not set"
            )
        _credential = ClientSecretCredential(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )
    return _credential


def _get_token() -> str:
    return _get_credential().get_token(GRAPH_SCOPE).token


def _get_container_client():
    global _container_client
    if _container_client is None:
        if not AZURE_CONNECTION_STRING:
            raise RuntimeError("AZURE_BLOB_CONNECTION_STRING is not set")
        svc = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        _container_client = svc.get_container_client(CONTAINER_NAME)
    return _container_client


# =====================================================================
# BLOB STORAGE (container) UPLOADS
# =====================================================================

def upload_pdf_to_container(local_pdf_path: str, vehicle_number: str) -> str:
    """
    Upload an already-encrypted vehicle PDF to the blob container.
    Deterministic name per vehicle -> re-runs overwrite, no duplicates.
    Returns the public blob URL.
    """
    if not local_pdf_path or not os.path.exists(local_pdf_path):
        raise FileNotFoundError(f"PDF not found: {local_pdf_path}")

    from DEPENDANT.BLOB_NAMING import blob_name_for
    blob_name = blob_name_for(vehicle_number)

    blob_client = _get_container_client().get_blob_client(blob_name)
    with open(local_pdf_path, "rb") as f:
        blob_client.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(content_type="application/pdf"),
        )

    logger.info(f"[BLOB] Uploaded PDF -> {blob_client.url}")
    return blob_client.url


def upload_image_to_blob(local_image_path: str, record_id: int) -> str:
    """Kept from previous code (defect images etc.)."""
    if not local_image_path or not os.path.exists(local_image_path):
        raise FileNotFoundError(f"Image not found: {local_image_path}")

    filename  = os.path.basename(local_image_path)
    blob_name = f"defects/{record_id}_{filename}"

    blob_client  = _get_container_client().get_blob_client(blob_name)
    content_type = "image/png" if filename.lower().endswith(".png") else "image/jpeg"

    with open(local_image_path, "rb") as f:
        blob_client.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
    return blob_client.url


# =====================================================================
# MICROSOFT GRAPH - MAIL
# =====================================================================

_CONTENT_TYPES = {
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".csv":  "text/csv",
    ".zip": "application/zip",
}


def azure_send_mail(subject: str, body: str, recipients: list[str],
                    attachments: list[str] | None = None) -> bool:
    """
    Send mail via Microsoft Graph. Attachments: list of local file paths.
    Returns True on success, raises on hard failure.
    NOTE: Graph inline attachments are limited to ~3 MB each; compress images
    before attaching (the alert code already uses the *_REDUCED.jpg images).
    """
    token = _get_token()

    graph_attachments = []
    for file_path in attachments or []:
        if not file_path or not os.path.exists(file_path):
            logger.warning(f"[MAIL] Attachment missing, skipping: {file_path}")
            continue

        ext = os.path.splitext(file_path)[1].lower()
        with open(file_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")

        graph_attachments.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": os.path.basename(file_path),
            "contentType": _CONTENT_TYPES.get(ext, "application/octet-stream"),
            "contentBytes": encoded,
        })

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [
                {"emailAddress": {"address": mail}} for mail in recipients
            ],
        },
        "saveToSentItems": True,
    }
    if graph_attachments:
        payload["message"]["attachments"] = graph_attachments

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url  = f"{GRAPH_BASE_URL}/users/{SENDER_EMAIL}/sendMail"
    resp = requests.post(url, headers=headers, json=payload, timeout=180)

    if resp.status_code not in (200, 202):
        raise Exception(f"Mail send failed: {resp.text}")

    logger.info(f"[MAIL] Sent: {subject} -> {recipients}")
    return True


# =====================================================================
# ONEDRIVE (kept from previous code, cleaned)
# =====================================================================

def _create_public_link(item_id: str, token: str) -> str:
    url = f"{GRAPH_BASE_URL}/users/{SENDER_EMAIL}/drive/items/{item_id}/createLink"
    payload = {"type": "view", "scope": "anonymous"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code not in (200, 201):
        raise Exception(f"Public link creation failed: {resp.text}")
    return resp.json()["link"]["webUrl"]


def azure_upload_file(file_path: str, recipients=None) -> str | None:
    """Chunked OneDrive upload -> anonymous view link."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)

    token     = _get_token()
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    session_url = (
        f"{GRAPH_BASE_URL}/users/{SENDER_EMAIL}"
        f"/drive/root:/{ONEDRIVE_FOLDER}/{file_name}:/createUploadSession"
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    session_payload = {
        "item": {"@microsoft.graph.conflictBehavior": "replace", "name": file_name}
    }
    session_resp = requests.post(session_url, headers=headers, json=session_payload, timeout=60)
    if session_resp.status_code not in (200, 201):
        raise Exception(f"Upload session creation failed: {session_resp.text}")

    upload_url = session_resp.json()["uploadUrl"]

    CHUNK_SIZE  = 5 * 1024 * 1024   # 5 MB (must be a multiple of 320 KiB)
    MAX_RETRIES = 3
    bytes_sent  = 0

    with open(file_path, "rb") as f:
        while bytes_sent < file_size:
            f.seek(bytes_sent)
            chunk     = f.read(CHUNK_SIZE)
            chunk_len = len(chunk)
            start, end = bytes_sent, bytes_sent + chunk_len - 1

            chunk_headers = {
                "Content-Length": str(chunk_len),
                "Content-Range": f"bytes {start}-{end}/{file_size}",
            }

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = requests.put(upload_url, headers=chunk_headers,
                                        data=chunk, timeout=(10, 600))
                    if resp.status_code in (200, 201, 202):
                        bytes_sent += chunk_len
                        print(f"[UPLOAD] {round(bytes_sent / file_size * 100, 2)}% completed")
                        break
                    raise Exception(resp.text)
                except Exception as e:
                    print(f"[UPLOAD] Retry {attempt}/{MAX_RETRIES} for bytes {start}-{end}: {e}")
                    if attempt >= MAX_RETRIES:
                        raise
                    time.sleep(3 * attempt)

    # Fetch item metadata (retry while OneDrive indexes)
    item_url = f"{GRAPH_BASE_URL}/users/{SENDER_EMAIL}/drive/root:/{ONEDRIVE_FOLDER}/{file_name}"
    headers  = {"Authorization": f"Bearer {token}"}

    for attempt in range(10):
        resp = requests.get(item_url, headers=headers)
        if resp.status_code == 200:
            return _create_public_link(resp.json()["id"], token)
        print(f"[UPLOAD] Waiting for OneDrive indexing... ({attempt + 1}/10)")
        time.sleep(15)

    print("[UPLOAD] File uploaded but metadata not yet available")
    return None