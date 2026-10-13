"""Prompt locally for a provider key; no key in shell history or tool output."""
import argparse
import getpass
import os
import stat
import tempfile
from pathlib import Path


def write_secret(directory, provider):
    directory = Path(directory).resolve(strict=True)
    details = directory.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_mode & 0o077:
        raise ValueError("secret directory must already be private (0700)")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ValueError("secret directory must belong to the current user")
    destination = directory / (provider + "_api_key")
    if destination.is_symlink():
        raise ValueError("refusing a symlink credential file")
    value = getpass.getpass(provider + " API Key (hidden): ").strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("enter one non-empty key token")
    os.umask(0o077)
    descriptor, temporary = tempfile.mkstemp(prefix=".key-", dir=directory)
    path = Path(temporary)
    assert path.resolve().is_relative_to(directory) and path != directory
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        os.replace(path, destination)
    finally:
        if path.exists():
            path.unlink()
    print("Private key file updated; recreate only the SeamPact control service to load it.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("secret_directory")
    parser.add_argument("provider", choices=("deepseek", "glm"))
    args = parser.parse_args()
    write_secret(args.secret_directory, args.provider)
