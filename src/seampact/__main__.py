import argparse
import os
import sys
from pathlib import Path

def main():
    # The manual worker entry executes the same scoped runtime as the image.
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        from .worker import main as worker_main
        return worker_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description="SeamPact: conversation, Harness intake, Units and RL. Worker options: seampact worker --help")
    parser.add_argument("command", choices=["serve"], nargs="?", default="serve")
    parser.add_argument("--home", default="var/prototype")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--public-port", type=int)
    parser.add_argument("--legacy-config", help="复用现有私有模型配置")
    args = parser.parse_args()
    os.umask(0o077)
    sys.stdout.reconfigure(encoding="utf-8")
    from .service import Platform
    from .server import make_server
    legacy = args.legacy_config
    if not legacy and Path("var/demo/config/providers.json").is_file():
        legacy = "var/demo/config"
    platform = Platform(args.home, legacy=legacy)
    server = make_server(platform, args.port, host=args.host, public_port=args.public_port)
    print(f"SeamPact http://127.0.0.1:{server.public_port} · state: {platform.home}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        platform.close()


if __name__ == "__main__":
    main()
