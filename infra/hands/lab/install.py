#!/usr/bin/env python3
"""Lab hand on the factory VM (docs/specs/security.md#hands). Idempotent; run as root by update.sh.

For every product whose product.yml lists `lab` under `hands`: build the job image factory-lab:1 (QEMU/KVM, baked
Alpine cloud image, factory-vm-run) and run one act_runner (compose project factory-lab-<org>) registered to that
product's Gitea organisation only, label `lab`. Runners of products that no longer declare `lab` are stopped and
unregistered. Without /dev/kvm on this machine nothing is started (the lab hand then needs a lab machine).
"""
import glob
import hashlib
import json
import os
import subprocess
import sys

import yaml

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "supervisor"))
from factory_supervisor.http import call  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
API = "http://127.0.0.1:3000/api/v1"
SECRETS = "/srv/factory/secrets"
PREFIX = "factory-lab-"
with open(f"{SECRETS}/gitea_ops_token") as f:
    HEADERS = {"Authorization": f"token {f.read().strip()}"}


def run(*args, env=None):
    subprocess.run(args, check=True, env={**os.environ, **(env or {})})


def compose(org, *args):
    with open(os.path.join(HERE, "runner.yaml"), "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()[:16]
    run("docker", "compose", "-p", PREFIX + org, "-f", os.path.join(HERE, "compose.yaml"), *args,
        env={"LAB_ORG": org, "LAB_TOKEN_FILE": f"{SECRETS}/lab_runner_token_{org}", "LAB_CONFIG_SHA": sha})


def main():
    orgs = set()
    for pf in sorted(glob.glob(os.path.join(ROOT, "products", "*", "product.yml"))):
        with open(pf) as f:
            p = yaml.safe_load(f)
        if "lab" in (p.get("hands") or []):
            orgs.add(p["gitea"]["org"])
    out = subprocess.run(["docker", "compose", "ls", "--all", "--format", "json"], check=True, capture_output=True,
                         text=True).stdout
    running = {c["Name"][len(PREFIX):] for c in json.loads(out or "[]") if c["Name"].startswith(PREFIX)}
    for org in sorted(running - orgs):
        compose(org, "down", "-v")
        for r in (call("GET", f"{API}/orgs/{org}/actions/runners", headers=HEADERS) or {}).get("runners", []):
            if r["name"] == f"lab-{org}":
                call("DELETE", f"{API}/orgs/{org}/actions/runners/{r['id']}", headers=HEADERS)
        token = f"{SECRETS}/lab_runner_token_{org}"
        if os.path.exists(token):
            os.remove(token)
        print("lab hand: removed runner of", org)
    if not orgs:
        return
    if not os.path.exists("/dev/kvm"):
        print("lab hand: no /dev/kvm on this machine; products", ", ".join(sorted(orgs)),
              "declare `lab` but need a lab machine", file=sys.stderr)
        return
    run("docker", "build", "-q", "-t", "factory-lab:1", HERE)
    for org in sorted(orgs):
        token = f"{SECRETS}/lab_runner_token_{org}"
        if not os.path.exists(token):
            t = call("POST", f"{API}/orgs/{org}/actions/runners/registration-token", headers=HEADERS)["token"]
            fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(t)
        compose(org, "up", "-d", "--quiet-pull")
        print("lab hand: runner lab-" + org, "up")


if __name__ == "__main__":
    main()
