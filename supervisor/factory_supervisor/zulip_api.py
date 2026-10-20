"""Zulip bot API: event queue, messages, users (REST, https://zulip.com/api/)."""
import base64
import json
import logging
import time
import urllib.parse

from .http import HttpError, call, fetch_bytes

log = logging.getLogger("zulip")


class Zulip:
    def __init__(self, site, email, api_key):
        self.site = site.rstrip("/")
        self.email = email
        token = base64.b64encode(f"{email}:{api_key}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}
        self.queue_id = None
        self.last_event_id = -1

    def _api(self, method, path, form=None, timeout=60):
        url = f"{self.site}/api/v1/{path}"
        if method == "GET" and form:
            url += "?" + urllib.parse.urlencode(form)
            form = None
        res = call(method, url, headers=self.headers, form=form, timeout=timeout)
        if isinstance(res, dict) and res.get("result") == "error":
            raise HttpError(400, json.dumps(res), url)
        return res

    # -- events ---------------------------------------------------------------------------------------------------
    def register(self):
        res = self._api("POST", "register", {
            "event_types": json.dumps(["message", "reaction"]),
            "apply_markdown": "false",
            "all_public_streams": "true",
        })
        self.queue_id = res["queue_id"]
        self.last_event_id = res["last_event_id"]
        log.info("event queue registered: %s", self.queue_id)

    def events(self):
        """Yield events forever (long polling); re-registers when the queue expires."""
        while True:
            if self.queue_id is None:
                try:
                    self.register()
                except Exception as e:  # Zulip restarting: retry
                    log.warning("register failed: %s", e)
                    time.sleep(10)
                    continue
            try:
                res = self._api("GET", "events", {"queue_id": self.queue_id, "last_event_id": self.last_event_id},
                                timeout=120)
            except HttpError as e:
                if "BAD_EVENT_QUEUE_ID" in e.body:
                    log.warning("event queue expired; re-registering")
                    self.queue_id = None
                    continue
                log.warning("events failed: %s", e)
                time.sleep(5)
                continue
            except Exception as e:
                log.warning("events failed: %s", e)
                time.sleep(5)
                continue
            for ev in res.get("events", []):
                self.last_event_id = max(self.last_event_id, ev["id"])
                if ev.get("type") != "heartbeat":
                    yield ev

    # -- messages -------------------------------------------------------------------------------------------------
    def send(self, stream, topic, content):
        content = content if len(content) <= 9500 else content[:9400] + "\n\n… (shortened)"
        res = self._api("POST", "messages", {"type": "stream", "to": stream, "topic": topic[:60], "content": content})
        return res.get("id")

    def react(self, message_id, emoji_name):
        try:
            self._api("POST", f"messages/{message_id}/reactions", {"emoji_name": emoji_name})
        except HttpError as e:
            log.debug("reaction failed: %s", e)

    def users(self):
        return self._api("GET", "users")["members"]

    def subscribe(self, streams, principals=None, description=None):
        """Subscribe the bot (or the given user ids) to streams; Zulip creates public streams that do not exist."""
        form = {"subscriptions": json.dumps([{"name": s, **({"description": description} if description else {})}
                                             for s in streams])}
        if principals:
            form["principals"] = json.dumps(principals)
        self._api("POST", "users/me/subscriptions", form)

    def stream_id(self, name):
        try:
            return self._api("GET", "get_stream_id", {"stream": name})["stream_id"]
        except HttpError:
            return None

    def archive_stream(self, name):
        sid = self.stream_id(name)
        if sid is not None:
            self._api("DELETE", f"streams/{sid}")

    def download(self, path, max_bytes):
        """Bytes of an uploaded file (/user_uploads/<realm>/<…>), via a temporary URL; None when over max_bytes."""
        rel = path.split("/user_uploads/", 1)[1]
        temp = self._api("GET", f"user_uploads/{rel}")["url"]
        return fetch_bytes(self.site + temp, max_bytes)

    def message_link(self, message_id):
        return f"{self.site}/#narrow/near/{message_id}"
