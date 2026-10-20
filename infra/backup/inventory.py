#!/usr/bin/env python3
"""What a factory server holds, as JSON, to compare two servers after a restore or a move.
Run as root on a factory server: python3 inventory.py [--until <ISO time>] > inventory.json

- gitea: every repository with its default branch, latest commit, number of issues and pull requests (all states)
- zulip: organisation, streams, the owner account, number of users and messages (messages sent up to --until only,
  when given: the time of the snapshot a restore came from); contact with Zulip's push service, a short hash of the
  server's identity there
- supervisor: whether the state file exists, its task count; service states of broker, supervisor, backup timer
- agents: each agent-* user, model logins stored in its OMP homes, whether it holds a Gitea token
- broker: model logins in its store; tailscale: backend state
Prints no secret (counts and names only).
"""
import argparse
import glob
import json
import os
import pwd
import sqlite3
import subprocess
import urllib.request

GITEA = "http://127.0.0.1:3000/api/v1"
ZULIP_SHELL = r"""
import hashlib, json, os
from datetime import datetime
from django.conf import settings
from zerver.models import Message, Realm, Stream, UserProfile
r = Realm.objects.get(string_id="")
msgs = Message.objects.filter(realm=r)
until = os.environ.get("UNTIL")
if until:
    msgs = msgs.filter(date_sent__lte=datetime.fromisoformat(until.replace("Z", "+00:00")))
owner = UserProfile.objects.filter(realm=r, role=UserProfile.ROLE_REALM_OWNER, is_bot=False, is_active=True)
print("INVENTORY=" + json.dumps({
    "organisation": r.name,
    "streams": sorted(s.name for s in Stream.objects.filter(realm=r, deactivated=False)),
    "owners": [{"email": u.delivery_email, "role": u.role} for u in owner],
    "users": UserProfile.objects.filter(realm=r, is_active=True).count(),
    "messages": msgs.count(),
    "last_message_id": msgs.order_by("-id").values_list("id", flat=True).first(),
    # Contact with Zulip's push service (push.zulipchat.com): none when all three are off.
    "service_push": bool(settings.ZULIP_SERVICE_PUSH_NOTIFICATIONS),
    "service_usage_statistics": bool(settings.ZULIP_SERVICE_SUBMIT_USAGE_STATISTICS),
    "service_security_alerts": bool(settings.ZULIP_SERVICE_SECURITY_ALERTS),
    "push_bouncer_url": settings.PUSH_NOTIFICATION_BOUNCER_URL,
    # The server's identity at that service (registered by the live server): a short hash, never the value.
    "org_id_sha8": hashlib.sha256((settings.ZULIP_ORG_ID or "").encode()).hexdigest()[:8],
}))
"""


def sh(*args, input=None):
    return subprocess.run(args, capture_output=True, text=True, input=input,
                          stdin=None if input is not None else subprocess.DEVNULL)


def gitea():
    with open("/srv/factory/secrets/gitea_ops_token") as f:
        token = f.read().strip()

    def get(path):
        req = urllib.request.Request(f"{GITEA}/{path}", headers={"Authorization": f"token {token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r), r.headers

    repos, page = [], 1
    while True:
        data, _ = get(f"repos/search?limit=50&page={page}&private=true")
        repos += data["data"]
        if len(data["data"]) < 50:
            break
        page += 1
    out = {}
    for r in sorted(repos, key=lambda r: r["full_name"]):
        full, branch = r["full_name"], r["default_branch"]
        try:
            head = get(f"repos/{full}/branches/{branch}")[0]["commit"]["id"][:12]
        except Exception:
            head = None
        counts = {}
        for kind in ("issues", "pulls"):
            _, h = get(f"repos/{full}/issues?state=all&type={kind}&limit=1")
            counts[kind] = int(h.get("X-Total-Count", 0))
        out[full] = {"branch": branch, "head": head, **counts}
    return out


def zulip(until):
    p = sh("docker", "exec", "-i", "-u", "zulip", "-e", f"UNTIL={until or ''}", "factory-zulip-zulip-1",
           "/home/zulip/deployments/current/manage.py", "shell", input=ZULIP_SHELL)
    for line in p.stdout.splitlines():
        if line.startswith("INVENTORY="):
            return json.loads(line[len("INVENTORY="):])
    return {"error": (p.stderr or p.stdout)[-300:]}


def logins(db):
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n = c.execute("select count(*) from auth_credentials").fetchone()[0]
        c.close()
        return n
    except sqlite3.Error:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", help="count Zulip messages sent up to this time (ISO 8601)")
    a = ap.parse_args()
    state = "/var/lib/factory-supervisor/state.json"
    sup: dict = {"state_file": os.path.exists(state)}
    if sup["state_file"]:
        with open(state) as f:
            st = json.load(f)
        sup["tasks"] = len(st.get("tasks", {}))
        sup["chats"] = len(st.get("chats", {}))
        sup["last_zulip_message_seen"] = st.get("last_message_id")
    for unit in ("factory-omp-broker", "factory-supervisor", "factory-backup.timer"):
        sup[unit] = sh("systemctl", "is-active", unit).stdout.strip()
    agents = {}
    for u in sorted(p.pw_name for p in pwd.getpwall() if p.pw_name.startswith("agent-")):
        home = pwd.getpwnam(u).pw_dir
        agents[u] = {"omp_home": os.path.isdir(f"{home}/.omp"),
                     "model_logins": sum(logins(d) for d in glob.glob(f"{home}/.omp/**/agent.db", recursive=True)),
                     "gitea_token": os.path.exists(f"{home}/.gitea-token")}
    broker = glob.glob("/var/lib/omp-broker/.omp/**/agent.db", recursive=True)
    ts = sh("tailscale", "status", "--json")
    print(json.dumps({
        "gitea": gitea(),
        "zulip": zulip(a.until),
        "supervisor": sup,
        "agents": agents,
        "broker_model_logins": sum(logins(d) for d in broker),
        "broker_token_for_agents": os.path.exists("/etc/factory/omp-broker.token"),
        "tailscale": json.loads(ts.stdout).get("BackendState") if ts.returncode == 0 else ts.stderr.strip()[:80],
        "role": open("/var/lib/factory/role").read().strip() if os.path.exists("/var/lib/factory/role") else None,
    }, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
