"""Tiny HTTP CONNECT proxy for routing AmberClaw's yt-dlp traffic through a trusted home IP.

No dependencies. Listens on 127.0.0.1 only; expose it to the server with an SSH reverse
tunnel (see start_home_proxy.bat). Do not expose it to the public internet.
"""

import select
import socket
import threading
import time
from urllib.parse import urlsplit

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8888


def _relay(a, b):
    try:
        while True:
            readable, _, errored = select.select([a, b], [], [a, b], 300)
            if errored:
                return
            for sock in readable:
                other = b if sock is a else a
                try:
                    data = sock.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                other.sendall(data)
    finally:
        for sock in (a, b):
            try:
                sock.close()
            except OSError:
                pass


def _handle(client):
    try:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(8192)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 262144:
                return
        head, rest = buf.split(b"\r\n\r\n", 1)
        parts = head.split(b"\r\n")
        method, target, _ = parts[0].split(b" ", 2)

        if method.upper() == b"CONNECT":
            host, _, port = target.decode().partition(":")
            upstream = socket.create_connection((host, int(port)), timeout=30)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            _relay(client, upstream)
        else:
            url = urlsplit(target.decode())
            upstream = socket.create_connection((url.hostname, url.port or 80), timeout=30)
            path = url.path or "/"
            if url.query:
                path += "?" + url.query
            request = [parts[0].replace(target, path.encode())] + parts[1:]
            upstream.sendall(b"\r\n".join(request) + b"\r\n\r\n" + rest)
            _relay(client, upstream)
    except Exception as exc:  # noqa: BLE001 - proxy must not take down the tunnel
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
        print(f"[{time.strftime('%H:%M:%S')}] error: {exc}", flush=True)
    finally:
        try:
            client.close()
        except OSError:
            pass


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_HOST, LISTEN_PORT))
    server.listen(64)
    print(f"Home proxy listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    while True:
        client, addr = server.accept()
        print(f"[{time.strftime('%H:%M:%S')}] connect {addr[0]}", flush=True)
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
