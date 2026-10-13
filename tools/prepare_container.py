"""Materialize a verified portable release as a credential-free Docker context.

The current context is updated only when previous file hashes still match. No
other directory is moved/deleted and edited/foreign files stop replacement.
"""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path,PurePosixPath


def sha(data):
    return hashlib.sha256(data).hexdigest()


def member_path(root,name):
    relative=PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or ":" in name or "\\" in name:
        raise ValueError("unsafe artifact member")
    target=root.joinpath(*relative.parts).resolve()
    if not target.is_relative_to(root) or target==root:
        raise ValueError("artifact member escapes context")
    return target


def prepare(archive,destination):
    archive,destination=Path(archive).resolve(),Path(destination).resolve()
    if destination==Path(destination.anchor):
        raise ValueError("context cannot be filesystem root")
    with zipfile.ZipFile(archive) as bundle:
        names=bundle.namelist()
        if len(names)!=len(set(names)):
            raise ValueError("duplicate release members")
        prefixes={PurePosixPath(name).parts[0] for name in names}
        if len(prefixes)!=1:raise ValueError("one release directory required")
        prefix=prefixes.pop()
        manifest_bytes=bundle.read(prefix+"/release-manifest.json")
        manifest=json.loads(manifest_bytes)
        hashes=manifest["artifact_hashes"]
        expected={prefix+"/"+name for name in hashes} | {prefix+"/release-manifest.json"}
        if set(names)!=expected:raise ValueError("release contains undeclared members")
        payloads={name:bundle.read(prefix+"/"+name) for name in hashes}
        if any(sha(data)!=hashes[name] for name,data in payloads.items()):
            raise ValueError("release integrity failure")
        payloads["release-manifest.json"]=manifest_bytes
    receipt=destination/"context-evidence.json"
    old=json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else None
    old_hashes=old.get("files",{}) if old else {}
    if old and old.get("schema")!="container-context/1":
        raise ValueError("unrecognized context receipt")
    if destination.exists():
        for current in destination.rglob("*"):
            if current.is_dir():continue
            relative=current.relative_to(destination).as_posix()
            if relative==receipt.name:continue
            expected_hash=old_hashes.get(relative)
            if expected_hash is None or current.is_symlink() or sha(current.read_bytes())!=expected_hash:
                raise ValueError("context contains an edited or foreign file: "+relative)
    # Validate every destination before replacing any file.
    targets={name:member_path(destination,name) for name in payloads}
    stale={name:member_path(destination,name) for name in old_hashes if name not in payloads}
    destination.mkdir(parents=True,exist_ok=True)
    for name,target in stale.items():
        if target.is_file():target.unlink()
    for name,target in targets.items():
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(payloads[name])
    evidence=dict(schema="container-context/1",version=manifest["version"],release_sha256=sha(archive.read_bytes()),
        files={name:sha(data) for name,data in payloads.items()},credentials="EXCLUDED",source_registry="portable verified release")
    receipt.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    return dict(version=manifest["version"],files=len(payloads),release_sha256=evidence["release_sha256"],credentials="EXCLUDED")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("release_zip")
    parser.add_argument("destination")
    args=parser.parse_args()
    print(json.dumps(prepare(args.release_zip,args.destination)))
