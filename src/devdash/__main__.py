"""Serve devdash.

    python3 -m devdash

There is no token to print: devdash listens on the tailnet address and nothing
else, and that is the access control.
"""

import argparse
import sys

from . import config, server


def main(argv=None):
    cfg = config.load()
    parser = argparse.ArgumentParser(prog="devdash", description=__doc__)
    parser.add_argument("--host", default=None, help="bind address (default: tailnet IP, else 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help=f"port (default: {config.DEFAULT_PORT})")
    args = parser.parse_args(argv)

    host = args.host or config.bind_host(cfg)
    port = args.port or int(cfg.get("port") or config.DEFAULT_PORT)

    root = server.web_root(cfg)
    httpd = server.serve(host, port, root)
    print(f"devdash on http://{host}:{port}/  (frontend: {root})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
