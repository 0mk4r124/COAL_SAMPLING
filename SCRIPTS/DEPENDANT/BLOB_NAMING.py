"""
SCRIPTS/DEPENDANT/BLOB_NAMING.py

Deterministic blob name + public URL for a vehicle's PDF.
Computable WITHOUT any network call, so the printer can get the URL instantly
while the actual upload happens in the background.

The blob name is an HMAC-SHA256 of the vehicle number keyed with a secret, so
the vehicle number never appears in the URL and the name can't be reproduced
from a vehicle number without the key.

Account name and container are taken from the SAME env vars you already have -
no new Azure vars needed:
  AZURE_BLOB_CONNECTION_STRING   (AccountName is parsed out of this)
  AZURE_BLOB_CONTAINER           e.g. coalsamplingdhar

Only new var required:
  BLOB_NAME_SECRET   key for the HMAC (falls back to VEHICLE_PDF_PASSWORD).
                     Keep it STABLE - changing it renames every blob.
"""

import os
import hmac
import hashlib

PDF_BLOB_PREFIX = "vehicle_pdfs"


def _account_from_conn_str() -> str:
    """
    Parse AccountName=... out of AZURE_BLOB_CONNECTION_STRING.
    Falls back to AZURE_BLOB_ACCOUNT, then to the known account name.
    """
    conn = os.getenv("AZURE_BLOB_CONNECTION_STRING", "")
    for part in conn.split(";"):
        part = part.strip()
        if part.lower().startswith("accountname="):
            return part.split("=", 1)[1].strip()
    return os.getenv("AZURE_BLOB_ACCOUNT", "insightzzblobstorage")


def _container() -> str:
    return os.getenv("AZURE_BLOB_CONTAINER", "coalsamplingdhar")


def blob_name_for(vehicle_number: str) -> str:
    """vehicle_pdfs/<hmac>.pdf  - opaque, deterministic, key-derived."""
    key = (os.getenv("BLOB_NAME_SECRET") or os.getenv("VEHICLE_PDF_PASSWORD") or "").encode("utf-8")
    digest = hmac.new(key, str(vehicle_number).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{PDF_BLOB_PREFIX}/{digest}.pdf"


def blob_url_for(vehicle_number: str) -> str:
    """
    Full public URL for the vehicle's PDF. Valid the moment the blob is
    uploaded to this deterministic name - no network needed to compute it.
    """
    account = _account_from_conn_str()
    return (f"https://{account}.blob.core.windows.net/"
            f"{_container()}/{blob_name_for(vehicle_number)}")