"""Serve devdash, or print the token it will accept.

    python3 -m devdash            # serve
    python3 -m devdash --token    # print the shared secret (and the URL to open)
"""

import argparse
import sys

from . import config, server


def main(argv=None):
    cfg = config.load()
    parser = argparse.ArgumentParser(prog="devdash", description=__doc__)
    parser.add_argument("--host", default=None, help="bind address (default: tailnet IP, else 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port (default: %d)" % config.DEFAULT_PORT)
    parser.add_argument("--token", action="store_true", help="print the token and exit")
    args = parser.parse_args(argv)

    host = args.host or config.bind_host(cfg)
    port = args.port or int(cfg.get("port") or config.DEFAULT_PORT)

    if args.token:
        print(cfg["token"])
        return 0

    root = server.web_root(cfg)
    httpd = server.serve(host, port, cfg["token"], root)
    print(f"devdash on http://{host}:{port}/  (frontend: {root})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
