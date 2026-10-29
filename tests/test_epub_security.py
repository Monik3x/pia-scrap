from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import urlsplit

import pytest
from curl_cffi import CurlOpt

from src.api import NovelpiaClient


class FakeResponse:
    def __init__(self, status_code=200, content=b"image", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


AUTH_COOKIES = {"USERKEY": "user", "TKEY": "token", "other": "secret"}
SIGNED_COOKIES = {
    "CloudFront-Policy": "fixture-policy",
    "CloudFront-Signature": "fixture-signature",
    "CloudFront-Key-Pair-Id": "fixture-key-id",
}


class FakeSession:
    def __init__(self, responses, expected_url=None):
        self.cookies = {}
        self.responses = list(responses)
        self.expected_url = expected_url

    def get(self, url, **kwargs):
        if self.expected_url is not None and url != self.expected_url:
            return FakeResponse(content=b"wrong URL")
        return self.responses.pop(0)


def make_client(session):
    client = object.__new__(NovelpiaClient)
    client.s = FakeSession([])
    client.s.cookies.update(AUTH_COOKIES)
    client._image_s = session
    client.timeout = 30
    client.cancel_event = None
    client.sleep_cooperative = lambda seconds: None
    return client


@pytest.fixture
def image_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get_all("Cookie", []))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"image")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, received
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def wire_client(monkeypatch, image_server):
    port, received = image_server
    client = NovelpiaClient(userkey="user", tkey="token")
    for name, value in AUTH_COOKIES.items():
        client._image_s.cookies.set(name, value, domain=".novelpia.com")
    get = client._image_s.get
    client._image_s.trust_env = False

    def get_locally(url, **kwargs):
        host = urlsplit(url).hostname
        client._image_s.curl_options[CurlOpt.RESOLVE] = [f"{host}:{port}:127.0.0.1"]
        return get(f"http://{host}:{port}/image.jpg", **kwargs)

    monkeypatch.setattr(client._image_s, "get", get_locally)
    return client, received


def sent_cookies(received):
    assert len(received) == 1
    pairs = []
    for header in received[0]:
        for part in header.split(";"):
            pairs.append(part.strip())
    return sorted(pairs)


def test_image_fetch_blocks_unapproved_hosts_without_request():
    session = FakeSession([])
    result = make_client(session).fetch_image(
        "https://evil.test/image.jpg", "https://global.novelpia.com/"
    )

    assert result is None


@pytest.mark.parametrize("host", ["global.novelpia.com", "images.novelpia.com"])
def test_session_image_fetch_sends_only_session_cookies(host, monkeypatch, image_server):
    client, received = wire_client(monkeypatch, image_server)
    result = client.fetch_image(
        f"https://{host}/image.jpg",
        "https://global.novelpia.com/viewer/1",
        dict(SIGNED_COOKIES),
    )

    assert result == b"image"
    assert sent_cookies(received) == sorted(["USERKEY=user", "TKEY=token"])


@pytest.mark.parametrize(
    "host", ["gn.novelpia.com", "image.novelpia.com", "pv-gn.novelpia.com"]
)
def test_cdn_image_fetch_sends_only_signed_cloudfront_cookies(host, monkeypatch, image_server):
    client, received = wire_client(monkeypatch, image_server)
    result = client.fetch_image(
        f"https://{host}/image.jpg",
        "https://global.novelpia.com/viewer/1",
        {**SIGNED_COOKIES, "unexpected": "secret"},
    )

    assert result == b"image"
    assert sent_cookies(received) == sorted(f"{key}={value}" for key, value in SIGNED_COOKIES.items())


@pytest.mark.parametrize("source", ["d.novelpia.com", "img.novelpia.com"])
def test_fetch_image_requests_canonical_host_for_aliases(source):
    session = FakeSession(
        [FakeResponse()],
        expected_url="https://image.novelpia.com/imagebox/ab/abc.png",
    )
    result = make_client(session).fetch_image(
        f"https://{source}/imagebox/ab/abc.png",
        "https://global.novelpia.com/viewer/1",
        dict(SIGNED_COOKIES),
    )

    assert result == b"image"


def test_image_fetch_does_not_follow_redirect_to_unapproved_host():
    session = FakeSession([
        FakeResponse(302, headers={"Location": "https://evil.test/steal"}),
    ])
    result = make_client(session).fetch_image(
        "https://image.novelpia.com/image.jpg",
        "https://global.novelpia.com/viewer/1",
        dict(SIGNED_COOKIES),
    )

    assert result is None
