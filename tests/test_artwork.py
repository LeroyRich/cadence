import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tagger.artwork import MAX_ARTWORK_BYTES, _public_connection, fetch_artwork_url
from tagger.server import Handler


PNG = b"\x89PNG\r\n\x1a\n" + b"example image"
JPEG = b"\xff\xd8\xff" + b"example image"


class FakeResponse:
    def __init__(self, status, data=b"", headers=None):
        self.status = status
        self.data = data
        self.headers = headers or {}

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, limit):
        return self.data[:limit]


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, method, target, headers):
        self.requests.append((method, target, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ArtworkUrlTests(unittest.TestCase):
    def test_rejects_private_addresses_credentials_and_nonstandard_ports(self):
        with patch("tagger.artwork.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaisesRegex(ValueError, "public artwork"):
                _public_connection("https://localhost/cover.png")
        for url in ("file:///etc/passwd", "https://user:pass@example.com/cover.jpg",
                    "https://example.com:8080/cover.jpg", "https://example.com:0/cover.jpg",
                    "http://example.com:443/cover.jpg", "https://example.com:80/cover.jpg"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _public_connection(url)

    def test_public_address_is_pinned_and_image_bytes_determine_format(self):
        address = "93.184.215.14"
        with patch("tagger.artwork.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]):
            connection, target = _public_connection("https://example.com/art%20cover.png?size=500")
        self.assertEqual((connection.host, connection.address, connection.port),
                         ("example.com", address, 443))
        self.assertEqual(target, "/art%20cover.png?size=500")

        fake = FakeConnection(FakeResponse(200, PNG, {"Content-Length": str(len(PNG))}))
        with patch("tagger.artwork._public_connection", return_value=(fake, "/art.png")):
            self.assertEqual(fetch_artwork_url("https://example.com/art.png"), (PNG, "image/png"))
        self.assertEqual(fake.requests[0][0:2], ("GET", "/art.png"))
        self.assertTrue(fake.closed)

    def test_redirect_to_private_address_is_rejected(self):
        redirect = FakeConnection(FakeResponse(302, headers={"Location": "http://192.168.1.4/cover.jpg"}))

        def addresses(host, port, type):
            address = "192.168.1.4" if host == "192.168.1.4" else "93.184.215.14"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

        with patch("tagger.artwork.socket.getaddrinfo", side_effect=addresses), patch(
                "tagger.artwork._PinnedHTTPSConnection", return_value=redirect):
            with self.assertRaisesRegex(ValueError, "public artwork"):
                fetch_artwork_url("https://example.com/cover.jpg")
        self.assertTrue(redirect.closed)

    def test_rejects_oversize_and_non_image_responses(self):
        responses = ((FakeResponse(200, PNG, {"Content-Length": str(MAX_ARTWORK_BYTES + 1)}), "larger"),
                     (FakeResponse(200, b"not an image"), "JPEG or PNG"),
                     (FakeResponse(404), "HTTP 404"))
        for response, message in responses:
            with self.subTest(message=message), patch("tagger.artwork._public_connection", return_value=(
                    FakeConnection(response), "/cover")):
                with self.assertRaisesRegex(ValueError, message):
                    fetch_artwork_url("https://example.com/cover")
        with patch("tagger.artwork._public_connection", return_value=(
                FakeConnection(FakeResponse(200, JPEG)), "/cover")):
            self.assertEqual(fetch_artwork_url("https://example.com/cover")[1], "image/jpeg")

    def test_url_endpoint_returns_upload_compatible_artwork(self):
        handler = object.__new__(Handler)
        handler.path = "/api/art/url"
        handler.server = SimpleNamespace(app=SimpleNamespace(library=object()))
        handler._session = lambda modify=False: None
        handler._body = lambda: {"url": "https://example.com/cover.png"}
        handler._send_json = lambda value, status=200: (status, value)
        with patch("tagger.server.fetch_artwork_url", return_value=(PNG, "image/png")):
            status, result = handler._handle("POST")
        self.assertEqual(status, 200)
        self.assertEqual(result["mime"], "image/png")
        self.assertIn("data", result)


if __name__ == "__main__":
    unittest.main()
