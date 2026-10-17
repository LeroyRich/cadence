"""Bounded downloads of public JPEG and PNG artwork URLs."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import quote, urljoin, urlsplit


MAX_ARTWORK_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, address, port):
        super().__init__(host, port=port, timeout=12)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, port):
        super().__init__(host, port=port, timeout=12, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _public_connection(url):
    if (not isinstance(url, str) or not 1 <= len(url) <= 4096 or
            any(ord(character) < 32 or ord(character) == 127 for character in url)):
        raise ValueError("Enter a valid artwork URL")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
        raise ValueError("Use a public HTTP or HTTPS artwork URL without credentials")
    try:
        host = parsed.hostname.encode("idna").decode("ascii")
        expected_port = 443 if parsed.scheme == "https" else 80
        port = parsed.port if parsed.port is not None else expected_port
    except (UnicodeError, ValueError) as error:
        raise ValueError("Enter a valid artwork URL") from error
    if port != expected_port:
        raise ValueError("Artwork URLs must use a standard HTTP or HTTPS port")
    addresses = [item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    if not addresses:
        raise ValueError("Artwork URL has no reachable address")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise ValueError("Artwork URL has an invalid address") from error
        if parsed_address.version == 6 and parsed_address.ipv4_mapped:
            parsed_address = parsed_address.ipv4_mapped
        if not parsed_address.is_global:
            raise ValueError("Only public artwork URLs are allowed")
    target = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        target += "?" + quote(parsed.query, safe="=&%:@!$'()*+,;/?-._~")
    connection_type = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
    return connection_type(host, addresses[0], port), target


def fetch_public_response(url, max_bytes, headers=None, data=None, allowed_hosts=None):
    seen = set()
    for _ in range(MAX_REDIRECTS + 1):
        if url in seen:
            raise ValueError("URL redirects in a loop")
        seen.add(url)
        if allowed_hosts and urlsplit(url).hostname not in allowed_hosts:
            raise ValueError("Metadata service redirected to an unexpected host")
        try:
            connection, target = _public_connection(url)
            try:
                request_headers = {"User-Agent": "Cadence/0.1", "Accept-Encoding": "identity"}
                request_headers.update(headers or {})
                if data is None:
                    connection.request("GET", target, headers=request_headers)
                else:
                    connection.request("POST", target, body=data, headers=request_headers)
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("URL redirected without a destination")
                    url = urljoin(url, location)
                    if response.status in (301, 302, 303):
                        data = None
                    continue
                if response.status != 200:
                    raise ValueError("Service returned HTTP {}".format(response.status))
                length = response.getheader("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise ValueError("Response is larger than allowed")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise ValueError("Response is larger than allowed")
                return payload, response.getheader("Content-Type") or ""
            finally:
                connection.close()
        except (OSError, http.client.HTTPException) as error:
            raise ValueError("Could not download from service: {}".format(error)) from error
    raise ValueError("URL redirected too many times")


def fetch_artwork_url(url):
    data, _ = fetch_public_response(url, MAX_ARTWORK_BYTES, {"Accept": "image/jpeg,image/png"})
    if data.startswith(b"\xff\xd8\xff"):
        return data, "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "image/png"
    raise ValueError("Artwork URL must provide a JPEG or PNG image")
