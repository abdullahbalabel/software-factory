"""One headless OMP process (`omp --mode rpc --no-ui`), driven over its JSONL protocol on stdin/stdout.

The process runs as an unprivileged agent user through `sudo -u <agent> factory-agent-exec omp …`; its stdin is the
RPC pipe (never a terminal), so OMP never waits for interactive input.
"""
import itertools
import json
import logging
import subprocess
import threading

log = logging.getLogger("omp")


class OmpExited(Exception):
    pass


class OmpSession:
    def __init__(self, argv, label, on_host_tool=None, log_file=None):
        self.label = label
        self.on_host_tool = on_host_tool
        # Own process group: sudo relays the supervisor's SIGTERM to OMP only when it comes from outside the group.
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=log_file if log_file else subprocess.DEVNULL, bufsize=0,
                                  start_new_session=True)
        self.cv = threading.Condition()
        self.write_lock = threading.Lock()
        self.responses = {}
        self.prompt_results = {}
        self.settled = 0
        self.alive = True
        self.ids = itertools.count(1)
        threading.Thread(target=self._read, name=f"omp-{label}", daemon=True).start()

    # -- transport ------------------------------------------------------------------------------------------------
    def _write(self, obj):
        data = (json.dumps(obj) + "\n").encode()
        with self.write_lock:
            try:
                self.p.stdin.write(data)
                self.p.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                raise OmpExited(f"{self.label}: OMP is not running") from None

    def _read(self):
        for raw in self.p.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "host_tool_call":
                threading.Thread(target=self._host_tool, args=(ev,), daemon=True).start()
                continue
            with self.cv:
                if t == "response" and ev.get("id"):
                    self.responses[ev["id"]] = ev
                elif t == "prompt_result" and ev.get("id"):
                    self.prompt_results[ev["id"]] = ev
                elif t == "session_settled":
                    self.settled += 1
                self.cv.notify_all()
        code = self.p.wait()
        log.info("%s: OMP exited with code %s", self.label, code)
        with self.cv:
            self.alive = False
            self.cv.notify_all()

    def _host_tool(self, ev):
        text, is_error = "host tool unavailable", True
        try:
            if self.on_host_tool:
                text, is_error = self.on_host_tool(ev.get("toolName"), ev.get("arguments") or {})
        except Exception as e:  # report to the agent, keep the session
            log.exception("host tool %s failed", ev.get("toolName"))
            text, is_error = f"error: {e}", True
        try:
            self._write({"type": "host_tool_result", "id": ev["id"], "isError": is_error,
                         "result": {"content": [{"type": "text", "text": text}]}})
        except OmpExited:
            pass

    def _wait(self, pred, timeout):
        with self.cv:
            ok = self.cv.wait_for(lambda: pred() or not self.alive, timeout=timeout)
            if pred():
                return
        raise OmpExited(f"{self.label}: OMP exited" if ok else f"{self.label}: timed out")

    # -- commands -------------------------------------------------------------------------------------------------
    def request(self, type_, timeout=120, **fields):
        rid = f"r{next(self.ids)}"
        self._write({"id": rid, "type": type_, **fields})
        self._wait(lambda: rid in self.responses, timeout)
        res = self.responses.pop(rid)
        if not res.get("success"):
            raise RuntimeError(f"{type_} failed: {res.get('error')}")
        return res.get("data")

    def prompt(self, message, timeout=None):
        """Send a prompt; wait until the agent yields and the session is settled. Returns the prompt_result frame."""
        rid = f"p{next(self.ids)}"
        settled_before = self.settled
        self._write({"id": rid, "type": "prompt", "message": message})
        self._wait(lambda: rid in self.responses, 120)
        ack = self.responses.pop(rid)
        if not ack.get("success"):
            raise RuntimeError(f"prompt rejected: {ack.get('error')}")
        if (ack.get("data") or {}).get("agentInvoked") is False:
            return {"status": "completed", "agentInvoked": False}
        self._wait(lambda: rid in self.prompt_results, timeout)
        result = self.prompt_results.pop(rid)
        if not result.get("sessionSettled"):
            self._wait(lambda: self.settled > settled_before, timeout)
        return result

    def steer(self, message):
        self.request("steer", message=message)

    def abort(self):
        try:
            self.request("abort", timeout=30)
        except Exception as e:
            log.warning("%s: abort failed: %s", self.label, e)

    def last_text(self):
        return (self.request("get_last_assistant_text") or {}).get("text") or ""

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.p.terminate()
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.p.kill()
