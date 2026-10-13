"""Allowlisted, credential-free SFTP release plus SHA-256 manifest."""
import argparse
import hashlib
import json
import tempfile
import tomllib
import zipfile
from pathlib import Path

from prepare_sources import prepare


def release(preflight, output, extension=None):
    core=Path(__file__).resolve().parents[1]
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    payloads={}
    for directory in ("src", "tests", "tools", "docs", "deploy"):
        for p in (core/directory).rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and (p.suffix in {".py",".js",".html",".css",".md",".txt",".yaml",".yml"} or p.name=="Dockerfile"):
                payloads[p.relative_to(core).as_posix()]=p.read_bytes()
    for name in ("README.md","pyproject.toml",".env.example",".dockerignore"):
        payloads[name]=(core/name).read_bytes()
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        scratch=Path(temporary).resolve()
        assert scratch.is_relative_to(output) and scratch!=output
        scope=prepare(preflight,Path(temporary)/"sources.json",portable=True,extension=extension)
        for p in Path(temporary).rglob("*"):
            if p.is_file():
                payloads[p.relative_to(temporary).as_posix()]=p.read_bytes()
    evidence=json.loads(Path(preflight).read_text(encoding="utf-8"))
    notices=[]
    for source in evidence["sources"]:
        notices.append(dict(id=source["target_id"],source_url=source["source_url"],revision=source["revision"]))
        root=Path(preflight).resolve().parent/source["checkout"]
        for pattern in ("LICENSE*","NOTICE*"):
            for license_file in root.glob(pattern):
                if license_file.is_file():
                    payloads["sources/"+source["target_id"]+"/"+license_file.name]=license_file.read_bytes()
    payloads["SOURCE-NOTICES.json"]=json.dumps(notices,indent=2).encode()
    if extension:payloads["SOURCE-EXTENSION.json"]=Path(extension).read_bytes()
    hashes={p:hashlib.sha256(data).hexdigest() for p,data in sorted(payloads.items())}
    version=tomllib.loads((core/"pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    manifest=dict(schema="release/1",version=version,artifact_hashes=hashes,
                  runtime_dependencies=[],sources=len(scope),
                  optional_sdk_environments=["docs/requirements-sdk-mmct.txt","docs/requirements-sdk-crag.txt"],
                  coverage="control plane, finite H/R builder, Docker profiles; full native model quality NOT_RUN")
    payloads["release-manifest.json"]=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    archive=output/("seampact-core-"+version+".zip")
    temporary=archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary,"w",compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in payloads.items():
            z.writestr("seampact-core-"+version+"/"+name,data)
    temporary.replace(archive)
    sha=hashlib.sha256(archive.read_bytes()).hexdigest()
    (output/(archive.name+".sha256")).write_text(sha+"  "+archive.name+"\n",encoding="utf-8")
    print(json.dumps(dict(archive=str(archive),sha256=sha,bytes=archive.stat().st_size,files=len(payloads))))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("preflight")
    parser.add_argument("output")
    parser.add_argument("--extension")
    args=parser.parse_args()
    release(args.preflight,args.output,args.extension)
