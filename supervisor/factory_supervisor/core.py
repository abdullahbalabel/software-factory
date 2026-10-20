"""Supervisor core: Zulip events → tasks and chats → OMP sessions → Zulip and Gitea.

Threads: the main thread reads Zulip events and answers commands at once; worker threads (settings `concurrency`,
1 by default) run queued agent steps, one OMP process per step, resumed from the step's session file.
State: Gitea is the record (issues, labels, comments with the session file); a local JSON file adds the job queue,
in-flight steps and topic links, so a restart resumes interrupted steps.
"""
import json
import logging
import os
import re
import secrets as pysecrets
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import commands, prompts
from .gitea_api import STATE_LABELS, Gitea
from .omp_rpc import OmpExited, OmpSession
from .settings import Settings
from .zulip_api import Zulip

log = logging.getLogger("supervisor")

APPROVE_WORDS = {"approve", "approved", "✅"}
REJECT_WORDS = {"reject", "rejected", "❌"}
APPROVE_EMOJI = {"check", "white_check_mark", "heavy_check_mark", "check_mark"}
REJECT_EMOJI = {"x", "cross_mark", "negative_squared_cross_mark"}
MERGE_WORDS = {"merge", "merge it"}
CLOSED = ("done", "merged", "cancelled")  # no agent work left (a done task's pull request can still be merged)
# the words after `merge`: `anyway: <reason>`, `with gate change: <reason>`, `anyway with gate change: <reason>`
MERGE_FORM = re.compile(r"^(anyway)?\s*(with gate change)?\s*:\s*(.*)$", re.I | re.S)
REWORK = re.compile(r"^/?rework\b\s*:?\s*(.*)$", re.I | re.S)  # `rework`, `rework: <note>`, `/rework <note>`
GATE_DIRS = (".gitea/workflows/", ".github/workflows/")  # gate configuration in every product (plus product.yml gates)
KIND_LINE = re.compile(r"^\W*kind\W*:\W*(feature|bugfix|docs|refactor)\b", re.I | re.M)
LOG_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
EXEC = "/usr/local/sbin/factory-agent-exec"
PRODUCT_ADMIN = "/usr/local/sbin/factory-product-admin"
PRODUCT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,18}[a-z0-9]$")
UPLOAD = re.compile(r"\[([^\]]*)\]\((/user_uploads/[^)\s]+)\)")
TEST_CLIENT = "factory-e2e-test"  # Zulip client name of messages posted by infra/supervisor/test/post-as-owner.sh
TEST_MARKS = "/var/lib/factory-test-marks"  # root-only marks written by the test helpers before they act
LONG_TEXT = 1500
ONBOARDING_TOPIC = "onboarding"  # a product's topic for the onboarding questions and its onboarding task
HOWTO_TOPIC = "how to use"  # the usage guide (/help text), posted in #factory and in every product stream


def norm_topic(topic):
    """A topic name as the owner may type it on a phone: case, spaces and a leading `/` or `#` do not matter."""
    return topic.strip().lstrip("/#").strip().lower()


def now_utc():
    return datetime.now(timezone.utc)


def zulip_time(ms):
    return f"<time:{datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}>"


def window_name(wid):
    """'5-hour' for omp usage's window id '5h', '7-day' for '7d'."""
    return {"5h": "5-hour", "7d": "7-day"}.get(wid, wid)


def failed_output(raw):
    """The output of the failed steps in an act_runner job log: from each step's "⭐ Run" line to its
    "❌  Failure" line, without timestamps, colours, the echoed script (::group:: blocks) and git [command] lines;
    the last 40 lines. Without a failure marker: the last 40 lines before the post-run cleanup."""
    lines = [ANSI.sub("", LOG_STAMP.sub("", ln)).rstrip() for ln in raw.splitlines()]
    out, start = [], None
    for i, ln in enumerate(lines):
        if "⭐ Run " in ln:
            start = i
        elif "❌  Failure" in ln and start is not None:
            depth = 0
            for x in lines[start + 1:i + 2]:
                if x.startswith("::group::"):
                    depth += 1
                elif x.startswith("::endgroup::"):
                    depth = max(0, depth - 1)
                elif depth == 0 and not x.startswith(("[command]", "::", "Writing entry to tarball",
                                                      "Extracting content to")):
                    out.append(x)
            start = None
    if not out:
        end = next((i for i, ln in enumerate(lines) if "⭐ Run Post" in ln), len(lines))
        out = [ln for ln in lines[:end] if "🐳" not in ln and not ln.startswith("[command]")]
    return "\n".join(out[-40:])


def short_title(text):
    line = re.sub(r"\s+", " ", text.strip().splitlines()[0] if text.strip() else "request")
    line = re.sub(r"^/request\s+", "", line, flags=re.I)
    return line if len(line) <= 50 else line[:49].rstrip() + "…"


class Supervisor:
    def __init__(self, cfg, secrets):
        self.cfg = cfg
        self.secrets = secrets
        self.z = Zulip(cfg["zulip"]["site"], cfg["zulip"]["bot_email"], secrets["zulip_api_key"])
        self.g = Gitea(cfg["gitea"]["api"], secrets["gitea_admin_token"], cfg["gitea"]["public"])
        self.products = cfg["products"]
        self.factory_stream = cfg["zulip"]["factory_stream"]
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.state = self._load_state()
        self.running = {}  # key -> OmpSession (None while starting)
        self.usage_cache = (0.0, {})
        self.owners = {}
        self.settings = Settings(self)
        self.agent_log = open(os.path.join(cfg["log_dir"], "agents.log"), "ab", buffering=0)
        self.others = set()  # user ids known not to be owners
        self.stopping = False
        self.hands = cfg.get("hands") or {}  # name -> {label, agents} (config.yml)

    # == state ======================================================================================================
    def _load_state(self):
        try:
            with open(self.cfg["state_file"]) as f:
                st = json.load(f)
        except FileNotFoundError:
            st = {}
        for k, v in (("tasks", {}), ("chats", {}), ("queue", []), ("paced", {}), ("hinted", []),
                     ("onboarding", {}), ("last_message_id", 0)):
            st.setdefault(k, v)
        return st

    def save(self):
        with self.lock:
            tmp = self.cfg["state_file"] + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.cfg["state_file"])

    def target(self, key):
        return self.state["chats"][key] if key.startswith("chat:") else self.state["tasks"][key]

    def enqueue(self, job, front=False):
        with self.cv:
            if front:
                self.state["queue"].insert(0, job)
            else:
                self.state["queue"].append(job)
            self.save()
            self.cv.notify_all()

    def task_by_topic(self, stream, topic):
        """The latest task of a topic (a product's topic `onboarding` can hold a cancelled and a newer onboarding)."""
        keys = [k for k, t in self.state["tasks"].items()
                if t["stream"] == stream and norm_topic(t["topic"]) == norm_topic(topic)]
        return max(keys, key=lambda k: self.state["tasks"][k]["number"]) if keys else None

    def repo(self, product):
        p = self.products[product]
        return f"{p['gitea']['org']}/{p['gitea']['repo']}"

    # == hands (runners on other machines; docs/architecture.md#hands) ===============================================
    def product_hands(self, product):
        """{name: spec} of the hands a product declares in product.yml that the factory knows (config.yml)."""
        out = {}
        for h in self.products[product].get("hands") or []:
            if h in self.hands:
                out[h] = self.hands[h]
            else:
                log.warning("product %s declares unknown hand %r (not in config.yml hands)", product, h)
        return out

    def hands_prompt(self, product):
        hands = self.product_hands(product)
        if not hands:
            return prompts.NO_HANDS
        return prompts.HANDS.format(lines="\n".join(
            f"  - `runs-on: {h['label']}`: {' '.join(h['agents'].split())}" for h in hands.values()))

    def hand_lines(self, t, runs):
        """One line per gates job that ran on a hand: hand, job, result, runner."""
        labels = {h["label"]: name for name, h in self.hands.items()}
        lines = []
        for r in runs:
            for j in self.g.run_jobs(self.repo(t["product"]), r["id"]):
                hand = next((labels[lb] for lb in j.get("labels") or [] if lb in labels), None)
                if hand:
                    icon = {"success": "✅", "failure": "❌", "skipped": "⏭"}.get(j.get("conclusion"), "⏳")
                    lines.append(f"🖐 Hand `{hand}`: job `{j['name']}` {icon} {j.get('conclusion') or j.get('status')}"
                                 + (f" on `{j['runner_name']}`" if j.get("runner_name") else ""))
        return lines

    def waiting_for_hands(self, t, runs):
        """Why queued jobs of these runs cannot start: no online runner serves their `runs-on` label."""
        p = self.products[t["product"]]
        online = {lb["name"] for rn in self.g.runners(p["gitea"]["org"])
                  if rn.get("status") == "online" and not rn.get("disabled") for lb in rn.get("labels") or []}
        declared = {h["label"] for h in self.product_hands(t["product"]).values()}
        labels = {h["label"]: name for name, h in self.hands.items()}
        out = []
        for r in runs:
            for j in self.g.run_jobs(self.repo(t["product"]), r["id"]):
                if j.get("status") not in ("queued", "waiting", "blocked"):
                    continue
                for lb in j.get("labels") or []:
                    if lb in online:
                        continue
                    if lb in labels and lb not in declared:
                        out.append(f"job `{j['name']}` needs hand `{labels[lb]}`, which this product does not "
                                   f"declare (product.yml `hands`)")
                    elif lb in labels:
                        out.append(f"job `{j['name']}` waits for hand `{labels[lb]}`: its runner is offline or not "
                                   f"installed")
                    else:
                        out.append(f"job `{j['name']}` has `runs-on: {lb}`, which no runner serves")
        return out

    def say(self, key, text):
        t = self.target(key)
        return self.z.send(t["stream"], t["topic"], text)

    # == agent processes ============================================================================================
    def agent_exec(self, user, args, stdin_text=None, timeout=300):
        # stdin is the given text (or bytes) or /dev/null, never inherited (headless OMP must not wait for input)
        stdin = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
        binary = isinstance(stdin_text, bytes)
        res = subprocess.run(["sudo", "-n", "-u", user, EXEC, *args], capture_output=True, text=not binary,
                             timeout=timeout, **stdin)
        out, err = (res.stdout.decode(), res.stderr.decode()) if binary else (res.stdout, res.stderr)
        if res.returncode != 0:
            raise RuntimeError(f"{user} {args[0]} failed ({res.returncode}): {err.strip()[-400:]}")
        return out

    def spawn(self, key, user, profile, cwd, system_prompt, max_time, tools, flags, session_file):
        # The system prompt goes to OMP as a file in the agent user's home (0600), never on the command line; OMP
        # reads it at start, and the file is removed as soon as the session answers.
        base = ["sudo", "-n", "-u", user, EXEC, "omp", "--mode", "rpc", "--no-ui", "--profile", profile,
                "--cwd", cwd, "--approval-mode", self.cfg["omp"]["approval_mode"], "--max-time", max_time, *flags]
        slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:40] or "session"
        for resume in ([session_file] if session_file else []) + [None]:
            pid = f"{slug}-{pysecrets.token_hex(4)}"
            path = self.agent_exec(user, ["put-prompt", pid], stdin_text=system_prompt).strip().splitlines()[-1]
            argv = base + ["--append-system-prompt", path] + (["--resume", resume] if resume else [])
            sess = OmpSession(argv, key, on_host_tool=lambda name, args: self.host_tool(key, name, args),
                              log_file=self.agent_log)
            try:
                st = sess.request("get_state", timeout=180)
                sess.request("set_host_tools", tools=tools)
                return sess, st["sessionFile"], resume is None
            except Exception as e:
                sess.close()
                if resume is None:
                    raise
                log.warning("%s: resume of %s failed (%s); starting a new session", key, resume, e)
            finally:
                try:
                    self.agent_exec(user, ["rm-prompt", pid], timeout=30)
                except Exception as e:
                    log.warning("%s: prompt file removal failed: %s", key, e)
        raise RuntimeError("unreachable")

    def host_tool(self, key, name, args):
        if name == "post_to_topic":
            self.say(key, "🤖 " + str(args.get("message", ""))[:3000])
            return "posted", False
        if name == "factory_status":
            return self.status_text(None), False
        if name == "create_task" and key.startswith("chat:"):
            product = args.get("product") or self.cfg["default_product"]
            if product not in self.products:
                return f"unknown product {product}; known: {', '.join(self.products)}", True
            c = self.target(key)
            tkey = self.intake(product, args.get("request", ""), c["stream"], c["topic"], None, args.get("title"))
            t = self.state["tasks"][tkey]
            return f"Created task #{t['number']} ({t['issue_url']}); topic {t['stream']} > {t['topic']}", False
        if name in ("set_hands", "add_backlog_item") and not key.startswith("chat:"):
            t = self.target(key)
            if t.get("kind") != "onboarding":
                return f"{name} is only for onboarding", True
            return self.onboarding_tool(key, t, name, args)
        return f"unknown tool {name}", True

    # == usage pacing ===============================================================================================
    def pace_windows(self):
        """(window id, the factory's threshold for it), in the order the pacing guard checks them."""
        return [(self.cfg["omp"]["usage_window"], float(self.settings.get("pace_threshold"))),
                (self.cfg["omp"]["weekly_window"], float(self.settings.get("pace.weekly_threshold")))]

    def usage(self):
        """{window id: (used fraction, reset time in ms)} of the paced windows from `omp usage --json`, cached two
        minutes. A window is missing when `omp usage` failed or does not report it."""
        at, windows = self.usage_cache
        if time.time() - at < 120:
            return windows
        windows = {}
        wanted = {w for w, _ in self.pace_windows()}
        try:
            out = self.agent_exec(self.cfg["chat"]["agent_user"], ["omp", "usage", "--json"], timeout=90)
            data = json.loads(out)
            for rep in data.get("reports", []):
                for lim in rep.get("limits", []):
                    wid = (lim.get("window") or {}).get("id")
                    f = (lim.get("amount") or {}).get("usedFraction")
                    if wid in wanted and f is not None and (wid not in windows or f > windows[wid][0]):
                        windows[wid] = (f, lim["window"].get("resetsAt"))
        except Exception as e:
            log.warning("omp usage failed: %s", e)
        self.usage_cache = (time.time(), windows)
        return windows

    def over_pace(self, windows):
        """The first paced window at or above its threshold: (window id, fraction, reset, threshold), or None."""
        for wid, limit in self.pace_windows():
            if wid in windows and windows[wid][0] >= limit:
                return (wid, *windows[wid], limit)
        return None

    def usage_lines(self, windows):
        """One phrase per paced window: share used, the factory's threshold, reset time."""
        out = []
        for wid, limit in self.pace_windows():
            if wid not in windows:
                out.append(f"{window_name(wid)} window unknown")
                continue
            frac, reset = windows[wid]
            out.append(f"**{frac:.0%}** of the {window_name(wid)} window (factory limit {limit:.0%})"
                       + (f", resets {zulip_time(reset)}" if reset else ""))
        return out

    def pace_hold(self, key, wid, frac, reset, limit):
        if key in self.state["paced"]:
            return
        self.state["paced"][key] = True
        self.save()
        when = zulip_time(reset) if reset else "the window resets"
        self.say(key, f"⏸ Waiting before the next step: model usage is at **{frac:.0%}** of the "
                      f"**{window_name(wid)}** window (factory limit {limit:.0%}). That window resets {when}; the "
                      f"step continues automatically once usage is below the limit.")
        if not key.startswith("chat:"):
            t = self.target(key)
            self.g.set_state(self.repo(t["product"]), t["number"], "paused")

    def pace_release(self, windows):
        for key in list(self.state["paced"]):
            self.state["paced"].pop(key)
            self.save()
            if key not in self.state["tasks"] and key not in self.state["chats"]:
                continue
            self.say(key, "▶ Continuing: model usage is now " + "; ".join(self.usage_lines(windows)) + ".")
            if not key.startswith("chat:"):
                t = self.target(key)
                self.g.set_state(self.repo(t["product"]), t["number"], t["state"])

    # == worker =====================================================================================================
    def next_job(self):
        for job in self.state["queue"]:
            if job["key"] not in self.running:
                return job
        return None

    def worker(self, index):
        while True:
            with self.cv:
                while index >= int(self.settings.get("concurrency")) or self.next_job() is None:
                    self.cv.wait(60)
                job = self.next_job()
                assert job is not None
                self.running[job["key"]] = None
            try:
                windows = self.usage()
                over = self.over_pace(windows)
                if over:
                    reset = over[2]
                    self.pace_hold(job["key"], *over)
                    with self.cv:
                        self.running.pop(job["key"], None)
                        wait = 300 if not reset else min(300, max(60, reset / 1000 - time.time()))
                        self.cv.wait(wait)
                    self.usage_cache = (0.0, {})
                    continue
                if self.state["paced"]:
                    self.pace_release(windows)
                with self.cv:
                    if job not in self.state["queue"]:  # removed meanwhile (/pause, /cancel)
                        self.running.pop(job["key"], None)
                        continue
                    self.state["queue"].remove(job)
                    self.target(job["key"])["inflight"] = job
                    self.save()
                if job["key"].startswith("chat:"):
                    self.run_chat(job)
                else:
                    self.run_task(job)
            except Exception as e:
                log.exception("job %s failed", job)
                try:
                    self.say(job["key"], f"⚠ The factory hit an error running this step: `{str(e)[:300]}`")
                except Exception:
                    pass
            finally:
                with self.cv:
                    self.running.pop(job["key"], None)
                    if not self.stopping:  # on shutdown the step stays in flight and is resumed after the restart
                        try:
                            self.target(job["key"]).pop("inflight", None)
                        except KeyError:
                            pass
                        self.save()
                    self.cv.notify_all()

    # == tasks ======================================================================================================
    def intake(self, product, text, origin_stream, origin_topic, origin_msg_id, title=None, uploads=None):
        p = self.products[product]
        repo = self.repo(product)
        title = short_title(title or text)
        link = self.z.message_link(origin_msg_id) if origin_msg_id else None
        source = f"[Zulip]({link})" if link else f"Zulip {origin_stream} > {origin_topic}"
        body = f"{text}\n\n---\nRequested by the owner in {source}."
        issue = self.g.create_issue(repo, title, body, "planning")
        key = self.register_task(product, issue, title, body, text)
        t = self.state["tasks"][key]
        self.z.send(origin_stream, origin_topic,
                    f"Task **#{t['number']}** created: [{title}]({issue['html_url']}) → "
                    f"#**{p['zulip_stream']}>{t['topic']}**")
        quoted = "\n".join("> " + ln for ln in text.splitlines())
        self.z.send(p["zulip_stream"], t["topic"],
                    f"**Request** (from #**{origin_stream}>{origin_topic}**):\n{quoted}\n\n"
                    f"Record: {issue['html_url']} · Planning now…")
        if uploads:
            self.add_uploads(key, {"stream": origin_stream, "topic": origin_topic, "msg_id": origin_msg_id}, uploads)
        self.enqueue({"key": key, "kind": "plan"})
        return key

    def register_task(self, product, issue, title, body, request, topic=None, state="planning", kind=None):
        """Make an issue a factory task: the topic marker in its body, its state label, the local state entry."""
        p = self.products[product]
        repo = self.repo(product)
        n = issue["number"]
        topic = topic or f"#{n} {title}"[:60]
        self.g.edit_issue(repo, n, body=body + f"\n<!-- factory-task stream={p['zulip_stream']} topic={topic} -->")
        self.g.set_state(repo, n, state)
        key = f"{product}#{n}"
        with self.lock:
            self.state["tasks"][key] = {
                "product": product, "number": n, "title": title, "stream": p["zulip_stream"], "topic": topic,
                "state": state, "issue_url": issue["html_url"], "request": request, "session_file": None,
            }
            if kind:
                self.state["tasks"][key]["kind"] = kind
            self.save()
        return key

    def add_uploads(self, key, ctx, uploads):
        """The owner's files of a task's first message into its workspace; their paths join the task's request."""
        t = self.state["tasks"][key]
        note = self.store_uploads(ctx, self.products[t["product"]]["agent_user"], self.ensure_task_workspace(t),
                                  uploads)
        with self.lock:
            t["request"] += note
            self.save()

    def ensure_task_workspace(self, t):
        p = self.products[t["product"]]
        url = f"{self.cfg['gitea']['api'].rstrip('/')}/{self.repo(t['product'])}.git"
        out = self.agent_exec(p["agent_user"], ["workspace", f"task-{t['number']}", url])
        t["workspace"] = out.strip().splitlines()[-1]
        return t["workspace"]

    def task_prompt(self, t, job, new_session):
        p = self.products[t["product"]]
        fields = {"n": t["number"], "title": t["title"], "product": t["product"], "base": p["gitea"]["branch"],
                  "request": t.get("request", ""), "text": job.get("text", ""), "kind": t.get("kind") or "feature",
                  "attempt": (t.get("gate") or {}).get("fixes", 1),
                  "max_fixes": self.settings.get("gates.max_fixes"), "product_title": p.get("title", t["product"]),
                  "known_hands": ", ".join(sorted(self.hands)) or "none"}
        kind = job["kind"]
        if kind == "resume":
            if not new_session:
                return prompts.RESUME.format(step=job.get("step", t["state"]))
            kind = job.get("step") or ("build" if t["state"] == "building" else "plan")
            if kind in ("forward", "change", "resume"):
                kind = "build" if t["state"] == "building" else "plan"
        if kind == "plan" or (kind == "change" and new_session):
            text = prompts.PLAN.format(**fields)
            return text + ("\n\n" + prompts.CHANGE.format(**fields) if kind == "change" else "")
        if kind == "change":
            return prompts.CHANGE.format(**fields)
        if kind == "build":
            return (prompts.ONBOARD if t.get("kind") == "onboarding" else prompts.BUILD).format(**fields)
        if kind == "fix":
            return prompts.FIX.format(**fields)
        if kind == "rework":
            return prompts.REWORK.format(**{**fields, "text": fields["text"] or "(none)"})
        return prompts.FORWARD.format(**fields)

    def run_task(self, job):
        key = job["key"]
        t = self.state["tasks"][key]
        if t["state"] in CLOSED + ("paused", "checking"):
            return
        p = self.products[t["product"]]
        ws = self.ensure_task_workspace(t)
        step = "build" if t["state"] == "building" else "plan"
        system = prompts.TASK_SYSTEM.format(n=t["number"], title=t["title"], product=t["product"],
                                            repo=self.repo(t["product"]), base=p["gitea"]["branch"],
                                            hands=self.hands_prompt(t["product"]))
        tools = prompts.onboard_tools(self.hands) if t.get("kind") == "onboarding" else prompts.TASK_TOOLS
        sess, session_file, new = self.spawn(key, p["agent_user"], p["omp_profile"], ws, system,
                                             self.settings.get(f"max_time.{step}"), tools,
                                             self.settings.omp_flags(t), t.get("session_file"))
        with self.lock:
            self.running[key] = sess
            if session_file != t.get("session_file"):
                t["session_file"] = session_file
                self.save()
                self.g.comment(self.repo(t["product"]), t["number"],
                               f"Agent session started (profile `{p['omp_profile']}`, user `{p['agent_user']}`)."
                               f"\n<!-- factory-session {session_file} -->")
        try:
            result = sess.prompt(self.task_prompt(t, job, new))
            text = sess.last_text().strip()
        except OmpExited as e:
            if self.stopping or t["state"] in ("paused", "cancelled"):
                return
            self.pause_task(key, f"the agent stopped ({e}; time limit {self.settings.get(f'max_time.{step}')})",
                            {"key": key, "kind": "resume", "step": job["kind"]})
            return
        finally:
            sess.close()
        status = result.get("status")
        if status == "aborted" or t["state"] in ("paused", "cancelled"):
            return
        if status == "error":
            err = result.get("error") or {}
            msg = err.get("message", "unknown error")
            if err.get("httpStatus") == 429 or "usage limit" in msg.lower() or "rate limit" in msg.lower():
                self.usage_cache = (0.0, {})
                self.enqueue({"key": key, "kind": "resume", "step": job["kind"]}, front=True)
                return
            self.pause_task(key, f"the model returned an error: {msg[:300]}",
                            {"key": key, "kind": "resume", "step": job["kind"]})
            return
        self.after_task_turn(key, t, text)

    def post_long(self, key, t, text, prefix=""):
        """Post text in the topic; long text goes to a Gitea comment with a link."""
        body = prefix + text
        if len(body) <= LONG_TEXT:
            return self.say(key, body)
        c = self.g.comment(self.repo(t["product"]), t["number"], body)
        return self.say(key, f"{prefix}{text[:600].rstrip()}…\n\n[Full text]({c['html_url']})")

    def owner_mention(self):
        if not self.owners:
            return ""
        uid, name = next(iter(self.owners.items()))
        return f"@**{name}|{uid}** "

    def after_task_turn(self, key, t, text):
        repo = self.repo(t["product"])
        if t["state"] == "planning":
            m = KIND_LINE.search(text or "")
            c = self.g.comment(repo, t["number"], text or "(empty plan)")
            mid = self.post_long(key, t, text or "(the agent returned an empty plan)")
            ask = self.say(key, f"{self.owner_mention()}Reply `approve`, `change: …` or `reject` "
                                f"(or react ✅ / ❌ on this message). [Plan in Gitea]({c['html_url']})")
            with self.lock:
                t["kind"] = m.group(1).lower() if m else "feature"
                t["plan_msg_ids"] = [mid, ask]
                t["state"] = "awaiting-approval"
                self.save()
            self.g.set_state(repo, t["number"], "awaiting-approval")
        elif t["state"] == "building":
            self.finish_build(key, t, text)
        else:
            self.post_long(key, t, text or "(no reply)")

    def finish_build(self, key, t, text):
        """The agent finished a build or fix turn: open the pull request and wait for the gates on the pushed head."""
        p = self.products[t["product"]]
        repo = self.repo(t["product"])
        branch = f"task/{t['number']}"
        b = self.g.branch(repo, branch)
        if not b:
            ask = ("❓ The agent needs more answers before it can set the product up; reply here." if
                   t.get("kind") == "onboarding" else f"(No branch `{branch}` pushed yet; reply here to guide the "
                                                      f"agent.)")
            self.post_long(key, t, text or "(no reply)", prefix=ask + "\n\n")
            return
        sha = b["commit"]["id"]
        gate = t.get("gate") or {}
        if gate.get("sha") == sha and gate.get("result") == "red":
            self.post_long(key, t, text or "(no reply)",
                           prefix=f"(No new commit on `{branch}`: the gates are still red on `{sha[:8]}`; reply here "
                                  f"to guide the agent.)\n\n")
            return
        note = self.onboarding_note(t) if t.get("kind") == "onboarding" else ""
        pr = self.g.find_pull(repo, branch) or self.g.create_pull(
            repo, branch, p["gitea"]["branch"], f"#{t['number']} {t['title']}",
            f"Closes #{t['number']}\n\n{text}" + (f"\n\n{note}" if note else ""))
        with self.cv:
            t["pr_url"], t["pr_number"] = pr["html_url"], pr["number"]
            t["state"] = "checking"
            t["gate"] = {"sha": sha, "since": time.time(), "fixes": gate.get("fixes", 0)}
            self.save()
            self.cv.notify_all()
        self.g.comment(repo, t["number"], f"Pushed `{sha[:8]}` to `{branch}`. Pull request: {pr['html_url']}. "
                                          f"Gates running.\n\n{text}")
        self.g.set_state(repo, t["number"], "checking")
        self.post_long(key, t, text or "(no summary)",
                       prefix=f"🔎 Pushed `{sha[:8]}` · [pull request]({pr['html_url']}). Gates are running…\n\n")
        if note:
            self.say(key, note)

    # == gates (Gitea Actions) ======================================================================================
    def gate_verdict(self, repo, sha, since=None):
        """('green' | 'red' | 'pending' | 'none', latest run per workflow) for one commit. 'none': no run, and no
        workflow file in the commit (asked only once `since` is 90 s old, so a run has had time to appear)."""
        latest = {}
        for r in self.g.runs(repo, sha):
            if r["path"] not in latest or r["id"] > latest[r["path"]]["id"]:
                latest[r["path"]] = r
        runs = sorted(latest.values(), key=lambda r: r["id"])
        if not runs:
            if since is None or time.time() - since > 90:
                if not any(self.g.has_path(repo, d, sha) for d in (".gitea/workflows", ".github/workflows")):
                    return "none", []
            return "pending", []
        if any(r.get("status") != "completed" for r in runs):
            return "pending", runs
        ok = all(r.get("conclusion") in ("success", "skipped") for r in runs)
        return ("green" if ok else "red"), runs

    def gate_failure(self, repo, runs):
        """The failing jobs' names, failed steps and their output (cleaned, at most ~2,500 characters per job)."""
        parts = []
        for r in runs:
            if r.get("conclusion") in ("success", "skipped"):
                continue
            for j in self.g.run_jobs(repo, r["id"]):
                if j.get("conclusion") in ("success", "skipped"):
                    continue
                steps = [s["name"] for s in j.get("steps") or [] if s.get("conclusion") == "failure"]
                try:
                    raw = self.g.job_log(repo, j["id"])
                except Exception as e:
                    raw = f"(log not readable: {e})"
                parts.append(f"Job `{j['name']}` ({j.get('conclusion')}), failed step(s): "
                             f"{', '.join(steps) or '?'}\n```\n{failed_output(raw)[-2500:]}\n```")
                if len(parts) >= 2:
                    return "\n\n".join(parts)
        return "\n\n".join(parts) or "(no failing job found in the runs)"

    def gates_loop(self):
        while True:
            with self.cv:
                self.cv.wait(15)
                keys = [k for k, t in self.state["tasks"].items() if t["state"] == "checking"]
            for key in keys:
                try:
                    self.check_gates(key)
                except Exception:
                    log.exception("gate check of %s failed", key)

    def check_gates(self, key):
        t = self.state["tasks"].get(key)
        if not t or t["state"] != "checking":
            return
        repo = self.repo(t["product"])
        gate = t.get("gate")
        if not gate:  # rebuilt from Gitea after the local state was lost
            branch = self.g.branch(repo, f"task/{t['number']}")
            pr = self.g.find_pull(repo, f"task/{t['number']}")
            if not branch or not pr:
                return
            with self.lock:
                gate = t["gate"] = {"sha": branch["commit"]["id"], "since": time.time(), "fixes": 0}
                t["pr_url"], t["pr_number"] = pr["html_url"], pr["number"]
                self.save()
        sha = gate["sha"]
        verdict, runs = self.gate_verdict(repo, sha, gate["since"])
        if verdict == "pending":
            limit = self.settings.get("gates.timeout")
            n, unit = int(limit[:-1]), limit[-1]
            if runs and not gate.get("hands_noted") and time.time() - gate["since"] > 120:
                waiting = self.waiting_for_hands(t, runs)
                if waiting:
                    with self.lock:
                        gate["hands_noted"] = True
                        self.save()
                    self.say(key, f"⏸ Gates on `{sha[:8]}`: " + "; ".join(waiting)
                                  + f". They wait up to {limit} (`gates.timeout`), then ask you.")
            if time.time() - gate["since"] > n * (3600 if unit == "h" else 60):
                with self.lock:
                    t["state"] = "building"  # paused from building: /resume asks the agent to check and push again
                    gate["result"] = "timeout"
                    self.save()
                self.say(key, f"{self.owner_mention()}⏳ No gate result for `{sha[:8]}` after {limit} "
                              f"(runner busy or down?).")
                self.pause_task(key, f"the gates gave no result within {limit}", {"key": key, "kind": "fix",
                                "text": "The gates of your last push gave no result in time. Check that the branch "
                                        "is pushed and push again (an empty commit is fine)."})
            return
        links = " · ".join(f"[{r['path'].split('@')[0]}]({r['html_url']})" for r in runs)
        hands = "".join("\n" + line for line in self.hand_lines(t, runs))
        if verdict in ("green", "none"):
            note = (f"✅ **Gates green** on `{sha[:8]}` · {links}{hands}" if verdict == "green" else
                    f"⚠ No gates declared in the repository (no `.gitea/workflows/`); `{sha[:8]}` is unchecked.")
            with self.lock:
                gate["result"] = verdict
                t["state"] = "done"
                t["closed_at"] = time.time()
                self.save()
            self.g.comment(repo, t["number"], note.replace("**", "") + f"\nPull request: {t['pr_url']}")
            self.g.set_state(repo, t["number"], "done")
            try:
                changed = self.gate_changes(t, t["pr_number"]) if verdict == "green" else []
            except Exception as e:
                log.warning("pull request files of %s not read: %s", key, e)
                changed = []
            what = ("accept the onboarding (README and gates into the default branch, the hands into product.yml)"
                    if t.get("kind") == "onboarding" else "merge it into the default branch")
            if verdict != "green":
                ask = ". It cannot be merged from Zulip without green gates."
            elif changed:
                ask = (f". ⚠ It changes the gates ({', '.join(f'`{c}`' for c in changed)}), and gates run from the "
                       f"agent's branch: read the change, then reply `merge with gate change: <reason>` to {what}.")
            else:
                ask = f". Reply `merge` to {what}."
            self.say(key, f"{note}\n\n{self.owner_mention()}**Done.** Pull request: [{t['pr_url']}]({t['pr_url']})"
                          + ask)
            try:  # the work is on the pushed branch; the workspace ends with the task (omp-runtime.md)
                self.agent_exec(self.products[t["product"]]["agent_user"], ["rm-workspace", f"task-{t['number']}"])
            except Exception as e:
                log.warning("workspace removal failed: %s", e)
            return
        excerpt = self.gate_failure(repo, runs)
        max_fixes = int(self.settings.get("gates.max_fixes"))
        c = self.g.comment(repo, t["number"], f"❌ Gates red on `{sha[:8]}` ({links}).{hands}\n\n{excerpt}")
        with self.lock:
            gate["result"] = "red"
            t["state"] = "building"
            fix = gate["fixes"] < max_fixes
            if fix:
                gate["fixes"] += 1
            self.save()
        self.g.set_state(repo, t["number"], "building")
        job = {"key": key, "kind": "fix", "text": excerpt}
        if fix:
            self.say(key, f"❌ **Gates red** on `{sha[:8]}` · {links} · [failing log]({c['html_url']}){hands}\n"
                          f"🔧 The agent is fixing it (attempt {gate['fixes']} of {max_fixes}).")
            self.enqueue(job)
        else:
            self.say(key, f"❌ **Gates red** on `{sha[:8]}` · {links} · [failing log]({c['html_url']}){hands}\n"
                          f"{self.owner_mention()}Still red after {max_fixes} fix attempt(s). Reply with guidance "
                          f"(it goes to the agent), then `/resume` for one more attempt; `merge anyway: <reason>` to "
                          f"merge it red; or `/cancel`.")
            self.pause_task(key, f"the gates are still red after {max_fixes} fix attempt(s)", job)

    # == release playbook: merge ====================================================================================
    def cmd_merge(self, ctx, args):
        """`merge`; `merge with gate change: <reason>` (the pull request changes the gates); `merge anyway: <reason>`
        (red, missing or unfinished gates; D17); `merge anyway with gate change: <reason>` (both)."""
        key = ctx["key"]
        t = self.state["tasks"][key]
        repo = self.repo(t["product"])
        m = MERGE_FORM.match(args.strip())
        if args.strip() and (not m or not (m.group(1) or m.group(2))):
            self.reply(ctx, "Usage: " + commands.find("/merge")["usage"])
            return
        anyway, gate_ok = bool(m and m.group(1)), bool(m and m.group(2))
        reason = " ".join((m.group(3) if m else "").split())
        if (anyway or gate_ok) and not reason:
            self.refuse_merge(ctx, t, "a reason is required after the colon (it goes into the decision log)")
            return
        if t["state"] == "merged":
            self.reply(ctx, f"Already merged: [pull request]({t.get('pr_url')}).")
            return
        if not t.get("pr_number"):
            self.reply(ctx, f"Nothing to merge yet: no pull request (state: {t['state']}).")
            return
        if anyway and (key in self.running or t["state"] == "cancelled"):
            self.refuse_merge(ctx, t, "the task is cancelled" if t["state"] == "cancelled" else
                              "the agent is working on it now; `/pause` first")
            return
        if not anyway and t["state"] != "done":
            why = {"checking": "the gates are still running", "building": "the agent is still working on it",
                   "paused": f"the task is paused ({t.get('pause_reason', '')})"}.get(t["state"], f"state {t['state']}")
            if (t.get("gate") or {}).get("result") in ("red", "timeout") and key not in self.running:
                why += "; to ship it as it is, reply `merge anyway: <reason>`"
            self.refuse_merge(ctx, t, why)
            return
        pr = self.g.pull(repo, t["pr_number"])
        if pr.get("merged"):
            self.mark_merged(ctx, t, pr, pr.get("merge_commit_sha") or "", via="already merged in Gitea")
            return
        if pr["state"] != "open":
            self.refuse_merge(ctx, t, "the pull request is closed")
            return
        sha = pr["head"]["sha"]
        verdict, runs = self.gate_verdict(repo, sha)
        links = " · ".join(f"[{r['path'].split('@')[0]}]({r['html_url']})" for r in runs)
        if verdict != "green" and not anyway:
            why = {"red": f"gates are **red** on the head `{sha[:8]}` ({links})",
                   "pending": f"gates are still running on the head `{sha[:8]}`",
                   "none": f"there are no gate results for the head `{sha[:8]}`"}[verdict]
            self.refuse_merge(ctx, t, why + ("; to ship it as it is, reply `merge anyway: <reason>`"
                                             if verdict != "pending" else ""))
            return
        if pr.get("mergeable") is False:
            self.refuse_merge(ctx, t, f"the pull request conflicts with `{pr['base']['ref']}`; reply `rework` (or "
                                      f"`rework: <note>`) to send it back to the agent to update the branch")
            return
        changed = self.gate_changes(t, t["pr_number"])
        if changed and not gate_ok:
            self.refuse_merge(ctx, t, self.gate_change_note(changed, anyway))
            return
        gates = (f"gates green on {sha[:8]}" if verdict == "green" else
                 f"gates **{verdict}** on {sha[:8]}" + (f" ({links})" if links else "")
                 + f", merged anyway by the owner: {reason}")
        if changed:
            gates += f"; changes the gates ({', '.join(changed)}), accepted by the owner: {reason}"
        with self.cv:  # a red task may be paused with a fix queued or held: nothing more goes to the agent
            self.state["queue"] = [j for j in self.state["queue"] if j["key"] != key]
            t["held"] = []
            self.save()
        res = self.g.merge_pull(repo, t["pr_number"], sha, f"{pr['title']} (#{t['pr_number']})",
                                f"Merged by the factory on the owner's `merge` ({ctx['who']}, {ctx['link']}); "
                                f"{gates.replace('**', '')}.")
        merged = self.g.pull(repo, t["pr_number"])
        if not merged.get("merged"):
            self.refuse_merge(ctx, t, f"Gitea did not merge it ({str(res)[:200]})")
            return
        self.mark_merged(ctx, t, merged, merged.get("merge_commit_sha") or "", via=gates.replace("**", ""),
                         extra=(f"\n⚠ Merged with **{verdict}** gates on `{sha[:8]}`" + (f" ({links})" if links else "")
                                + f", on your `merge anyway`: {reason}" if verdict != "green" else ""))

    def cmd_rework(self, ctx, note):
        """Send a task with a pull request back to its agent (D17): update the branch against the default branch
        (merge it in, resolve conflicts) plus the owner's note; the gates run again and `merge` is asked again."""
        key = ctx["key"]
        t = self.state["tasks"][key]
        if t["state"] in ("merged", "cancelled"):
            self.reply(ctx, f"Nothing to rework: the task is {t['state']}. Post a new request in topic `requests`.")
            return
        if not t.get("pr_number"):
            self.reply(ctx, "No pull request yet: write to the agent here instead.")
            return
        if t["state"] not in ("done", "paused") or key in self.running:
            self.reply(ctx, f"The task is {t['state']}" + (" and its agent is working" if key in self.running else "")
                       + "; `rework` is for a finished (`done`) or paused task.")
            return
        repo = self.repo(t["product"])
        with self.cv:
            self.state["queue"] = [j for j in self.state["queue"] if j["key"] != key]
            for k in ("held", "prev_state", "pause_reason", "closed_at"):
                t.pop(k, None)
            t["state"] = "building"
            if t.get("gate"):
                t["gate"]["fixes"] = 0
            self.save()
        self.g.set_state(repo, t["number"], "building")
        self.g.comment(repo, t["number"], f"Rework requested by {ctx['who']} ([Zulip]({ctx['link']})): update the "
                                          f"branch against the default branch" + (f"; note: {note}" if note else "."))
        self.record(f"Task {key}: rework" + (f" ({note})" if note else ""), ctx)
        self.say(key, f"🔁 Rework: the agent updates `task/{t['number']}` against "
                      f"`{self.products[t['product']]['gitea']['branch']}`" + (" and your note" if note else "")
                      + "; the gates run again, then `merge` is asked again.")
        self.enqueue({"key": key, "kind": "rework", "text": note})

    def gate_changes(self, t, pr_number):
        """Files of the pull request that are gate configuration: the workflow directories, and the paths the
        product's product.yml lists under `gates`. Gates run from the pushed branch, so a branch that edits them
        can make itself green; merging such a change needs the owner's explicit `merge with gate change: …`."""
        declared = [g.rstrip("/") for g in self.products[t["product"]].get("gates") or []]
        out = []
        for f in self.g.pull_files(self.repo(t["product"]), pr_number):
            names = {f.get("filename") or "", f.get("previous_filename") or ""} - {""}
            if any(n.startswith(GATE_DIRS) or any(n == d or n.startswith(d + "/") for d in declared) for n in names):
                out.append(f.get("filename") or f.get("previous_filename"))
        return out

    @staticmethod
    def gate_change_note(changed, anyway=False):
        form = "merge anyway with gate change: <reason>" if anyway else "merge with gate change: <reason>"
        return (f"this pull request changes the gates ({', '.join(f'`{c}`' for c in changed)}). Gates run from the "
                f"agent's branch, so green does not show the change is safe: read it in the pull request, then reply "
                f"`{form}` (recorded in the decision log)")

    def refuse_merge(self, ctx, t, why):
        self.record(f"Task {ctx['key']}: merge refused ({why.replace('**', '')})", ctx)
        self.reply(ctx, f"⛔ Not merged: {why}.")

    def mark_merged(self, ctx, t, pr, merge_sha, via, extra=""):
        repo = self.repo(t["product"])
        with self.lock:
            t["state"] = "merged"
            t["merge_sha"] = merge_sha
            t["closed_at"] = time.time()
            self.save()
        self.g.set_state(repo, t["number"], "merged")
        self.g.comment(repo, t["number"], f"Pull request {pr['html_url']} **merged** into `{pr['base']['ref']}` "
                                          f"(`{merge_sha[:8]}`) by the factory on {ctx['who']}'s `merge` "
                                          f"([Zulip]({ctx['link']})); {via}.")
        try:
            self.g.edit_issue(repo, t["number"], state="closed")
        except Exception as e:
            log.warning("closing issue failed: %s", e)
        self.record(f"Task {ctx['key']}: merged [PR #{pr['number']}]({pr['html_url']}) into {pr['base']['ref']} "
                    f"({merge_sha[:8]}; {via})", ctx)
        self.reply(ctx, f"🚀 **Merged** into `{pr['base']['ref']}` (`{merge_sha[:8]}`): [pull request]"
                        f"({pr['html_url']}). Recorded in the decision log.{extra}")
        try:  # gone already when the gates turned green; still there after `merge anyway` from a paused task
            self.agent_exec(self.products[t["product"]]["agent_user"], ["rm-workspace", f"task-{t['number']}"])
        except Exception as e:
            log.warning("workspace removal failed: %s", e)
        if t.get("kind") == "onboarding":
            self.onboarding_accepted(ctx, t)

    # == onboarding (skills/factory-onboarding; docs/specs/products.md#onboarding) ==================================
    def ask_onboarding(self, name):
        """Post the questionnaire in the product's topic `onboarding`; the owner's answer there starts the task."""
        p = self.products[name]
        with self.lock:
            self.state["onboarding"][name] = "asked"
            self.save()
        self.z.send(p["zulip_stream"], ONBOARDING_TOPIC, prompts.ONBOARD_QUESTIONS.format(
            mention=self.owner_mention(), name=name, title=p.get("title", name)))
        self.post_howto(p["zulip_stream"])

    def post_howto(self, stream):
        """The usage guide (the /help text) as topic `how to use` in a stream, so it is one tap away on the phone."""
        self.z.send(stream, HOWTO_TOPIC, self.help())

    def onboarding_task(self, name, open_only=False):
        """The product's latest onboarding task key, or None."""
        keys = [k for k, t in self.state["tasks"].items() if t["product"] == name and t.get("kind") == "onboarding"
                and not (open_only and t["state"] in CLOSED)]
        return max(keys, key=lambda k: self.state["tasks"][k]["number"]) if keys else None

    def onboarding_start(self, ctx, text, uploads):
        """The owner's answers in topic `onboarding`: an onboarding task (issue, state building, no plan step: the
        answers are the brief and the pull request is what the owner approves with `merge`)."""
        name = ctx["product"]
        repo = self.repo(name)
        body = (f"{text}\n\n---\nOnboarding of product `{name}`: the owner's answers to the onboarding questions, in "
                f"[Zulip]({ctx['link']}). The agent prepares README, gates, hands and 3-5 backlog issues; the owner "
                f"accepts with `merge with gate change: <reason>`.")
        issue = self.g.create_issue(repo, "Onboarding", body, "building")
        key = self.register_task(name, issue, "Onboarding", body, text, topic=ctx["topic"], state="building",
                                 kind="onboarding")
        with self.lock:
            self.state["onboarding"][name] = "running"
            self.save()
        self.record(f"Product `{name}`: onboarding answers given (task {key})", ctx)
        self.z.react(ctx["msg_id"], "eyes")
        self.reply(ctx, f"Onboarding task **#{issue['number']}**: [record]({issue['html_url']}). The agent is preparing "
                        f"the README, the gates, the hands and the backlog; the pull request comes here.")
        if uploads:
            self.add_uploads(key, ctx, uploads)
        self.enqueue({"key": key, "kind": "build"})

    def onboarding_tool(self, key, t, name, args):
        repo = self.repo(t["product"])
        if name == "set_hands":
            hands = list(dict.fromkeys(args.get("hands") or []))
            unknown = [h for h in hands if h not in self.hands]
            if unknown:
                return f"unknown hand(s) {', '.join(unknown)}; known: {', '.join(sorted(self.hands))}", True
            with self.lock:
                t["hands"] = hands
                self.save()
            return (f"Recorded hands {hands or 'none'}; written to product.yml when the owner merges the onboarding "
                    f"pull request."), False
        title = " ".join(str(args.get("title", "")).split())[:60]
        body = str(args.get("body", "")).strip()
        if not title or not body:
            return "title and body are required", True
        backlog = t.setdefault("backlog", [])
        same = next((b for b in backlog if b["title"].lower() == title.lower()), None)
        if same:
            return f"already created: #{same['number']}", False
        if len(backlog) >= 5:
            return "the backlog already has 5 items (the most onboarding creates)", True
        stream = self.products[t["product"]]["zulip_stream"]
        issue = self.g.create_backlog_issue(repo, title, f"{body}\n\n---\nBacklog item from onboarding (#{t['number']}"
                                                         f"). Not started: `/backlog start <n>` in #{stream} starts it.")
        with self.lock:
            backlog.append({"number": issue["number"], "title": title, "url": issue["html_url"]})
            self.save()
        return f"created backlog issue #{issue['number']}", False

    def onboarding_note(self, t):
        hands = t.get("hands")
        lines = ["**Onboarding**", "- Hands: " + (", ".join(f"`{h}`" for h in hands) if hands else
                                                  "none (only the Linux runner)" if hands is not None else
                                                  "not stated by the agent (product.yml unchanged)")
                 + " — written to product.yml on `merge`"]
        backlog = t.get("backlog") or []
        lines.append(f"- Backlog ({len(backlog)} issue(s), not started; `/backlog start <n>` starts one):")
        lines += [f"  - [#{b['number']} {b['title']}]({b['url']})" for b in backlog] or ["  - none"]
        return "\n".join(lines)

    def onboarding_accepted(self, ctx, t):
        """The owner merged the onboarding pull request: the hands into product.yml (deployed checkout, its Gitea copy
        and the live registry; a lab runner starts if `lab` was added)."""
        name = t["product"]
        done = []
        if t.get("hands") is not None:
            try:
                res = self.product_admin(["hands", name, *t["hands"]])
                with self.lock:
                    self.products[name]["hands"] = res["product"]["hands"]
                    self.save()
                path = f"products/{name}/product.yml"
                old, sha = self.g.read_file(self.cfg["gitea"]["factory_repo"], path)
                if old != res["file"]:
                    self.g.write_file(self.cfg["gitea"]["factory_repo"], path, res["file"],
                                      f"products: {name} hands {t['hands']} (onboarding merged by {ctx['who']})", sha)
                    done.append(f"product.yml: hands {t['hands'] or 'none'} (committed to Gitea "
                                f"`{self.cfg['gitea']['factory_repo']}`)")
                else:
                    done.append(f"product.yml: hands {t['hands'] or 'none'} (unchanged)")
            except Exception as e:
                done.append(f"⚠ product.yml hands not written ({str(e)[:200]})")
        with self.lock:
            self.state["onboarding"][name] = "accepted"
            self.save()
        self.record(f"Product `{name}`: onboarding accepted (hands {t.get('hands')})", ctx)
        backlog = t.get("backlog") or []
        self.reply(ctx, f"✅ **Onboarding of `{name}` accepted.**\n" + "".join(f"- {d}\n" for d in done)
                   + f"- Backlog: {len(backlog)} issue(s); `/backlog` lists them, `/backlog start <n>` starts one; new "
                     f"requests go in #**{t['stream']}>requests**.")

    def cmd_onboarding(self, ctx, args):
        name = ctx["product"]
        arg = args.strip().lower()
        open_key = self.onboarding_task(name, open_only=True)
        if arg == "skip":
            if open_key:
                t = self.state["tasks"][open_key]
                self.g.comment(self.repo(name), t["number"], f"Onboarding skipped by {ctx['who']} "
                                                             f"([Zulip]({ctx['link']})).")
                self.close_task(open_key, "cancelled")
            with self.lock:
                self.state["onboarding"][name] = "skipped"
                self.save()
            self.record(f"Product `{name}`: onboarding skipped", ctx)
            self.reply(ctx, f"Onboarding of `{name}` skipped" + (" (its open task cancelled)" if open_key else "")
                       + ". The product keeps the default gates (placeholders); `/onboarding` asks the questions again.")
        elif not arg:
            if open_key:
                t = self.state["tasks"][open_key]
                self.reply(ctx, f"Onboarding is under way: #**{t['stream']}>{t['topic']}** ({t['state']}).")
                return
            self.ask_onboarding(name)
            if ctx["topic"].lower() != ONBOARDING_TOPIC:
                self.reply(ctx, f"Questions posted in #**{self.products[name]['zulip_stream']}>{ONBOARDING_TOPIC}**.")
        else:
            self.reply(ctx, "Usage: " + commands.find("/onboarding")["usage"])

    def cmd_backlog(self, ctx, args):
        name = ctx["product"]
        repo = self.repo(name)
        parts = args.split()
        if not parts:
            items = self.g.backlog_issues(repo)
            self.reply(ctx, f"**Backlog of `{name}`** (not started)\n" + ("\n".join(
                f"- [#{i['number']} {i['title']}]({i['html_url']})" for i in sorted(items, key=lambda i: i["number"]))
                or "- empty") + ("\n`/backlog start <n>` starts one." if items else ""))
            return
        if parts[0].lower() != "start" or len(parts) != 2 or not parts[1].lstrip("#").isdigit():
            self.reply(ctx, "Usage: " + commands.find("/backlog")["usage"])
            return
        n = int(parts[1].lstrip("#"))
        try:
            iss = self.g.issue(repo, n)
        except Exception:
            self.reply(ctx, f"No issue #{n} in {repo}.")
            return
        labels = {lb["name"] for lb in iss.get("labels") or []}
        if iss.get("pull_request") or iss["state"] != "open" or "backlog" not in labels or "factory-task" in labels:
            self.reply(ctx, f"#{n} is not an open backlog issue (`/backlog` lists them).")
            return
        title = short_title(iss["title"])
        request = iss["title"] + "\n\n" + (iss.get("body") or "").split("\n\n---\n")[0]
        body = (iss.get("body") or "") + f"\n\nStarted as a task by the owner in [Zulip]({ctx['link']})."
        key = self.register_task(name, iss, title, body, request)
        t = self.state["tasks"][key]
        self.record(f"Task {key}: backlog item started", ctx)
        self.reply(ctx, f"Backlog #{n} started → #**{t['stream']}>{t['topic']}**")
        quoted = "\n".join("> " + ln for ln in request.splitlines())
        self.z.send(t["stream"], t["topic"], f"**Backlog item started** (from #**{ctx['stream']}>{ctx['topic']}**):\n"
                                             f"{quoted}\n\nRecord: {iss['html_url']} · Planning now…")
        self.enqueue({"key": key, "kind": "plan"})

    def pause_task(self, key, reason, resume_job=None):
        t = self.state["tasks"][key]
        with self.cv:
            held = [j for j in self.state["queue"] if j["key"] == key]
            for j in held:
                self.state["queue"].remove(j)
            if resume_job:
                held.insert(0, resume_job)
            t["held"] = held + t.get("held", [])
            if t["state"] != "paused":
                t["prev_state"] = t["state"]
            t["state"] = "paused"
            t["pause_reason"] = reason
            self.save()
            sess = self.running.get(key)
        if sess:
            sess.abort()
        self.g.set_state(self.repo(t["product"]), t["number"], "paused")
        self.say(key, f"⏸ Paused: {reason}. Reply `/resume` to continue.")

    # == Zulip events ===============================================================================================
    def load_owners(self):
        self.owners = {u["user_id"]: u["full_name"] for u in self.z.users()
                       if u.get("role") == 100 and not u.get("is_bot") and u.get("is_active", True)}
        log.info("owners: %s", self.owners)

    def is_owner(self, uid):
        if uid not in self.owners and uid not in self.others:
            self.load_owners()
            if uid not in self.owners:
                self.others.add(uid)
        return uid in self.owners

    def handle(self, ev):
        if ev["type"] == "message":
            m = ev["message"]
            if m["id"] <= self.state["last_message_id"]:
                return
            try:
                self.on_message(m)
            finally:
                self.state["last_message_id"] = max(self.state["last_message_id"], m["id"])
                self.save()
        elif ev["type"] == "reaction" and ev.get("op") == "add":
            self.on_reaction(ev)

    def streams(self):
        return [self.factory_stream] + [p["zulip_stream"] for p in self.products.values()]

    def on_message(self, m):
        if m.get("type") != "stream" or m.get("sender_email") == self.z.email:
            return
        stream, topic, text = m["display_recipient"], m["subject"], m["content"].strip()
        if stream not in self.streams() or not self.is_owner(m["sender_id"]):
            return  # only the owner's messages count (bots and other members are ignored)
        product = next((n for n, p in self.products.items() if p["zulip_stream"] == stream), None)
        key = self.task_by_topic(stream, topic)
        name = norm_topic(topic)
        onboarding = product and name == ONBOARDING_TOPIC
        if key and onboarding and self.state["tasks"][key]["state"] == "cancelled" \
                and self.state["onboarding"].get(product) == "asked":
            key = None  # asked again after a cancelled onboarding: the next answer starts a new one
        if key:
            kind = "task"
        elif name == "requests":
            kind = "requests"
        elif stream == self.factory_stream:
            kind = "chat"
            key = f"chat:{stream}:{topic.lower()}"
        elif onboarding:
            kind = "onboarding"
        else:
            kind = "other"
        ctx = {"stream": stream, "topic": topic, "msg_id": m["id"], "key": key, "kind": kind, "product": product,
               "who": self.who(m["sender_id"], test=m.get("client") == TEST_CLIENT and self.test_mark("post", 120)),
               "link": self.z.message_link(m["id"])}
        word = text.split(None, 1)[0].lower() if text else ""
        cmd = commands.find(word)
        if cmd:
            if cmd["where"] == "factory" and stream != self.factory_stream:
                self.z.send(stream, topic, f"`{cmd['word']}` works in stream `{self.factory_stream}`.")
                return
            if cmd["where"] == "task" and kind != "task":
                self.z.send(stream, topic, f"`{cmd['word']}` works in a task topic. `/help` lists the commands.")
                return
            if cmd["where"] == "product" and not product:
                self.z.send(stream, topic, f"`{cmd['word']}` works in a product's channel (e.g. "
                                           f"`{self.cfg['default_product']}`).")
                return
            args = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            getattr(self, "cmd_" + cmd["handler"])(ctx, args)
            return
        uploads = UPLOAD.findall(text)
        if kind == "task":
            t = self.state["tasks"][key]
            if uploads and t["state"] not in CLOSED:
                text += self.store_uploads(ctx, self.products[t["product"]]["agent_user"],
                                           self.ensure_task_workspace(t), uploads)
            self.on_task_message(ctx, text)
        elif kind != "task" and self.misplaced_answer(ctx, text):
            return
        elif kind == "requests":
            if word.startswith("/") and word != "/request":
                self.z.send(stream, topic, f"Unknown command `{word}`. `/help` lists the commands.")
                return
            self.intake(product or self.cfg["default_product"], text, stream, topic, m["id"], uploads=uploads)
        elif kind == "chat":
            if uploads:
                text += self.store_uploads(ctx, self.cfg["chat"]["agent_user"], self.chat_workspace(), uploads)
            self.on_chat(ctx, text)
        elif kind == "onboarding":
            status = self.state["onboarding"].get(product)
            if word.startswith("/"):
                self.z.send(stream, topic, f"Unknown command `{word}`. `/help` lists the commands.")
            elif status in ("skipped", "accepted"):
                self.z.send(stream, topic, f"Onboarding of `{product}` is {status}; `/onboarding` asks the questions "
                                           f"again, new work goes in topic `requests`.")
            else:
                self.onboarding_start(ctx, text, uploads)
        elif product and self.state["onboarding"].get(product) == "asked":
            self.z.send(stream, topic, f"Onboarding of `{product}` is waiting for your answers: reply in topic "
                                       f"#**{stream}>{ONBOARDING_TOPIC}** (exactly `{ONBOARDING_TOPIC}`). "
                                       "New work goes in topic `requests`; `/help` lists the commands.")
        elif f"{stream}>{topic}" not in self.state["hinted"]:
            self.state["hinted"].append(f"{stream}>{topic}")
            self.z.send(stream, topic, "This topic is not a task. Post new requests in topic `requests`; "
                                       "`/help` lists the commands.")

    def who(self, uid, test=False):
        """The name the decision log and records use for an owner action; server-side test actions are marked."""
        name = self.owners.get(uid, "owner")
        return f"TEST, not the owner (build agent as {name})" if test else name

    def test_mark(self, name, max_age=None):
        """A mark the test helpers (infra/supervisor/test/*-as-owner.sh) write as root before acting as the owner
        from the server: `post` (fresh for `max_age` seconds) or `reaction-<message id>-<emoji>`. Only root on the
        VM can write the directory, so a mark cannot come from Zulip."""
        try:
            st = os.stat(os.path.join(TEST_MARKS, name))
        except OSError:
            return False
        return st.st_uid == 0 and (max_age is None or time.time() - st.st_mtime <= max_age)

    def on_reaction(self, ev):
        if not self.is_owner(ev["user_id"]):
            return
        for key, t in self.state["tasks"].items():
            if ev["message_id"] in (t.get("plan_msg_ids") or []):
                ctx = {"stream": t["stream"], "topic": t["topic"], "key": key, "kind": "task",
                       "who": self.who(ev["user_id"],
                                       test=self.test_mark(f"reaction-{ev['message_id']}-{ev['emoji_name']}")),
                       "link": self.z.message_link(ev["message_id"])}
                if ev["emoji_name"] in APPROVE_EMOJI:
                    self.approve(ctx, "reaction ✅")
                elif ev["emoji_name"] in REJECT_EMOJI:
                    self.reject(ctx, "reaction ❌")
                return

    def on_task_message(self, ctx, text):
        key = ctx["key"]
        t = self.state["tasks"][key]
        low = text.lower().strip().rstrip(".!")
        if t["state"] == "awaiting-approval":
            if low in APPROVE_WORDS:
                return self.approve(ctx, "message")
            if low in REJECT_WORDS:
                return self.reject(ctx, "message")
            if low.startswith("change:"):
                return self.change(ctx, text.split(":", 1)[1].strip())
        if low in MERGE_WORDS:
            return self.cmd_merge(ctx, "")
        if low.startswith(("merge anyway", "merge with gate change")):
            return self.cmd_merge(ctx, text.strip()[len("merge"):].strip())
        if low == "rework" or low.startswith("rework:"):
            return self.cmd_rework(ctx, REWORK.match(text.strip()).group(1).strip())
        if t["state"] in CLOSED:
            self.say(key, f"This task is {t['state']}. Post a new request in topic `requests`.")
            return
        if t["state"] == "checking":
            self.say(key, "The gates are running on the last push; nothing goes to the agent meanwhile. Send your "
                          "message again after the result (✅/❌ here), or `/pause` first.")
            return
        if t["state"] == "paused":
            t.setdefault("held", []).append({"key": key, "kind": "forward", "text": text})
            self.save()
            self.say(key, "Task is paused; your message goes to the agent after `/resume`.")
            return
        self.forward(ctx, text, {"key": key, "kind": "forward", "text": text})

    def misplaced_answer(self, ctx, text):
        """`approve`, `reject`, `change: …`, `merge…` or `rework…` typed outside a task topic (e.g. in `requests` or a
        chat) is an answer, never a new request: with one task waiting for that answer it is applied there, else the
        owner gets the list of waiting tasks with links."""
        low = text.lower().strip().rstrip(".!")
        approve, reject, change = low in APPROVE_WORDS, low in REJECT_WORDS, low.startswith("change:")
        merge = low in MERGE_WORDS or low.startswith(("merge anyway", "merge with gate change"))
        rework = low == "rework" or low.startswith("rework:")
        if not (approve or reject or change or merge or rework):
            return False
        want = "awaiting-approval" if (approve or reject or change) else None
        waiting = [(k, t) for k, t in self.state["tasks"].items()
                   if (ctx["product"] is None or t["product"] == ctx["product"])
                   and (t["state"] == want if want else t.get("pr_number") and t["state"] not in ("merged", "cancelled"))]
        if len(waiting) == 1:
            key, t = waiting[0]
            moved = dict(ctx, stream=t["stream"], topic=t["topic"], key=key, kind="task")
            self.z.send(ctx["stream"], ctx["topic"], f"Applied to task #{t['number']}: "
                                                     f"#**{t['stream']}>{t['topic']}** (next time, reply there).")
            self.on_task_message(moved, text)
        elif waiting:
            links = "\n".join(f"- #**{t['stream']}>{t['topic']}**" for _, t in waiting)
            self.z.send(ctx["stream"], ctx["topic"], f"Which task? Open its topic and reply `{text.strip()}` there:\n"
                                                     f"{links}")
        else:
            self.z.send(ctx["stream"], ctx["topic"], "No task is waiting for that answer. `/status` lists the tasks.")
        return True

    def forward(self, ctx, text, job):
        sess = self.running.get(ctx["key"])
        if sess is not None:
            try:
                sess.steer(prompts.FORWARD.format(text=text))
                self.z.react(ctx["msg_id"], "eyes")
                return
            except Exception as e:
                log.warning("steer failed (%s); queueing", e)
        self.enqueue(job)
        self.z.react(ctx["msg_id"], "eyes")
        busy = [k for k in self.running if k != ctx["key"]]
        if busy and len(self.running) >= int(self.settings.get("concurrency")):
            self.z.send(ctx["stream"], ctx["topic"], f"Queued: the agent slot is busy with `{busy[0]}`.")

    def approve(self, ctx, via):
        key = ctx["key"]
        t = self.state["tasks"][key]
        if t["state"] != "awaiting-approval":
            self.say(key, f"No plan is waiting for approval (state: {t['state']}).")
            return
        repo = self.repo(t["product"])
        with self.lock:
            t["state"] = "building"
            self.save()
        self.g.set_state(repo, t["number"], "building")
        self.g.comment(repo, t["number"], f"Plan **approved** by {ctx['who']} ({via}, [Zulip]({ctx['link']})) at "
                                          f"{now_utc():%Y-%m-%d %H:%M} UTC.")
        self.record(f"Task {key}: plan approved", ctx)
        self.say(key, f"👍 Approved. Building on branch `task/{t['number']}`…")
        self.enqueue({"key": key, "kind": "build"})

    def change(self, ctx, text):
        key = ctx["key"]
        t = self.state["tasks"][key]
        repo = self.repo(t["product"])
        with self.lock:
            t["state"] = "planning"
            self.save()
        self.g.set_state(repo, t["number"], "planning")
        self.g.comment(repo, t["number"], f"Change requested by {ctx['who']} ([Zulip]({ctx['link']})):\n\n{text}")
        self.record(f"Task {key}: plan change requested", ctx)
        self.say(key, "✏️ Revising the plan…")
        self.enqueue({"key": key, "kind": "change", "text": text})

    def reject(self, ctx, via):
        key = ctx["key"]
        t = self.state["tasks"][key]
        if t["state"] != "awaiting-approval":
            self.say(key, f"No plan is waiting for approval (state: {t['state']}).")
            return
        self.g.comment(self.repo(t["product"]), t["number"],
                       f"Plan **rejected** by {ctx['who']} ({via}, [Zulip]({ctx['link']})).")
        self.record(f"Task {key}: plan rejected", ctx)
        self.close_task(key, "cancelled")
        self.say(key, "❌ Rejected. Task cancelled and its issue closed.")

    def close_task(self, key, state):
        t = self.state["tasks"][key]
        with self.cv:
            self.state["queue"] = [j for j in self.state["queue"] if j["key"] != key]
            t["state"] = state
            t["closed_at"] = time.time()
            t["held"] = []
            self.save()
            sess = self.running.get(key)
        if sess:
            sess.abort()
        repo = self.repo(t["product"])
        self.g.set_state(repo, t["number"], state)
        self.g.edit_issue(repo, t["number"], state="closed")
        try:
            self.agent_exec(self.products[t["product"]]["agent_user"], ["rm-workspace", f"task-{t['number']}"])
        except Exception as e:
            log.warning("workspace removal failed: %s", e)

    def record(self, what, ctx):
        try:
            self.settings.decision(what, ctx["who"], ctx["link"])
        except Exception as e:
            log.warning("decision log write failed: %s", e)

    # == chat =======================================================================================================
    def on_chat(self, ctx, text):
        key = ctx["key"]
        with self.lock:
            self.state["chats"].setdefault(key, {"stream": ctx["stream"], "topic": ctx["topic"],
                                                 "session_file": None})
            self.save()
        self.forward(ctx, text, {"key": key, "kind": "chat", "text": text})

    def chat_workspace(self):
        url = f"{self.cfg['gitea']['api'].rstrip('/')}/{self.cfg['gitea']['factory_repo']}.git"
        return self.agent_exec(self.cfg["chat"]["agent_user"], ["workspace", "factory", url]).strip().splitlines()[-1]

    # == attachments ================================================================================================
    def store_uploads(self, ctx, user, ws, uploads):
        """Copy the owner's uploaded files into <ws>/attachments/ (as the agent user). Returns a note listing their
        paths for the agent's message; files of other types, too large or unreadable are refused in the topic."""
        a = self.cfg["attachments"]
        max_bytes = int(a["max_mb"]) * 1024 * 1024
        allowed = {e.lower() for e in a["extensions"]}
        name = os.path.basename(ws.rstrip("/"))
        saved, refused = [], []
        for label, path in uploads:
            fname = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext not in allowed:
                refused.append(f"`{fname}`: type `.{ext or '?'}` is not accepted")
                continue
            try:
                data = self.z.download(path, max_bytes)
                if data is None:
                    refused.append(f"`{fname}`: larger than {a['max_mb']} MB")
                    continue
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", fname).lstrip("._-")[-100:] or f"file.{ext}"
                out = self.agent_exec(user, ["put-file", name, f"{ctx['msg_id']}-{safe}", str(max_bytes)],
                                      stdin_text=data, timeout=120)
                saved.append(out.strip().splitlines()[-1])
            except Exception as e:
                log.warning("attachment %s failed: %s", path, e)
                refused.append(f"`{fname}`: could not be copied ({str(e)[:120]})")
        if refused:
            self.z.send(ctx["stream"], ctx["topic"],
                        "Not given to the agent:\n" + "\n".join(f"- {r}" for r in refused)
                        + f"\nAccepted: up to {a['max_mb']} MB, types {', '.join(sorted(allowed))}.")
        if not saved:
            return ""
        return ("\n\nFiles the owner attached (copied into your workspace; not committed unless the task needs "
                "them):\n" + "\n".join(f"- {p}" for p in saved))

    def run_chat(self, job):
        key = job["key"]
        c = self.state["chats"][key]
        chat = self.cfg["chat"]
        user = chat["agent_user"]
        ws = self.chat_workspace()
        system = prompts.CHAT_SYSTEM.format(
            stream=c["stream"], topic=c["topic"], token_file=chat["token_file"],
            repos=", ".join(self.repo(p) for p in self.products), records=self.cfg["gitea"]["records_repo"],
            commands=self.help())
        sess, session_file, new = self.spawn(key, user, chat["omp_profile"], ws, system,
                                             self.settings.get("max_time.chat"), prompts.CHAT_TOOLS,
                                             self.settings.omp_flags(c), c.get("session_file"))
        with self.lock:
            self.running[key] = sess
            c["session_file"] = session_file
            self.save()
        try:
            if job["kind"] == "resume":
                msg = prompts.CHAT_RESUME if not new else prompts.CHAT.format(
                    stream=c["stream"], topic=c["topic"], text=job.get("text", ""))
            else:
                msg = prompts.CHAT.format(stream=c["stream"], topic=c["topic"], text=job["text"])
            result = sess.prompt(msg)
            text = sess.last_text().strip()
        except OmpExited as e:
            if not self.stopping:
                self.say(key, f"⚠ The assistant stopped ({e}). Ask again to continue.")
            return
        finally:
            sess.close()
        if result.get("status") == "error":
            msg = (result.get("error") or {}).get("message", "unknown error")
            self.say(key, f"⚠ The model returned an error: {msg[:300]}")
            return
        if result.get("status") == "completed" and text:
            self.say(key, text)

    # == commands ===================================================================================================
    def reply(self, ctx, text):
        return self.z.send(ctx["stream"], ctx["topic"], text)

    def cmd_help(self, ctx, args):
        self.reply(ctx, self.help())

    def help(self):
        return commands.help_text(self.cfg["default_product"], self.cfg["attachments"])

    def status_text(self, ctx):
        if ctx and ctx["kind"] == "task":
            t = self.state["tasks"][ctx["key"]]
            lines = [f"**#{t['number']} {t['title']}**: {t['state']}"]
            if t["state"] == "paused":
                lines.append(f"Reason: {t.get('pause_reason', '')}")
            if ctx["key"] in self.state["paced"]:
                lines.append("Waiting for model usage to drop (pacing).")
            if ctx["key"] in self.running:
                lines.append("The agent is working on it now.")
            queued = sum(1 for j in self.state["queue"] if j["key"] == ctx["key"])
            if queued:
                lines.append(f"Queued steps: {queued}")
            lines.append(f"[Issue]({t['issue_url']})" + (f" · [Pull request]({t['pr_url']})" if t.get("pr_url") else ""))
            gate = t.get("gate")
            if gate:
                lines.append(f"Gates on `{gate['sha'][:8]}`: {gate.get('result') or 'running'} (fix attempts "
                             f"{gate.get('fixes', 0)} of {self.settings.get('gates.max_fixes')})")
            if t.get("kind"):
                lines.append(f"Kind: {t['kind']} (playbook factory-{t['kind']})")
            lines.append(f"Model: `{t.get('model') or self.settings.get('model')}`, thinking "
                         f"`{t.get('thinking') or self.settings.get('thinking')}`")
            return "\n".join(lines)
        only = ctx["product"] if ctx and ctx.get("product") else None
        lines = ["**Tasks**" + (f" ({only})" if only else "")]
        tasks = [(k, t) for k, t in self.state["tasks"].items() if not only or t["product"] == only]
        open_tasks = [(k, t) for k, t in tasks if t["state"] not in CLOSED]
        closed = [(k, t) for k, t in tasks if t["state"] in CLOSED][-5:]
        for k, t in open_tasks + closed:
            extra = f" · [PR]({t['pr_url']})" if t.get("pr_url") else ""
            lines.append(f"- {t['product']} #**{t['stream']}>{t['topic']}** — {t['state']}"
                         f"{' (running)' if k in self.running else ''}{extra}")
        if len(lines) == 1:
            lines.append("- none yet")
        lines.append("\nModel usage: " + "; ".join(self.usage_lines(self.usage())))
        lines.append(f"Queue: {len(self.state['queue'])} step(s); running: "
                     f"{', '.join(self.running) or 'nothing'}.")
        return "\n".join(lines)

    def cmd_status(self, ctx, args):
        self.reply(ctx, self.status_text(ctx))

    def cmd_pause(self, ctx, args):
        t = self.state["tasks"][ctx["key"]]
        if t["state"] in CLOSED + ("paused",):
            self.reply(ctx, f"Nothing to pause (state: {t['state']}).")
            return
        resume = {"key": ctx["key"], "kind": "resume", "step": t["state"]} if ctx["key"] in self.running else None
        self.record(f"Task {ctx['key']}: paused", ctx)
        self.pause_task(ctx["key"], f"paused by {ctx['who']}", resume)

    def cmd_resume(self, ctx, args):
        key = ctx["key"]
        t = self.state["tasks"][key]
        if t["state"] != "paused":
            note = " It is waiting for model usage and continues automatically." if key in self.state["paced"] else ""
            self.reply(ctx, f"The task is not paused (state: {t['state']}).{note}")
            return
        with self.cv:
            t["state"] = t.pop("prev_state", "planning")
            held = t.pop("held", [])
            fixes = [j for j in held if j["kind"] == "fix"]
            if fixes:  # red gates after the last attempt: the owner's guidance goes into the next fix attempt
                guidance = [j["text"] for j in held if j["kind"] == "forward"]
                job = dict(fixes[0])
                if guidance:
                    job["text"] += "\n\nThe owner's guidance:\n" + "\n\n".join(guidance)
                held = [job] + [j for j in held if j["kind"] not in ("fix", "forward")]
            if not held and t["state"] in ("planning", "building"):
                held = [{"key": key, "kind": "resume", "step": t["state"]}]
            if t["state"] == "checking" and t.get("gate"):
                t["gate"]["since"] = time.time()
            self.state["queue"][0:0] = held
            t.pop("pause_reason", None)
            self.save()
            self.cv.notify_all()
        self.g.set_state(self.repo(t["product"]), t["number"], t["state"])
        self.record(f"Task {key}: resumed ({t['state']})", ctx)
        self.reply(ctx, f"▶ Resumed ({t['state']}).")

    def cmd_cancel(self, ctx, args):
        t = self.state["tasks"][ctx["key"]]
        if t["state"] in CLOSED:
            self.reply(ctx, f"Nothing to cancel (state: {t['state']}).")
            return
        self.g.comment(self.repo(t["product"]), t["number"], f"Cancelled by {ctx['who']} ([Zulip]({ctx['link']})).")
        self.record(f"Task {ctx['key']}: cancelled", ctx)
        self.close_task(ctx["key"], "cancelled")
        self.reply(ctx, "🛑 Cancelled. Issue closed, workspace removed.")

    def topic_target(self, ctx):
        if ctx["kind"] == "task":
            return self.state["tasks"][ctx["key"]]
        if ctx["kind"] == "chat":
            return self.state["chats"].setdefault(ctx["key"], {"stream": ctx["stream"], "topic": ctx["topic"],
                                                               "session_file": None})
        return None

    def cmd_model(self, ctx, args):
        parts = args.split()
        target = self.topic_target(ctx)
        try:
            if not parts:
                roles = ", ".join(f"{r}: `{self.settings.get('role.' + r) or 'OMP default'}`"
                                  for r in ("smol", "slow", "plan"))
                here = f"This topic: `{target.get('model') or 'factory default'}`\n" if target is not None else ""
                self.reply(ctx, f"{here}Factory default: `{self.settings.get('model') or 'OMP default'}`\n"
                                f"Roles: {roles}\n`/model list` shows the models OMP can use.")
            elif parts[0] == "list":
                models = self.settings.models()
                self.reply(ctx, "**Models OMP can use with the current login**\n" +
                           "\n".join(f"- `{m}`" for m in models))
            elif parts[0] == "default" and len(parts) == 2:
                old, new = self.settings.set("model", parts[1], ctx["who"], ctx["link"])
                self.reply(ctx, f"Factory default model: `{old}` → `{new}`; used by new sessions and by topics without "
                                f"their own model from their next turn. Recorded in the decision log.")
            elif parts[0] == "role" and len(parts) == 3 and parts[1] in ("smol", "slow", "plan"):
                old, new = self.settings.set(f"role.{parts[1]}", parts[2], ctx["who"], ctx["link"])
                self.reply(ctx, f"Role `{parts[1]}`: `{old}` → `{new}` from the next turn. Recorded.")
            elif len(parts) == 1 and target is not None:
                model = self.settings.model_selector(parts[0])
                target["model"] = model
                self.save()
                self.record(f"Topic {ctx['stream']} > {ctx['topic']}: model `{model}`", ctx)
                self.reply(ctx, f"This topic's agent uses `{model}` from its next turn. Recorded.")
            else:
                self.reply(ctx, "Usage: " + commands.find("/model")["usage"]
                           + ("" if target is not None else " (a model for one topic: in a task or chat topic)"))
        except ValueError as e:
            self.reply(ctx, f"Not changed: {e}")

    def cmd_thinking(self, ctx, args):
        parts = args.split()
        target = self.topic_target(ctx)
        try:
            if len(parts) == 2 and parts[0] == "default":
                old, new = self.settings.set("thinking", parts[1], ctx["who"], ctx["link"])
                self.reply(ctx, f"Default thinking: `{old}` → `{new}` from the next turn. Recorded.")
            elif len(parts) == 1 and target is not None:
                from .settings import _thinking
                target["thinking"] = _thinking(parts[0])
                self.save()
                self.record(f"Topic {ctx['stream']} > {ctx['topic']}: thinking `{parts[0]}`", ctx)
                self.reply(ctx, f"This topic's agent thinks at `{parts[0]}` from its next turn. Recorded.")
            else:
                cur = (target or {}).get("thinking") or self.settings.get("thinking")
                self.reply(ctx, f"Thinking: `{cur}`. Usage: " + commands.find("/thinking")["usage"])
        except ValueError as e:
            self.reply(ctx, f"Not changed: {e}")

    def cmd_settings(self, ctx, args):
        parts = args.split()
        try:
            if not parts:
                self.reply(ctx, self.settings.show())
            elif parts[0] == "set" and len(parts) == 3:
                old, new = self.settings.set(parts[1], parts[2], ctx["who"], ctx["link"])
                self.with_workers_notified()
                self.reply(ctx, f"`{parts[1]}`: `{old}` → `{new}`; applies from the next step. Recorded in the "
                                f"decision log.")
            elif parts[0] == "reset" and len(parts) == 2:
                old, new = self.settings.reset(parts[1], ctx["who"], ctx["link"])
                self.with_workers_notified()
                self.reply(ctx, f"`{parts[1]}`: `{old}` → `{new}` (default). Recorded.")
            else:
                self.reply(ctx, "Usage: " + commands.find("/settings")["usage"])
        except ValueError as e:
            self.reply(ctx, f"Not changed: {e}")

    def with_workers_notified(self):
        with self.cv:
            self.usage_cache = (0.0, {})
            self.cv.notify_all()

    # == products ===================================================================================================
    def cmd_product(self, ctx, args):
        parts = args.split(None, 2)
        sub = parts[0].lower() if parts else "list"
        if sub == "list":
            lines = ["**Products**"]
            for name, p in self.products.items():
                n_open = sum(1 for t in self.state["tasks"].values()
                             if t["product"] == name and t["state"] not in CLOSED)
                default = " *(default)*" if name == self.cfg["default_product"] else ""
                onb = self.state["onboarding"].get(name)
                lines.append(f"- `{name}`{default} — {p.get('title', name)}: stream #**{p['zulip_stream']}**, "
                             f"repository [{self.repo(name)}]({self.g.public}/{self.repo(name)}), agent user "
                             f"`{p['agent_user']}`; {n_open} open task(s)" + (f"; onboarding {onb}" if onb else ""))
            self.reply(ctx, "\n".join(lines))
        elif sub == "new" and len(parts) >= 2:
            self.product_new(ctx, parts[1], parts[2].strip() if len(parts) > 2 else "")
        elif sub == "remove" and len(parts) == 2:
            self.product_remove(ctx, parts[1])
        else:
            self.reply(ctx, "Usage: " + commands.find("/product")["usage"])

    def product_admin(self, args, stdin_text=""):
        res = subprocess.run(["sudo", "-n", PRODUCT_ADMIN, *args], input=stdin_text, capture_output=True, text=True,
                             timeout=600)
        if res.returncode != 0:
            raise ValueError(res.stderr.strip()[-400:] or f"exit {res.returncode}")
        return json.loads(res.stdout.strip().splitlines()[-1])

    def product_new(self, ctx, name, title):
        if not PRODUCT_NAME.fullmatch(name) or "--" in name:
            self.reply(ctx, "Not created: the name must be 2-20 characters of lowercase letters, digits and single "
                            "hyphens, starting with a letter (e.g. `demo-one`).")
            return
        if name in self.products:
            self.reply(ctx, f"Not created: product `{name}` already exists.")
            return
        self.reply(ctx, f"Creating product `{name}`…")
        try:
            res = self.product_admin(["add", name], (title or name).replace("\n", " ") + "\n")
        except Exception as e:
            self.reply(ctx, f"Not created: {e}")
            return
        p, full = res["product"], res["repo"]
        done = [f"Gitea: repository [{full}]({self.g.public}/{full}) with a README, `main` protected; agent account "
                f"`{p['gitea']['agent_login']}` with a token that can write to that repository only",
                f"Agent user `{p['agent_user']}` (own home and OMP profile `{p['omp_profile']}`; no sudo, no secrets)"]
        try:
            self.g.write_file(self.cfg["gitea"]["factory_repo"], f"products/{name}/product.yml", res["file"],
                              f"products: add {name} (/product new by {ctx['who']})")
            done.append(f"Registry: `products/{name}/product.yml` committed to Gitea "
                        f"`{self.cfg['gitea']['factory_repo']}` (GitHub gets it with the next workstation sync)")
        except Exception as e:
            done.append(f"⚠ Registry commit to Gitea failed ({str(e)[:200]}); the product works but is not in the "
                        f"repository yet")
        try:
            self.z.subscribe([p["zulip_stream"]], description=p["title"])
            if not self.owners:
                self.load_owners()
            self.z.subscribe([p["zulip_stream"]], principals=list(self.owners))
            done.append(f"Zulip: stream #**{p['zulip_stream']}**, you are subscribed")
        except Exception as e:
            done.append(f"⚠ Zulip stream not created ({str(e)[:200]})")
        with self.lock:
            self.products[name] = p
            self.save()
        self.g.ensure_labels(full)
        self.record(f"Product `{name}` created ({p['title']})", ctx)
        self.reply(ctx, f"✅ Product **{name}** created and live:\n" + "\n".join(f"- {d}" for d in done)
                   + f"\n\nNext: answer the onboarding questions in #**{p['zulip_stream']}>{ONBOARDING_TOPIC}** (or "
                     f"`/onboarding skip` there); requests go in #**{p['zulip_stream']}>requests**.")
        self.ask_onboarding(name)

    def product_remove(self, ctx, name):
        if name not in self.products:
            self.reply(ctx, f"No product `{name}`. `/product list` shows them.")
            return
        if name == self.cfg["default_product"]:
            self.reply(ctx, f"`{name}` is the default product; it is not removed from chat.")
            return
        open_tasks = [k for k, t in self.state["tasks"].items()
                      if t["product"] == name and t["state"] not in CLOSED]
        if open_tasks or any(k.startswith(name + "#") for k in self.running):
            self.reply(ctx, f"Not removed: open task(s) {', '.join(open_tasks)}; `/cancel` them first.")
            return
        p = self.products[name]
        self.reply(ctx, f"Removing product `{name}`…")
        try:
            self.product_admin(["remove", name])
        except Exception as e:
            self.reply(ctx, f"Not removed: {e}")
            return
        done = [f"Gitea: repository {self.repo(name)}, its organisation and agent account `{p['gitea']['agent_login']}`"
                f" (and its token) deleted", f"Agent user `{p['agent_user']}`, its home and workspaces deleted"]
        try:
            path = f"products/{name}/product.yml"
            _, sha = self.g.read_file(self.cfg["gitea"]["factory_repo"], path)
            if sha:
                self.g.delete_file(self.cfg["gitea"]["factory_repo"], path, sha,
                                   f"products: remove {name} (/product remove by {ctx['who']})")
            done.append(f"Registry: `{path}` removed in Gitea (GitHub follows with the next workstation sync)")
        except Exception as e:
            done.append(f"⚠ Registry file not removed in Gitea ({str(e)[:200]})")
        try:
            self.z.archive_stream(p["zulip_stream"])
            done.append(f"Zulip: stream `{p['zulip_stream']}` archived")
        except Exception as e:
            done.append(f"⚠ Zulip stream `{p['zulip_stream']}` not archived ({str(e)[:150]}); archive it in its "
                        f"channel settings")
        with self.lock:
            self.products.pop(name)
            for k in [k for k, t in self.state["tasks"].items() if t["product"] == name]:
                self.state["tasks"].pop(k)
            self.state["queue"] = [j for j in self.state["queue"] if not j["key"].startswith(name + "#")]
            self.state["onboarding"].pop(name, None)
            self.save()
        self.record(f"Product `{name}` removed", ctx)
        self.reply(ctx, f"🗑 Product **{name}** removed:\n" + "\n".join(f"- {d}" for d in done))

    # == daily digest ===============================================================================================
    def digest_loop(self):
        """Post the digest once a day at digest.time (digest.timezone); a day missed while down is posted on start."""
        while True:
            time.sleep(30)
            try:
                now = datetime.now(ZoneInfo(self.settings.get("digest.timezone")))
                hh, mm = map(int, self.settings.get("digest.time").split(":"))
                if (now.hour, now.minute) >= (hh, mm) and self.state.get("digest_day") != now.date().isoformat():
                    self.post_digest(scheduled=True)
            except Exception:
                log.exception("digest failed")
                time.sleep(600)

    def cmd_report(self, ctx, args):
        """/report <text>: a factory problem report in the records repository, with the factory's recent log."""
        repo = self.cfg["gitea"]["records_repo"]
        if args.strip().lower() == "list":
            reports = self.g.open_reports(repo)
            self.reply(ctx, "**Open factory reports**\n" + ("\n".join(f"- [{r['title']}]({r['html_url']})"
                                                                      for r in reports) or "- none"))
            return
        if not args.strip():
            self.reply(ctx, "Write what went wrong after the command, e.g. `/report the plan never appeared in #2`.")
            return
        uploads = [name or url for name, url in UPLOAD.findall(args)]
        log_tail = ""
        try:
            with open(Path(self.cfg["log_dir"]) / "supervisor.log", encoding="utf-8", errors="replace") as f:
                log_tail = "".join(f.readlines()[-80:])
        except OSError as e:
            log_tail = f"(log not readable: {e})"
        body = (f"**Reported by** {ctx['who']} at {now_utc().strftime('%Y-%m-%d %H:%M UTC')} in "
                f"`{ctx['stream']} > {ctx['topic']}` ([message]({ctx['link']}))\n\n{args.strip()}\n\n"
                + (f"Attachments: {', '.join(uploads)}\n\n" if uploads else "")
                + f"<details><summary>Supervisor log (last 80 lines)</summary>\n\n```\n{log_tail}\n```\n</details>\n")
        issue = self.g.create_report(repo, short_title(args), body)
        self.record(f"Factory report filed: [{issue['title']}]({issue['html_url']})", ctx)
        self.reply(ctx, f"📝 Recorded as [report #{issue['number']}]({issue['html_url']}). It is listed in the digest "
                        "until it is fixed; `/report list` shows the open reports.")

    def cmd_digest(self, ctx, args):
        self.post_digest(scheduled=False)
        if not (ctx["stream"] == self.factory_stream and ctx["topic"].lower() == "digest"):
            self.reply(ctx, f"Posted in #**{self.factory_stream}>digest**.")

    def post_digest(self, scheduled):
        """The scheduled digest covers the time since the previous scheduled one; /digest shows the same span and
        does not replace the next scheduled post."""
        now = time.time()
        since = self.state.get("digest_at") or now - 86400
        if scheduled:
            with self.lock:
                tz = ZoneInfo(self.settings.get("digest.timezone"))
                self.state["digest_day"] = datetime.now(tz).date().isoformat()
                self.state["digest_at"] = now
                self.save()
        self.z.send(self.factory_stream, "digest", self.digest_text(since, scheduled))

    def digest_text(self, since, scheduled):
        tasks = self.state["tasks"]
        kind = "Daily digest" if scheduled else "Digest (on request)"
        lines = [f"**{kind}** · {zulip_time(time.time() * 1000)}", "", "**Tasks by state**"]
        order = ["planning", "awaiting-approval", "building", "checking", "paused", "done", "merged", "cancelled"]
        for name in self.products:
            counts = {s: 0 for s in order}
            for t in tasks.values():
                if t["product"] == name:
                    counts[t["state"]] = counts.get(t["state"], 0) + 1
            lines.append(f"- {name}: " + " · ".join(f"{s} {n}" for s, n in counts.items()))
        waiting = [t for t in tasks.values() if t["state"] == "awaiting-approval"]
        ready = [t for t in tasks.values() if t["state"] == "done" and (t.get("gate") or {}).get("result") == "green"]
        lines += ["", "**Waiting for your decision**"]
        lines += ([f"- {t['product']} #**{t['stream']}>{t['topic']}**: plan to approve · [issue]({t['issue_url']})"
                   for t in waiting]
                  + [f"- {t['product']} #**{t['stream']}>{t['topic']}**: gates green, `merge` to merge · "
                     f"[pull request]({t['pr_url']})" for t in ready]) or ["- nothing"]
        closed = sorted((t for t in tasks.values() if (t.get("closed_at") or 0) > since),
                        key=lambda t: t["closed_at"])
        lines += ["", f"**Finished since {zulip_time(since * 1000)}**"]
        lines += [f"- {t['product']} #**{t['stream']}>{t['topic']}**: {t['state']}"
                  + (f" · [pull request]({t['pr_url']})" if t.get("pr_url") else "") for t in closed] or ["- nothing"]
        paused = [f"- {t['product']} #**{t['stream']}>{t['topic']}**: {t.get('pause_reason', 'paused')}"
                  for t in tasks.values() if t["state"] == "paused"]
        paused += [f"- `{k}`: waiting for model usage to drop" for k in self.state["paced"]]
        lines += ["", "**Paused**"] + (paused or ["- nothing"])
        try:
            reports = self.g.open_reports(self.cfg["gitea"]["records_repo"])
            lines += ["", "**Open factory reports**"] + ([f"- [{r['title']}]({r['html_url']})" for r in reports]
                                                         or ["- none"])
        except Exception as e:
            log.warning("open reports failed: %s", e)
        windows = self.usage()
        lines += ["", "**Model usage**: " + ("; ".join(self.usage_lines(windows)) if windows
                                             else "unknown (omp usage failed)")]
        lines += ["", "**Backups**", "- " + self.backup_status(), "- " + self.offsite_status()]
        return "\n".join(lines)

    def backup_status(self):
        unit = self.cfg["digest"]["backup_unit"]
        out = subprocess.run(["systemctl", "show", unit, "-p", "Result", "-p", "ExecMainStatus", "-p",
                              "ExecMainStartTimestamp", "-p", "ExecMainExitTimestamp"],
                             capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).stdout
        v = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if not v.get("ExecMainExitTimestamp"):
            return f"Nightly backup (VM): no finished run since the VM started ({unit})"
        ok = v.get("Result") == "success" and v.get("ExecMainStatus") == "0"
        return (f"Nightly backup (VM): {'✅ succeeded' if ok else '❌ FAILED (status ' + v.get('ExecMainStatus', '?') + ')'}"
                f", finished {v['ExecMainExitTimestamp']}")

    def offsite_status(self):
        try:
            with open(self.cfg["digest"]["offsite_status_file"]) as f:
                line = f.read().strip().splitlines()[-1]
        except (OSError, IndexError):
            return "Off-site copy (owner's PC): no report received on the VM yet"
        mark = "✅" if " ok " in f" {line} " else "❌"
        return f"Off-site copy (owner's PC): {mark} `{line[:200]}`"

    # == start-up ===================================================================================================
    def provision_agents(self):
        """Give each agent user its Gitea credential (product write token, or read-only for chat)."""
        for name, p in self.products.items():
            self.agent_exec(p["agent_user"], ["set-token", p["gitea"]["agent_login"]],
                            stdin_text=self.secrets[f"gitea_token_{name}"] + "\n")
        chat = self.cfg["chat"]
        self.agent_exec(chat["agent_user"], ["set-token", chat["gitea_login"]],
                        stdin_text=self.secrets["gitea_token_reader"] + "\n")

    def rebuild(self):
        """Merge Gitea's record into local state; queue every step a restart interrupted."""
        tasks = self.state["tasks"]
        for name in self.products:
            repo = self.repo(name)
            for iss in self.g.task_issues(repo):
                key = f"{name}#{iss['number']}"
                labels = {lb["name"] for lb in iss.get("labels", [])}
                gstate = next((s for s in STATE_LABELS if s in labels), None)
                t = tasks.get(key)
                if t is None:
                    m = re.search(r"<!-- factory-task stream=(\S+) topic=(.+?) -->", iss.get("body") or "")
                    if not m:
                        continue
                    session = None
                    for c in self.g.comments(repo, iss["number"]):
                        sm = re.search(r"<!-- factory-session (\S+) -->", c.get("body") or "")
                        if sm:
                            session = sm.group(1)
                    req = (iss.get("body") or "").split("\n\n---\n")[0]
                    tasks[key] = t = {"product": name, "number": iss["number"], "title": iss["title"],
                                      "stream": m.group(1), "topic": m.group(2), "state": gstate or "planning",
                                      "issue_url": iss["html_url"], "request": req, "session_file": session}
                    if m.group(2).lower() == ONBOARDING_TOPIC:  # hands and backlog list stay with the lost state
                        t["kind"] = "onboarding"
                    if t["state"] == "paused":
                        t["prev_state"] = "building" if t.get("kind") == "onboarding" else "planning"
                    log.info("rebuilt %s from Gitea (%s)", key, t["state"])
                    continue
                t["issue_url"] = iss["html_url"]
                if iss["state"] == "closed" and t["state"] not in CLOSED:
                    t["state"] = "cancelled"
                elif gstate and gstate != "paused" and gstate != t["state"] and key not in self.state["paced"]:
                    t["state"] = gstate
        queued = {j["key"] for j in self.state["queue"]}
        for key, t in tasks.items():
            job = t.pop("inflight", None)
            if job or (t["state"] in ("planning", "building") and key not in queued):
                step = job["kind"] if job else ("build" if t["state"] == "building" else "plan")
                self.enqueue({"key": key, "kind": "resume", "step": step, "text": (job or {}).get("text", "")},
                             front=True)
                self.say(key, f"♻ The factory restarted while this task was {t['state']}; resuming the "
                              f"{step} step.")
        for key, c in self.state["chats"].items():
            job = c.pop("inflight", None)
            if job:
                self.enqueue({"key": key, "kind": "resume", "text": job.get("text", "")}, front=True)
        self.save()

    def catch_up(self):
        """Handle owner messages sent while the supervisor was down (since the last handled message)."""
        last = self.state["last_message_id"]
        if not last:
            return
        res = self.z._api("GET", "messages", {"anchor": last, "num_before": 0, "num_after": 100,
                                              "include_anchor": "false", "apply_markdown": "false",
                                              "narrow": json.dumps([{"operator": "channels", "operand": "public"}])})
        for m in res.get("messages", []):
            self.handle({"type": "message", "message": m})

    def run(self):
        # After a boot Zulip and Gitea take a minute or two; wait for them instead of crash-looping.
        for attempt in range(60):
            try:
                self.load_owners()
                self.g.task_issues(self.repo(self.cfg["default_product"]))
                break
            except Exception as e:
                log.warning("waiting for Zulip and Gitea (%s): %s", attempt + 1, str(e)[:200])
                time.sleep(10)
        # Agent processes of a previous supervisor process must not keep writing to sessions this one resumes.
        for user in [p["agent_user"] for p in self.products.values()] + [self.cfg["chat"]["agent_user"]]:
            self.agent_exec(user, ["stop-agents"], timeout=30)
        self.z.subscribe(self.streams())
        for name in self.products:
            self.g.ensure_labels(self.repo(name))
        self.provision_agents()
        self.settings.load()
        self.z.register()
        self.rebuild()
        self.catch_up()
        for i in range(3):
            threading.Thread(target=self.worker, args=(i,), name=f"worker-{i}", daemon=True).start()
        if "digest_day" not in self.state:  # first start: the first digest is the next scheduled one
            now = datetime.now(ZoneInfo(self.settings.get("digest.timezone")))
            if now.strftime("%H:%M") >= self.settings.get("digest.time"):
                self.state["digest_day"] = now.date().isoformat()
                self.save()
        threading.Thread(target=self.digest_loop, name="digest", daemon=True).start()
        threading.Thread(target=self.gates_loop, name="gates", daemon=True).start()
        log.info("supervisor running: streams %s", self.streams())
        for ev in self.z.events():
            try:
                self.handle(ev)
            except Exception:
                log.exception("event failed: %s", ev)
