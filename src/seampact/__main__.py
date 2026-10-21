import argparse
import os
import sys
from pathlib import Path

from .service import Platform
from .server import make_server


def main():
    parser = argparse.ArgumentParser(description="SeamPact: conversation, Harness intake, Units and RL")
    parser.add_argument("command", choices=["serve", "worker"], nargs="?", default="serve")
    parser.add_argument("--home", default="var/prototype")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--public-port", type=int)
    parser.add_argument("--legacy-config", help="复用现有私有模型配置")
    parser.add_argument("--spec", help="分析能力池导出的运行组worker.json")
    parser.add_argument("--coordinator", help="运行进程可访问的平台HTTP地址")
    parser.add_argument("--advertise-url", help="平台可访问的运行进程URL；填写后自动登记")
    parser.add_argument("--instance-id", help="平台启动实例时分配的实例身份")
    parser.add_argument("--generation", type=int, default=0, help="平台分配的实例代次")
    args = parser.parse_args()
    os.umask(0o077)
    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "worker":
        from .deployment import json_request
        from .worker import Worker, make_worker_server
        if not args.spec or not args.coordinator:
            parser.error("worker需要--spec和--coordinator")
        worker = Worker(args.spec, args.coordinator, args.instance_id, args.generation)
        server = make_worker_server(worker, args.host, args.port)
        if args.advertise_url:
            import threading
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                json_request(worker.coordinator + "/api/workers/attach", dict(endpoint=args.advertise_url), timeout=120)
                print("[SeamPact] worker_ready " + worker.instance, flush=True)
                thread.join()
            except KeyboardInterrupt:
                server.shutdown()
        else:
            print("[SeamPact] worker_ready " + worker.instance, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        server.server_close()
        return
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
