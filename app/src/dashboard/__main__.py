"""Entry point: python -m dashboard"""

import argparse
import os
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
                   help="declare that TLS terminates in front of this "
                        "process, so cookies are marked Secure and HSTS is sent")
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

    server.Handler.secure = args.tls
    httpd, port = server.bind(args.host, args.port)
    scheme = "https" if args.tls else "http"
    storage.log("started on %s://%s:%d (pid %d)"
                % (scheme, args.host, port, os.getpid()))
    print("Dashboard  ->  %s://%s:%d/" % (scheme, args.host, port))
    print("data: %s" % root)
    if args.host not in ("127.0.0.1", "localhost") and not args.tls:
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
