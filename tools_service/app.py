"""Side-effecting tools behind a small HTTP API.

Kept in its own container so the agent (which holds the API key) never touches
the network on a tool's behalf or the filesystem directly.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

WORKSPACE = Path("/workspace")
NOTE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # rejects "/", "..", etc.
MAX_NOTE_BYTES = 100_000
MAX_FETCH_BYTES = 1_000_000
MAX_REDIRECTS = 3

app = FastAPI()


class Rejected(Exception):
    pass


@app.exception_handler(Rejected)
def _rejected(_, exc: Rejected):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/health")
def health():
    return {"status": "ok"}


# --- notes ---------------------------------------------------------------------

class NoteRef(BaseModel):
    name: str


class NoteWrite(NoteRef):
    content: str


def _note_path(name: str) -> Path:
    if not NOTE_NAME.match(name):
        raise Rejected("invalid note name: use 1-64 letters, digits, '-' or '_'")
    return WORKSPACE / f"{name}.txt"


@app.post("/write_note")
def write_note(body: NoteWrite):
    if len(body.content.encode()) > MAX_NOTE_BYTES:
        raise Rejected(f"note too large (max {MAX_NOTE_BYTES} bytes)")
    _note_path(body.name).write_text(body.content, encoding="utf-8")
    return {"result": f"saved note {body.name!r}"}


@app.post("/read_note")
def read_note(body: NoteRef):
    path = _note_path(body.name)
    if not path.is_file():
        raise Rejected(f"no note named {body.name!r}")
    return {"result": path.read_text(encoding="utf-8")}


# --- fetch_url -----------------------------------------------------------------

class FetchRequest(BaseModel):
    url: str


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _check_url(url: str) -> None:
    """Allow only http(s) to public addresses (blocks localhost, LAN, cloud metadata).

    Note: resolving here and again inside httpx leaves a small DNS-rebinding
    window; fine for a tutorial, worth closing (pin the IP) in production.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise Rejected("only http(s) URLs are allowed")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror:
        raise Rejected(f"cannot resolve host {parsed.hostname!r}")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise Rejected("URL resolves to a non-public address")


@app.post("/fetch_url")
def fetch_url(body: FetchRequest):
    url = body.url
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                _check_url(url)  # re-check every hop, redirects can point inward
                with client.stream("GET", url, headers={"User-Agent": "my-first-agent/1.0"}) as resp:
                    if resp.is_redirect:
                        url = urljoin(url, resp.headers.get("location", ""))
                        continue
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MAX_FETCH_BYTES:
                            break
                    text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                    if resp.status_code >= 400:
                        raise Rejected(f"HTTP {resp.status_code} from {url}")
                    if "html" in resp.headers.get("content-type", ""):
                        extractor = _TextExtractor()
                        extractor.feed(text)
                        text = "\n".join(extractor.parts)
                    return {"result": text}
            raise Rejected("too many redirects")
    except httpx.HTTPError as exc:
        raise Rejected(f"fetch failed: {exc}")
