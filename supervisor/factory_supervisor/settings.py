"""Agent settings the owner manages from Zulip (/model, /thinking, /settings).

Defaults live in this repository (supervisor/config.yml `defaults`). The owner's changes are overrides stored in
Gitea, repository factory/factory-records, file settings/overrides.json, with every change appended to
decision-log.md there (who, when, old and new value). A local copy in the state directory covers Gitea being down.
Security settings (agent users, tokens, approval mode, sandboxing) are deliberately not in this table.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("settings")

THINKING = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
ROLES = ["smol", "slow", "plan"]
DURATION = re.compile(r"^(\d+)(m|h)$")


def _minutes(v):
    m = DURATION.match(v)
    if not m:
        raise ValueError("use minutes or hours, e.g. 20m or 1h")
    n = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    if not 1 <= n <= 180:
        raise ValueError("between 1m and 3h")
    return v


def _int_range(lo, hi):
    def check(v):
        n = int(v)
        if not lo <= n <= hi:
            raise ValueError(f"between {lo} and {hi}")
        return n
    return check


def _fraction(v):
    f = float(v.rstrip("%"))
    f = f / 100 if f > 1 else f
    if not 0 <= f <= 0.95:
        raise ValueError("between 0% (no new steps at all) and 95%")
    return round(f, 2)


def _thinking(v):
    if v not in THINKING:
        raise ValueError("one of " + ", ".join(THINKING))
    return v


def _clock(v):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", v)
    if not m:
        raise ValueError("a time of day HH:MM, e.g. 08:00")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _zone(v):
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("a time zone such as Asia/Riyadh") from None
    return v


class Settings:
    def __init__(self, sup):
        self.sup = sup
        self.defaults = dict(sup.cfg["defaults"])
        self.records = sup.cfg["gitea"]["records_repo"]
        self.cache_path = os.path.join(os.path.dirname(sup.cfg["state_file"]), "settings-overrides.json")
        self.overrides = {}
        self.lock = threading.Lock()
        self._models = (0.0, [])
        # key: (description, validator)
        self.keys = {
            "model": ("default model for new agent sessions", self.model_selector),
            "thinking": ("default thinking level", _thinking),
            **{f"role.{r}": (f"OMP '{r}' role model (--{r})", self.model_selector) for r in ROLES},
            "max_time.plan": ("time limit of a planning step", _minutes),
            "max_time.build": ("time limit of a build step", _minutes),
            "max_time.chat": ("time limit of one chat answer", _minutes),
            "concurrency": ("agent sessions running at the same time", _int_range(1, 3)),
            "pace_threshold": ("pause new steps at or above this share of the 5-hour usage window", _fraction),
            "pace.weekly_threshold": ("pause new steps at or above this share of the 7-day usage window", _fraction),
            "digest.time": ("time of day the daily digest is posted in #factory > digest", _clock),
            "digest.timezone": ("time zone of digest.time", _zone),
            "gates.max_fixes": ("times the agent gets a red gate's log to fix before the owner is asked",
                                _int_range(0, 5)),
            "gates.timeout": ("how long the factory waits for the gates of one push before asking the owner",
                              _minutes),
        }

    # -- storage --------------------------------------------------------------------------------------------------
    def load(self):
        try:
            text, _ = self.sup.g.read_file(self.records, "settings/overrides.json")
            self.overrides = json.loads(text) if text else {}
        except Exception as e:
            log.warning("settings from Gitea unavailable (%s); using the local copy", e)
            try:
                with open(self.cache_path) as f:
                    self.overrides = json.load(f)
            except FileNotFoundError:
                self.overrides = {}
        self.overrides = {k: v for k, v in self.overrides.items() if k in self.keys}
        self._save_local()

    def _save_local(self):
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.overrides, f, indent=2)
        os.replace(tmp, self.cache_path)

    def _persist(self, key, old, new, who, link):
        self._save_local()
        text, sha = self.sup.g.read_file(self.records, "settings/overrides.json")
        self.sup.g.write_file(self.records, "settings/overrides.json", json.dumps(self.overrides, indent=2) + "\n",
                              f"settings: {key} {old} -> {new} ({who})", sha)
        self.decision(f"Setting `{key}`: `{old}` → `{new}`", who, link)

    def decision(self, what, who, link):
        """Append one line to the decision log in factory-records."""
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text, sha = self.sup.g.read_file(self.records, "decision-log.md")
        if not text:
            text = "# Factory decision log\n\nWritten by the supervisor. Newest last.\n\n| When | Who | Decision | Source |\n|---|---|---|---|\n"
        line = f"| {stamp} | {who} | {what.replace('|', '/')} | [Zulip]({link}) |\n"
        self.sup.g.write_file(self.records, "decision-log.md", text + line, f"decision: {what[:60]}", sha)

    # -- values ---------------------------------------------------------------------------------------------------
    def get(self, key):
        return self.overrides.get(key, self.defaults.get(key))

    def set(self, key, raw, who, link):
        if key not in self.keys:
            raise ValueError(f"unknown or not changeable from chat: {key}")
        value = self.keys[key][1](raw)
        with self.lock:
            old = self.get(key)
            self.overrides[key] = value
            self._persist(key, old, value, who, link)
        return old, value

    def reset(self, key, who, link):
        if key not in self.keys:
            raise ValueError(f"unknown or not changeable from chat: {key}")
        with self.lock:
            old = self.get(key)
            self.overrides.pop(key, None)
            new = self.get(key)
            self._persist(key, old, f"{new} (default)", who, link)
        return old, new

    def show(self):
        lines = ["**Agent settings** (changeable: `/settings set <key> <value>`, `/settings reset <key>`)"]
        for key, (desc, _) in self.keys.items():
            mark = " *(changed)*" if key in self.overrides else ""
            lines.append(f"- `{key}` = `{self.get(key)}`{mark} — {desc}")
        fixed = self.sup.cfg["omp"]
        lines += ["", "**Fixed (security, not changeable from chat)**",
                  f"- approval mode `{fixed['approval_mode']}` inside the agent sandbox (unprivileged agent users, "
                  "one per product; chat read-only)",
                  "- profiles: one OMP profile per product (`--profile <product>`), `factory` for chat"]
        return "\n".join(lines)

    # -- models ---------------------------------------------------------------------------------------------------
    def models(self):
        """Models OMP can use with the current login, from `omp models --json` (cached 10 minutes)."""
        at, cached = self._models
        if cached and time.time() - at < 600:
            return cached
        out = self.sup.agent_exec(self.sup.cfg["chat"]["agent_user"], ["omp", "models", "--json"], timeout=90)
        models = [m["selector"] for m in json.loads(out)["models"] if m.get("kind", "chat") == "chat"]
        self._models = (time.time(), models)
        return models

    def model_selector(self, name):
        models = self.models()
        if name in models:
            return name
        matches = [m for m in models if m.split("/", 1)[-1] == name]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"unknown model `{name}`; see `/model list`")

    def omp_flags(self, target):
        """--model/--thinking/role flags for a new session of a task or chat (topic choice first)."""
        flags = []
        model = target.get("model") or self.get("model")
        if model:
            flags += ["--model", model]
        flags += ["--thinking", target.get("thinking") or self.get("thinking") or "low"]
        for r in ROLES:
            if self.get(f"role.{r}"):
                flags += [f"--{r}", self.get(f"role.{r}")]
        return flags
