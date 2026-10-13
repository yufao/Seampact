import argparse
import json
import os
import sys
from pathlib import Path

from .service import Platform, load_sources
from .server import make_server


def main():
    parser = argparse.ArgumentParser(description="SeamPact native-owner platform (CPU/stdlib)")
    parser.add_argument("command", choices=["serve", "demo"], nargs="?", default="serve")
    parser.add_argument("--home", default="var")
    parser.add_argument("--sources", default="sources.json" if Path("sources.json").exists() else None)
    parser.add_argument("--port", type=int, default=8874)
    parser.add_argument("--host",choices=["127.0.0.1","0.0.0.0"],default="127.0.0.1",
                        help="explicit container wildcard requires startup authentication")
    parser.add_argument("--public-port",type=int,help="host loopback browser port when container port is mapped")
    parser.add_argument("--legacy-config", help="optional read-only current-user DeepSeek migration directory")
    parser.add_argument("--evidence", help="explicit read-only local probe-report root; evidence never grants admission")
    parser.add_argument("--artifacts", default=os.environ.get("SEAMPACT_API_INPUT_DIR"),
                        help="read-only CRAG API package/record directory for browser task runs")
    args = parser.parse_args()
    os.umask(0o077)
    sys.stdout.reconfigure(encoding="utf-8")
    platform = Platform(args.home, load_sources(args.sources), args.legacy_config,
                        evidence_root=args.evidence, artifact_root=args.artifacts)
    if args.command == "demo":
        import time
        identity = platform.demo()
        while True:
            result = platform.store.get("demo", identity)
            if result["status"] != "RUNNING":
                print(json.dumps(result, ensure_ascii=False, indent=2))
                break
            time.sleep(.05)
        platform.close()
        raise SystemExit(0 if result["status"] == "PASS" else 1)
    server = make_server(platform,args.port,host=args.host,public_port=args.public_port)
    print(f"SeamPact http://127.0.0.1:{server.public_port} · state: {platform.home}", flush=True)
    if server.access_token:
        print(f"本次登录 http://127.0.0.1:{server.public_port}/?access={server.access_token}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        platform.close()


if __name__ == "__main__":
    main()
