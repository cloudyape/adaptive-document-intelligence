"""Microsoft Graph client for Outlook mail (app-only / client-credentials flow).

Azure setup: register an app, add the *application* permission `Mail.Read` (scope it to the
accounts mailbox with an Exchange application access policy), grant admin consent, and create a
client secret. Then set ADI_GRAPH_TENANT_ID / CLIENT_ID / CLIENT_SECRET / MAILBOX.
"""
from __future__ import annotations

import base64
import logging
import time
from collections.abc import Iterator
from datetime import datetime

import httpx
import msal

from app.config import get_settings

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/.default"]


class GraphNotConfigured(RuntimeError):
    pass


class GraphClient:
    def __init__(self) -> None:
        s = get_settings()
        if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_mailbox]):
            raise GraphNotConfigured("set ADI_GRAPH_TENANT_ID, ADI_GRAPH_CLIENT_ID, ADI_GRAPH_CLIENT_SECRET, ADI_GRAPH_MAILBOX")
        self.mailbox = s.graph_mailbox
        self.folder = s.graph_folder
        self._app = msal.ConfidentialClientApplication(
            s.graph_client_id, authority=f"https://login.microsoftonline.com/{s.graph_tenant_id}",
            client_credential=s.graph_client_secret,
        )
        self._http = httpx.Client(timeout=60)

    def _token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=SCOPES)  # msal caches until expiry
        if "access_token" not in result:
            raise RuntimeError(f"Graph auth failed: {result.get('error')}: {result.get('error_description')}")
        return result["access_token"]

    def _get(self, url: str, params: dict | None = None, raw: bool = False):
        for attempt in range(5):
            resp = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._token()}"})
            if resp.status_code in (429, 503, 504):
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("Graph throttled (%s); retrying in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content if raw else resp.json()
        resp.raise_for_status()

    def messages_since(self, since: datetime | None, limit: int) -> Iterator[dict]:
        url = f"{GRAPH}/users/{self.mailbox}/mailFolders/{self.folder}/messages"
        params = {
            "$select": "id,internetMessageId,subject,from,receivedDateTime,hasAttachments,bodyPreview,body",
            "$orderby": "receivedDateTime asc",
            "$top": str(min(limit, 50)),
        }
        if since:
            params["$filter"] = f"receivedDateTime gt {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        count = 0
        while url and count < limit:
            page = self._get(url, params)
            for msg in page.get("value", []):
                yield msg
                count += 1
                if count >= limit:
                    return
            url, params = page.get("@odata.nextLink"), None

    def attachments(self, message_id: str) -> Iterator[dict]:
        """Yields file attachments with decoded `content` bytes."""
        base = f"{GRAPH}/users/{self.mailbox}/messages/{message_id}/attachments"
        page = self._get(base)
        for att in page.get("value", []):
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue  # item/reference attachments (attached emails, OneDrive links) are skipped
            if att.get("contentBytes"):
                content = base64.b64decode(att["contentBytes"])
            else:  # large attachments omit contentBytes
                content = self._get(f"{base}/{att['id']}/$value", raw=True)
            yield {**att, "content": content}
