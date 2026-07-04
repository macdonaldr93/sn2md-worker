# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "google-api-python-client>=2.150.0",
#   "google-auth>=2.35.0",
# ]
# ///
"""
Verify a service account can (a) list files in a folder shared to it by a
personal Gmail user and (b) receive change notifications for those files
via changes.list.

See scripts/verify/README.md for setup instructions.
"""

from __future__ import annotations

import os
import sys
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
POLL_INTERVAL_SECONDS = 5


def die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> int:
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    folder_id = os.environ.get("SOURCE_FOLDER_ID")
    if not creds_path:
        die("GOOGLE_APPLICATION_CREDENTIALS not set")
    if not folder_id:
        die("SOURCE_FOLDER_ID not set")

    if not os.path.isfile(creds_path):
        die(f"service account key file not found: {creds_path}")

    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    sa_email = getattr(creds, "service_account_email", "<unknown>")
    print(f"authenticated as: {sa_email}")
    print(f"target folder id: {folder_id}\n")

    # 1. list files in the shared folder
    print("[1/3] listing files in the shared folder ...")
    try:
        resp = (
            drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields=("files(id,name,mimeType,md5Checksum,size,modifiedTime)"),
                pageSize=100,
                supportsAllDrives=False,
                includeItemsFromAllDrives=False,
            )
            .execute()
        )
    except HttpError as e:
        die(f"files.list failed: {e}")

    files = resp.get("files", [])
    if not files:
        print(
            "  (folder appears empty from the service account's view — "
            "did you share it to the service account's email?)"
        )
    else:
        for f in files:
            print(
                f"  - {f['name']:<40} "
                f"mime={f['mimeType']:<30} "
                f"md5={f.get('md5Checksum', '-')} "
                f"size={f.get('size', '-')}"
            )
    print(f"  total: {len(files)}\n")

    # 2. fetch a starting page token for the changes feed
    print("[2/3] fetching startPageToken ...")
    try:
        token_resp = drive.changes().getStartPageToken(supportsAllDrives=False).execute()
    except HttpError as e:
        die(f"changes.getStartPageToken failed: {e}")
    page_token = token_resp["startPageToken"]
    print(f"  startPageToken = {page_token}\n")

    # 3. poll changes.list and print any change entries
    print(f"[3/3] polling changes.list every {POLL_INTERVAL_SECONDS}s ...")
    print("      NOW: go modify a file in the shared folder from your personal account.")
    print("      Add / edit / rename / delete — any change is fine.")
    print("      Ctrl-C to stop.\n")

    change_count = 0
    while True:
        try:
            time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print(f"\nstopped. total changes observed: {change_count}")
            return 0 if change_count > 0 else 1

        try:
            resp = (
                drive.changes()
                .list(
                    pageToken=page_token,
                    includeRemoved=True,
                    restrictToMyDrive=False,
                    spaces="drive",
                    fields=(
                        "nextPageToken,newStartPageToken,"
                        "changes(fileId,removed,time,"
                        "file(id,name,md5Checksum,parents,"
                        "mimeType,trashed,modifiedTime))"
                    ),
                    supportsAllDrives=False,
                    pageSize=100,
                )
                .execute()
            )
        except HttpError as e:
            print(f"  changes.list failed: {e}", file=sys.stderr)
            continue

        for c in resp.get("changes", []):
            change_count += 1
            f = c.get("file") or {}
            state = "REMOVED" if c.get("removed") else "CHANGED"
            print(
                f"  [{state}] fileId={c.get('fileId')} "
                f"name={f.get('name', '?')} "
                f"md5={f.get('md5Checksum', '-')} "
                f"time={c.get('time', '-')}"
            )

        if "newStartPageToken" in resp:
            page_token = resp["newStartPageToken"]
        elif "nextPageToken" in resp:
            page_token = resp["nextPageToken"]


if __name__ == "__main__":
    sys.exit(main())
