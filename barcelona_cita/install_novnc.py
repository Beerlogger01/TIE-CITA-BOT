"""Install the pinned upstream noVNC release with archive integrity verification."""
import hashlib
import io
import tarfile
import urllib.request
from pathlib import Path

VERSION = "1.7.0"
SHA256 = "b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6"
URL = f"https://codeload.github.com/novnc/noVNC/tar.gz/refs/tags/v{VERSION}"
archive = urllib.request.urlopen(URL, timeout=90).read()
if hashlib.sha256(archive).hexdigest() != SHA256:
    raise SystemExit("noVNC archive hash mismatch")
target = Path("/usr/share/novnc")
target.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
    for member in tar.getmembers():
        parts = Path(member.name).parts
        if len(parts) > 1:
            member.name = str(Path(*parts[1:]))
            tar.extract(member, target, filter="data")
