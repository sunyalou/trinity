"""HTTP client for the Trinity Backend API."""

import sys
from typing import Any, Optional

import httpx

from .config import get_api_key, get_instance_url


class TrinityAPIError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class TrinityClient:
    """Thin HTTP wrapper around the Trinity FastAPI backend."""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = base_url or get_instance_url()
        self.token = token or get_api_key()
        if not self.base_url:
            print("Error: No Trinity instance configured. Run 'trinity init' or 'trinity login' first.", file=sys.stderr)
            sys.exit(1)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _handle_response(self, resp: httpx.Response) -> Any:
        if resp.status_code == 401:
            print("Error: Authentication failed. Run 'trinity login' to re-authenticate.", file=sys.stderr)
            sys.exit(1)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise TrinityAPIError(resp.status_code, str(detail))
        if resp.status_code == 204:
            return None
        return resp.json()

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        with httpx.Client(timeout=30) as c:
            resp = c.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
            return self._handle_response(resp)

    def post(self, path: str, json: Optional[dict] = None) -> Any:
        with httpx.Client(timeout=60) as c:
            resp = c.post(f"{self.base_url}{path}", headers=self._headers(), json=json)
            return self._handle_response(resp)

    def put(self, path: str, json: Optional[dict] = None) -> Any:
        with httpx.Client(timeout=30) as c:
            resp = c.put(f"{self.base_url}{path}", headers=self._headers(), json=json)
            return self._handle_response(resp)

    def delete(self, path: str) -> Any:
        with httpx.Client(timeout=30) as c:
            resp = c.delete(f"{self.base_url}{path}", headers=self._headers())
            return self._handle_response(resp)

    def post_form(self, path: str, data: dict) -> Any:
        """POST with form-encoded body (for OAuth2 token endpoint)."""
        with httpx.Client(timeout=30) as c:
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            resp = c.post(f"{self.base_url}{path}", data=data, headers=headers)
            return self._handle_response(resp)

    def post_unauthenticated(self, path: str, json: Optional[dict] = None) -> Any:
        """POST without auth header (for login/registration flows)."""
        with httpx.Client(timeout=30) as c:
            resp = c.post(
                f"{self.base_url}{path}",
                headers={"Content-Type": "application/json"},
                json=json,
            )
            return self._handle_response(resp)

    def get_unauthenticated(self, path: str) -> Any:
        """GET without auth header."""
        with httpx.Client(timeout=30) as c:
            resp = c.get(f"{self.base_url}{path}")
            return self._handle_response(resp)
