"""Exercise the real entrypoint, private endpoints and restart. No portal access."""
import json
import subprocess
import time
import uuid

name = "cita-smoke-" + uuid.uuid4().hex[:10]
volume = name + "-data"
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]


def docker(*args, **kwargs):
    return subprocess.run([*DOCKER, *args], check=True, text=True, capture_output=True, **kwargs).stdout


def ready():
    probe = """import urllib.request,urllib.error
try:
 urllib.request.urlopen('http://127.0.0.1:8099/api/status', timeout=2)
 raise SystemExit('Ingress incorrectly accepted loopback')
except urllib.error.HTTPError as e:
 assert e.code == 403
"""
    for _ in range(40):
        if subprocess.run([*DOCKER, "exec", name, "python", "-c", probe], capture_output=True).returncode == 0:
            return
        time.sleep(.5)
    raise RuntimeError("Container did not become ready")


try:
    docker("volume", "create", volume)
    docker("run", "-d", "--name", name, "--memory=1500m", "-v", f"{volume}:/data", "barcelona-cita:test")
    ready()
    info = json.loads(docker("inspect", name))[0]
    assert not info["HostConfig"]["Privileged"] and not info["HostConfig"]["PortBindings"]
    assert info["Config"]["Labels"]["io.hass.type"] == "app"
    # Check the base selected by the actual build, not just the Dockerfile text.
    docker("exec", name, "python", "-c", """from pathlib import Path
release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
assert release['ID'].strip('"') == 'debian'
assert release['VERSION_CODENAME'].strip('"') == 'bookworm'
""")
    # /proc status avoids requiring procps in the image.
    docker("exec", name, "python", "-c", """from pathlib import Path
import stat
from src.storage import Storage
s=Storage(Path('/data'));s.put('smoke_marker', 'persistent');s.close()
assert stat.S_IMODE(Path('/data/cita.sqlite3').stat().st_mode)==0o600
found=False
for p in Path('/proc').glob('[0-9]*/status'):
 try:
  value=p.read_text()
  found=found or ('python' in value.splitlines()[0] and 'Uid:\\t1000\\t1000' in value)
 except OSError:
  pass
assert found
""")
    start = time.monotonic()
    docker("stop", "--time", "40", name)
    stopped = json.loads(docker("inspect", name))[0]["State"]
    assert stopped["ExitCode"] == 0 and not stopped["OOMKilled"]
    assert time.monotonic() - start < 40
    docker("start", name)
    ready()
    docker("exec", name, "python", "-c", "from pathlib import Path; from src.storage import Storage; s=Storage(Path('/data')); assert s.get('smoke_marker')=='persistent'; s.close()")
    docker("stop", "--time", "40", name)
    print("PASS: Debian base, HA image labels, entrypoint, Ingress peer rejection, uid 1000, private storage, SIGTERM, persistent restart")
finally:
    subprocess.run([*DOCKER, "rm", "-f", name], capture_output=True)
    subprocess.run([*DOCKER, "volume", "rm", volume], capture_output=True)
