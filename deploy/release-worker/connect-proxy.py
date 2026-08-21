#!/usr/bin/env python3
"""CONNECT-only proxy for operator-selected approved model endpoints."""

from __future__ import annotations

import ipaddress
import os
import select
import socket
import socketserver

APPROVED_HOSTS = frozenset({
    "api.deepseek.com",
    "inference.do-ai.run",
})
ALLOWED_HOSTS = frozenset(
    host.strip().casefold()
    for host in os.environ.get(
        "PROJECT_HERMES_ALLOWED_MODEL_HOSTS",
        "api.deepseek.com",
    ).split(",")
    if host.strip()
)
if not ALLOWED_HOSTS or not ALLOWED_HOSTS <= APPROVED_HOSTS:
    raise RuntimeError("PROJECT_HERMES_ALLOWED_MODEL_HOSTS is not approved")
ALLOWED_PORT = 443
MAX_HEADER_BYTES = 16 * 1024
MODEL_RESPONSE_IDLE_TIMEOUT_SECONDS = 660


def _log_proxy_event(event: str, **fields: object) -> None:
    """Emit credential-free lifecycle evidence for one model tunnel."""

    details = " ".join(
        f"{name}={value}"
        for name, value in sorted(fields.items())
    )
    print(
        f"PROJECT_HERMES_MODEL_PROXY event={event} {details}".rstrip(),
        flush=True,
    )


def _public_addresses(host: str) -> list[tuple[int, int, int, tuple[object, ...]]]:
    addresses: list[tuple[int, int, int, tuple[object, ...]]] = []
    for family, kind, protocol, _, address in socket.getaddrinfo(
        host,
        ALLOWED_PORT,
        type=socket.SOCK_STREAM,
    ):
        resolved = ipaddress.ip_address(str(address[0]))
        if not resolved.is_global:
            raise RuntimeError("model endpoint resolved to a non-public address")
        addresses.append((family, kind, protocol, address))
    if not addresses:
        raise RuntimeError("model endpoint did not resolve")
    return addresses


def _connect_upstream(host: str) -> socket.socket:
    last_error: OSError | None = None
    for family, kind, protocol, address in _public_addresses(host):
        upstream = socket.socket(family, kind, protocol)
        upstream.settimeout(30)
        try:
            upstream.connect(address)
            upstream.settimeout(None)
            return upstream
        except OSError as exc:
            last_error = exc
            upstream.close()
    assert last_error is not None
    raise last_error


class ConnectHandler(socketserver.BaseRequestHandler):
    """Validate one CONNECT authority and relay opaque TLS bytes."""

    def handle(self) -> None:
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
            if len(header) > MAX_HEADER_BYTES:
                self.request.sendall(b"HTTP/1.1 431 Request Header Too Large\r\n\r\n")
                return
        raw_headers, buffered = bytes(header).split(b"\r\n\r\n", 1)
        lines = raw_headers.split(b"\r\n")
        try:
            method, authority, version = lines[0].decode("ascii").split(" ")
            host, raw_port = authority.rsplit(":", 1)
            port = int(raw_port)
        except (UnicodeDecodeError, ValueError):
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        if (
            method != "CONNECT"
            or version not in {"HTTP/1.0", "HTTP/1.1"}
            or host.casefold() not in ALLOWED_HOSTS
            or port != ALLOWED_PORT
            or any(
                line.lower().startswith(b"proxy-authorization:") for line in lines[1:]
            )
        ):
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            upstream = _connect_upstream(host.casefold())
        except OSError as exc:
            _log_proxy_event(
                "upstream_connect_failed",
                error=type(exc).__name__,
                host=host.casefold(),
            )
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        with upstream:
            self.request.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: project-hermes\r\n\r\n"
            )
            if buffered:
                upstream.sendall(buffered)
            sockets = (self.request, upstream)
            _log_proxy_event(
                "tunnel_opened",
                host=host.casefold(),
                idle_timeout_seconds=MODEL_RESPONSE_IDLE_TIMEOUT_SECONDS,
            )
            while True:
                readable, _, _ = select.select(
                    sockets,
                    (),
                    (),
                    MODEL_RESPONSE_IDLE_TIMEOUT_SECONDS,
                )
                if not readable:
                    _log_proxy_event(
                        "tunnel_idle_timeout",
                        host=host.casefold(),
                        idle_timeout_seconds=(
                            MODEL_RESPONSE_IDLE_TIMEOUT_SECONDS
                        ),
                    )
                    return
                for source in readable:
                    data = source.recv(64 * 1024)
                    if not data:
                        _log_proxy_event(
                            "tunnel_peer_closed",
                            host=host.casefold(),
                            peer=(
                                "worker"
                                if source is self.request
                                else "upstream"
                            ),
                        )
                        return
                    destination = upstream if source is self.request else self.request
                    destination.sendall(data)


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    port = int(os.environ.get("PROJECT_HERMES_PROXY_PORT", "3128"))
    if not 1 <= port <= 65_535:
        raise RuntimeError("PROJECT_HERMES_PROXY_PORT is invalid")
    with ProxyServer(("0.0.0.0", port), ConnectHandler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
