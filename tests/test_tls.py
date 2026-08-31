"""TLS serving, and the preconnect pattern that browsers actually use.

The bug this guards against: wrapping the *listening* socket makes accept()
perform the handshake inline, so a client that opens a TCP connection without
immediately sending a ClientHello blocks every other connection. Browsers do
this routinely -- Chrome opens sockets speculatively -- with the result that
curl works perfectly and the site never loads in a browser.
"""

import http.client
import shutil
import socket
import ssl
import subprocess
import threading

import pytest

from dashboard import auth, schema, server, storage

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None,
                                reason="openssl needed to make a test certificate")


@pytest.fixture
def certs(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(crt), "-days", "2",
         "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True)
    return str(crt), str(key)


@pytest.fixture
def tls_live(tmp_path, certs):
    crt, key = certs
    storage.configure(str(tmp_path / "data"))
    conn = storage.connect()
    schema.migrate(conn)
    auth.create_user(conn, "king", "hunter2hunter2")
    conn.close()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(crt, key)

    httpd, port = server.bind("127.0.0.1", 0, tls_context=ctx)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port, crt
    httpd.shutdown()
    httpd.server_close()


def client(port, crt):
    ctx = ssl.create_default_context(cafile=crt)
    return http.client.HTTPSConnection("127.0.0.1", port, timeout=8, context=ctx)


def test_serves_over_tls(tls_live):
    port, crt = tls_live
    c = client(port, crt)
    c.request("GET", "/api/health")
    r = c.getresponse()
    assert r.status == 200
    assert b"ok" in r.read()
    c.close()


def test_an_idle_preconnect_does_not_block_other_clients(tls_live):
    """The regression. A socket opened and left silent must not freeze the server."""
    port, crt = tls_live

    # Open TCP and send nothing -- exactly what a browser preconnect does.
    idle = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        # A real client must still be served promptly.
        c = client(port, crt)
        c.request("GET", "/api/health")
        r = c.getresponse()
        assert r.status == 200
        r.read()
        c.close()
    finally:
        idle.close()


def test_several_idle_preconnects_do_not_block(tls_live):
    port, crt = tls_live
    idles = [socket.create_connection(("127.0.0.1", port), timeout=5)
             for _ in range(6)]
    try:
        for _ in range(3):
            c = client(port, crt)
            c.request("GET", "/api/health")
            r = c.getresponse()
            assert r.status == 200
            r.read()
            c.close()
    finally:
        for s in idles:
            s.close()


def test_plain_http_to_a_tls_port_is_dropped_not_fatal(tls_live):
    port, crt = tls_live
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    raw.close()
    # The server must survive someone speaking the wrong protocol at it.
    c = client(port, crt)
    c.request("GET", "/api/health")
    assert c.getresponse().status == 200
    c.close()


def test_cookies_are_marked_secure_over_tls(tls_live):
    port, crt = tls_live
    c = client(port, crt)
    c.request("POST", "/api/auth/login",
              body=b'{"username":"king","password":"hunter2hunter2"}',
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    r.read()
    assert r.status == 200
    assert "Secure" in r.getheader("Set-Cookie")
    assert r.getheader("Strict-Transport-Security")
    c.close()
