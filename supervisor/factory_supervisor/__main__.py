"""Entry point: python3 -m factory_supervisor --config supervisor/config.yml

Secrets come from systemd credentials ($CREDENTIALS_DIRECTORY, LoadCredential=sup:/srv/factory/secrets/supervisor),
so they never sit in the environment, the repository or a world-readable file.
"""
import argparse
import glob
import logging
import logging.handlers
import os
import signal
import sys

import yaml

from .core import Supervisor

SITE_FILE = "/etc/factory-supervisor/site.yml"


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # This server's own addresses (zulip.site, gitea.public), written by infra/core/tailnet-serve.sh when the server
    # joins the tailnet; they override config.yml, so a factory installed or restored under another tailnet name
    # links to itself. Only those two keys are taken.
    if os.path.exists(SITE_FILE):
        with open(SITE_FILE) as f:
            site = yaml.safe_load(f) or {}
        for section, key in (("zulip", "site"), ("gitea", "public")):
            value = (site.get(section) or {}).get(key)
            if value:
                cfg[section][key] = value
    root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)), cfg.get("repo_root", "..")))
    cfg["products"] = {}
    for pf in sorted(glob.glob(os.path.join(root, "products", "*", "product.yml"))):
        with open(pf) as f:
            p = yaml.safe_load(f)
        cfg["products"][p["name"]] = p
    if cfg["default_product"] not in cfg["products"]:
        raise SystemExit(f"default product {cfg['default_product']} not registered under products/")
    return cfg


def load_secrets():
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    if not d:
        raise SystemExit("no CREDENTIALS_DIRECTORY: run under systemd with LoadCredential (infra/supervisor)")
    out = {}
    for name in os.listdir(d):
        with open(os.path.join(d, name)) as f:
            out[name.removeprefix("sup_")] = f.read().strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    cfg = load_config(args.config)
    os.makedirs(cfg["log_dir"], exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(os.path.join(cfg["log_dir"], "supervisor.log"),
                                              maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    sup = Supervisor(cfg, load_secrets())

    def stop(signum, frame):
        # Steps in flight stay recorded as in flight; the next start resumes them. The agents are stopped now (sudo
        # passes SIGTERM on to OMP), and the next start stops any that survived.
        sup.stopping = True
        sup.save()
        for sess in list(sup.running.values()):
            if sess is not None:
                sess.p.terminate()
        logging.getLogger("supervisor").info("stopping (signal %s); in-flight steps resume after restart", signum)
        logging.shutdown()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sup.run()


if __name__ == "__main__":
    main()
