#!/usr/bin/env python3
"""Provision Gitea and the supervisor's secret files for the factory and every registered product. Idempotent.
Run as root on the factory VM from a checkout of this repository: python3 infra/supervisor/provision.py

Creates (when missing):
- organisation `factory` with private repositories `software-factory` (Gitea copy of this repository, pushed from a
  workstation) and `factory-records` (decision log, settings overrides);
- per product (products/<name>/product.yml): its organisation and repository (with a README and, when the repository
  is created, the default gates workflow .gitea/workflows/gates.yml), the protected default branch (no direct
  pushes), and its agent account (write access to that repository only);
- account `factory-reader` with read access to all of these (chat sessions);
- access tokens: product agents `write:repository`; reader `read:repository,read:issue,read:organization,read:user`;
- /srv/factory/secrets/supervisor/ (root 0700): zulip_api_key, gitea_admin_token, gitea_token_<product>,
  gitea_token_reader — handed to the supervisor service only (systemd LoadCredential).
"""
import base64
import glob
import os
import secrets
import shutil
import subprocess
import sys

import yaml

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "supervisor"))
from factory_supervisor.http import HttpError, call  # noqa: E402

API = "http://127.0.0.1:3000/api/v1"
SECRETS = "/srv/factory/secrets"
SUP = f"{SECRETS}/supervisor"
GITEA_CONTAINER = "factory-gitea-gitea-1"
OWNER_LOGIN = "owner"  # the owner's Gitea account (infra/core/gitea); added to every product organisation
with open(f"{SECRETS}/gitea_ops_token") as f:
    HEADERS = {"Authorization": f"token {f.read().strip()}"}


def g(method, path, body=None):
    return call(method, f"{API}/{path}", headers=HEADERS, json_body=body)


def exists(path):
    try:
        g("GET", path)
        return True
    except HttpError as e:
        if e.status == 404:
            return False
        raise


def org(name, desc):
    if not exists(f"orgs/{name}"):
        g("POST", "orgs", {"username": name, "description": desc, "visibility": "private"})
        print("org", name)
    # The owner sees every product in Gitea: he is in each organisation's Owners team (idempotent).
    owners = next((t["id"] for t in g("GET", f"orgs/{name}/teams") if t["name"] == "Owners"), None)
    if owners is not None and exists(f"users/{OWNER_LOGIN}"):
        g("PUT", f"teams/{owners}/members/{OWNER_LOGIN}")


def repo(owner, name, desc, readme):
    """Create the repository when missing; True if it was created now."""
    if exists(f"repos/{owner}/{name}"):
        return False
    g("POST", f"orgs/{owner}/repos", {"name": name, "description": desc, "private": True, "auto_init": readme,
                                      "default_branch": "main", "readme": "Default" if readme else ""})
    print("repo", f"{owner}/{name}")
    return True


def seed_gates(full, branch):
    """Commit the default gates workflow (infra/gates/default-gates.yml) as .gitea/workflows/gates.yml. Only for a
    repository created now, before its branch is protected; later the product owns its gates."""
    with open(os.path.join(ROOT, "infra", "gates", "default-gates.yml"), "rb") as f:
        content = base64.b64encode(f.read()).decode()
    g("POST", f"repos/{full}/contents/.gitea/workflows/gates.yml",
      {"content": content, "branch": branch, "message": "Factory gates: default workflow (placeholders)"})
    print("gates", full)


def user(login, desc):
    if not exists(f"users/{login}"):
        g("POST", "admin/users", {"username": login, "email": f"{login}@factory.invalid", "full_name": desc,
                                  "password": secrets.token_urlsafe(32), "must_change_password": False,
                                  "visibility": "private"})
        print("user", login)


def collaborator(full, login, perm):
    g("PUT", f"repos/{full}/collaborators/{login}", {"permission": perm})


def protect(full, branch):
    """No direct pushes, and merges only by the factory's admin account (the supervisor's credential, used on the
    owner's `merge`); agents' write tokens cannot merge their own pull requests."""
    rule = {"enable_push": False, "enable_merge_whitelist": True, "merge_whitelist_usernames": [ops_login()]}
    path = f"repos/{full}/branch_protections/{branch}"
    if not exists(path):
        g("POST", f"repos/{full}/branch_protections", {"rule_name": branch, **rule})
        print("protected", f"{full}:{branch}")
        return
    cur = g("GET", path)
    if not cur.get("enable_merge_whitelist") or cur.get("merge_whitelist_usernames") != rule["merge_whitelist_usernames"]:
        g("PATCH", path, rule)
        print("merge whitelist", f"{full}:{branch}")


def ops_login():
    return g("GET", "user")["login"]


def token(login, scopes, path):
    if os.path.exists(path):
        return
    out = subprocess.run(["docker", "exec", "-u", "git", GITEA_CONTAINER, "gitea", "admin", "user",
                          "generate-access-token", "--username", login, "--token-name",
                          f"factory-{os.path.basename(path)}", "--scopes", scopes, "--raw"],
                         check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
    write_secret(path, out.splitlines()[-1].strip())
    print("token", login, scopes)


def write_secret(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")


def main():
    os.makedirs(SUP, mode=0o700, exist_ok=True)
    for src, dst in (("zulip_supervisor_bot_api_key", "zulip_api_key"), ("gitea_ops_token", "gitea_admin_token")):
        shutil.copyfile(f"{SECRETS}/{src}", f"{SUP}/{dst}")
        os.chmod(f"{SUP}/{dst}", 0o600)

    org("factory", "The software factory itself")
    repo("factory", "software-factory", "Gitea copy of the factory's own repository", False)
    repo("factory", "factory-records", "Decision log and settings history, written by the supervisor", True)
    user("factory-reader", "Factory chat (read-only)")
    readable = ["factory/software-factory", "factory/factory-records"]

    for pf in sorted(glob.glob(os.path.join(ROOT, "products", "*", "product.yml"))):
        with open(pf) as f:
            readable.append(product(yaml.safe_load(f)))

    for full in readable:
        collaborator(full, "factory-reader", "read")
    token("factory-reader", "read:repository,read:issue,read:organization,read:user", f"{SUP}/gitea_token_reader")
    print("provisioned; readable by factory-reader:", ", ".join(readable))


def product(p):
    """Organisation, repository (README, default gates workflow, protected default branch), agent account with a
    write token. Returns <org>/<repo>; the caller gives factory-reader read access to it."""
    gi = p["gitea"]
    full = f"{gi['org']}/{gi['repo']}"
    org(gi["org"], p.get("title", p["name"]))
    if repo(gi["org"], gi["repo"], p.get("title", p["name"]), True):
        seed_gates(full, gi["branch"])
    protect(full, gi["branch"])
    user(gi["agent_login"], f"Agents of product {p['name']}")
    collaborator(full, gi["agent_login"], "write")
    token(gi["agent_login"], "write:repository", f"{SUP}/gitea_token_{p['name']}")
    return full


if __name__ == "__main__":
    main()
