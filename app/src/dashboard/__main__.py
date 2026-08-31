"""Entry point: python -m dashboard"""

import argparse
import os
import ssl
import sys

from . import auth, schema, server, storage


def main(argv=None):
    p = argparse.ArgumentParser(prog="dashboard")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to bind. Default 127.0.0.1 (this machine "
                        "only). Anything else exposes an app holding your "
                        "mail, calendar and finances -- put TLS in front.")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--data", default=None,
                   help="where the database lives. Default $DASHBOARD_DATA, "
                        "else ~/.dashboard. Never inside the checkout.")
    p.add_argument("--tls", action="store_true",
                   help="declare that TLS terminates in FRONT of this process "
                        "(a reverse proxy), so cookies are marked Secure and "
                        "HSTS is sent. Use --cert/--key to serve TLS directly.")
    p.add_argument("--cert", help="PEM certificate; serve HTTPS directly")
    p.add_argument("--key", help="PEM private key for --cert")
    p.add_argument("--create-user", metavar="USERNAME",
                   help="create a user and exit; the password is read from "
                        "stdin so it never appears in the process list")
    args = p.parse_args(argv)

    root = storage.configure(storage.resolve_root(args.data))
    conn = storage.get_conn()
    schema.migrate(conn)

    if args.create_user:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            p.error("no password on stdin")
        auth.create_user(conn, args.create_user, password)
        print("created user %s" % args.create_user)
        return 0

    if not conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        print("No users yet. Create one:\n"
              "    echo -n 'your-password' | python -m dashboard --create-user you")

    if bool(args.cert) != bool(args.key):
        p.error("--cert and --key must be given together")

    serving_tls = bool(args.cert)

    ctx = None
    if serving_tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # TLS 1.2 floor: everything below it is broken, and nothing that can
        # reach this needs it.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.load_cert_chain(args.cert, args.key)
        except (OSError, ssl.SSLError) as e:
            p.error("could not load certificate: %s" % e)

    # Either we terminate TLS ourselves, or something in front of us does.
    # Both mean the browser is on HTTPS, which decides whether the session
    # cookie may be marked Secure -- marking it on plain HTTP makes the
    # browser drop the cookie silently and login appears to do nothing.
    httpd, port = server.bind(args.host, args.port, tls_context=ctx,
                              behind_proxy_tls=args.tls)

    secure = serving_tls or args.tls
    scheme = "https" if secure else "http"
    storage.log("started on %s://%s:%d (pid %d)"
                % (scheme, args.host, port, os.getpid()))
    print("Dashboard  ->  %s://%s:%d/" % (scheme, args.host, port))
    print("data: %s" % root)
    if args.host not in ("127.0.0.1", "localhost") and not secure:
        print("\n  ! Bound to %s without --tls. This app uses Web Crypto, which\n"
              "    browsers disable outside a secure context, so it will fail\n"
              "    to load -- and it would be serving your finances in the\n"
              "    clear. Put a certificate in front. See docs/SETUP.md.\n"
              % args.host)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
