"""Gitea REST API (the parts the supervisor uses). https://docs.gitea.com/api/"""
import base64
import urllib.parse

from .http import HttpError, call

STATE_LABELS = {
    "planning": "1d76db",
    "awaiting-approval": "fbca04",
    "building": "0e8a16",
    "checking": "d4c5f9",
    "done": "5319e7",
    "merged": "0052cc",
    "paused": "c5def5",
    "cancelled": "b60205",
}
TASK_LABEL = ("factory-task", "006b75")
BACKLOG_LABEL = ("backlog", "c2e0c6")  # an issue waiting to be started as a task (/backlog start <n>)
REPORT_LABEL = ("factory-report", "d93f0b")  # a problem with the factory itself, reported with /report


class Gitea:
    def __init__(self, api, token, public=None):
        self.base = api.rstrip("/")
        self.api = self.base + "/api/v1"
        self.public = (public or api).rstrip("/")
        self.headers = {"Authorization": f"token {token}", "Accept": "application/json"}
        self._labels = {}

    def _call(self, method, path, body=None, query=None, timeout=60):
        url = f"{self.api}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self._public_links(call(method, url, headers=self.headers, json_body=body, timeout=timeout))

    def _public_links(self, obj):
        """Gitea builds html_url from the request's host (127.0.0.1 here); the owner needs the tailnet address."""
        if isinstance(obj, list):
            return [self._public_links(x) for x in obj]
        if isinstance(obj, dict):
            return {k: (v.replace(self.base, self.public, 1) if k == "html_url" and isinstance(v, str)
                        else self._public_links(v)) for k, v in obj.items()}
        return obj

    # -- labels ---------------------------------------------------------------------------------------------------
    def ensure_labels(self, repo):
        existing = {lb["name"]: lb["id"] for lb in self._call("GET", f"repos/{repo}/labels", query={"limit": 100})}
        for name, color in [TASK_LABEL, BACKLOG_LABEL, *STATE_LABELS.items()]:
            if name not in existing:
                existing[name] = self._call("POST", f"repos/{repo}/labels", {"name": name, "color": "#" + color})["id"]
        self._labels[repo] = existing

    def label_ids(self, repo, names):
        if repo not in self._labels:
            self.ensure_labels(repo)
        return [self._labels[repo][n] for n in names]

    def set_state(self, repo, number, state):
        self._call("PUT", f"repos/{repo}/issues/{number}/labels",
                   {"labels": self.label_ids(repo, [TASK_LABEL[0], state])})

    # -- issues ---------------------------------------------------------------------------------------------------
    def create_issue(self, repo, title, body, state):
        return self._call("POST", f"repos/{repo}/issues",
                          {"title": title, "body": body, "labels": self.label_ids(repo, [TASK_LABEL[0], state])})

    def edit_issue(self, repo, number, **fields):
        return self._call("PATCH", f"repos/{repo}/issues/{number}", fields)

    def issue(self, repo, number):
        return self._call("GET", f"repos/{repo}/issues/{number}")

    def task_issues(self, repo):
        return self._call("GET", f"repos/{repo}/issues",
                          query={"labels": TASK_LABEL[0], "state": "all", "type": "issues", "limit": 50})

    def create_backlog_issue(self, repo, title, body):
        return self._call("POST", f"repos/{repo}/issues",
                          {"title": title, "body": body, "labels": self.label_ids(repo, [BACKLOG_LABEL[0]])})

    def backlog_issues(self, repo):
        """Open issues labelled backlog and not (yet) tasks."""
        out = self._call("GET", f"repos/{repo}/issues",
                         query={"labels": BACKLOG_LABEL[0], "state": "open", "type": "issues", "limit": 50})
        return [i for i in out if TASK_LABEL[0] not in {lb["name"] for lb in i.get("labels") or []}]

    def create_report(self, repo, title, body):
        """A factory problem report (/report): an issue labelled `factory-report` in the records repository."""
        existing = {lb["name"]: lb["id"] for lb in self._call("GET", f"repos/{repo}/labels", query={"limit": 100})}
        if REPORT_LABEL[0] not in existing:
            existing[REPORT_LABEL[0]] = self._call("POST", f"repos/{repo}/labels",
                                                   {"name": REPORT_LABEL[0], "color": "#" + REPORT_LABEL[1]})["id"]
        return self._call("POST", f"repos/{repo}/issues",
                          {"title": title, "body": body, "labels": [existing[REPORT_LABEL[0]]]})

    def open_reports(self, repo):
        return self._call("GET", f"repos/{repo}/issues",
                          query={"labels": REPORT_LABEL[0], "state": "open", "type": "issues", "limit": 50})

    def pull_files(self, repo, number):
        """Files a pull request changes (filename, previous_filename for renames, status), all pages."""
        out, page = [], 1
        while True:
            batch = self._call("GET", f"repos/{repo}/pulls/{number}/files", query={"page": page, "limit": 50}) or []
            out += batch
            if len(batch) < 50:
                return out
            page += 1

    def comment(self, repo, number, body):
        return self._call("POST", f"repos/{repo}/issues/{number}/comments", {"body": body})

    def comments(self, repo, number):
        return self._call("GET", f"repos/{repo}/issues/{number}/comments", query={"limit": 100})

    # -- branches and pull requests --------------------------------------------------------------------------------
    def branch(self, repo, name):
        try:
            return self._call("GET", f"repos/{repo}/branches/{urllib.parse.quote(name, safe='')}")
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    def find_pull(self, repo, head):
        for pr in self._call("GET", f"repos/{repo}/pulls", query={"state": "all", "limit": 50}):
            if pr["head"]["ref"] == head:
                return pr
        return None

    def create_pull(self, repo, head, base, title, body):
        return self._call("POST", f"repos/{repo}/pulls", {"head": head, "base": base, "title": title, "body": body})

    # -- files (settings and decision log in factory-records) ------------------------------------------------------
    def read_file(self, repo, path):
        try:
            res = self._call("GET", f"repos/{repo}/contents/{path}")
        except HttpError as e:
            if e.status == 404:
                return None, None
            raise
        return base64.b64decode(res["content"]).decode(), res["sha"]

    def write_file(self, repo, path, text, message, sha=None):
        body = {"content": base64.b64encode(text.encode()).decode(), "message": message}
        if sha:
            body["sha"] = sha
            return self._call("PUT", f"repos/{repo}/contents/{path}", body)
        return self._call("POST", f"repos/{repo}/contents/{path}", body)

    def delete_file(self, repo, path, sha, message):
        return self._call("DELETE", f"repos/{repo}/contents/{path}", {"sha": sha, "message": message})

    # -- gates (Gitea Actions) and merging ------------------------------------------------------------------------
    def runs(self, repo, sha):
        """Workflow runs of one commit (all workflows, push events and others). Gitea's API puts the run's id in
        html_url, but its web page is /actions/runs/<run_number> (an id link answers "Not found")."""
        res = self._call("GET", f"repos/{repo}/actions/runs", query={"head_sha": sha, "limit": 50})
        out = [r for r in (res or {}).get("workflow_runs", []) if r.get("head_sha") == sha]
        for r in out:
            if r.get("run_number") and r.get("html_url", "").endswith(f"/actions/runs/{r['id']}"):
                r["html_url"] = r["html_url"][:-len(str(r["id"]))] + str(r["run_number"])
        return out

    def run_jobs(self, repo, run_id):
        return (self._call("GET", f"repos/{repo}/actions/runs/{run_id}/jobs") or {}).get("jobs", [])

    def job_log(self, repo, job_id):
        out = self._call("GET", f"repos/{repo}/actions/jobs/{job_id}/logs", timeout=120)
        return out if isinstance(out, str) else ""

    def runners(self, org):
        """Runners that can take an organisation's jobs: the instance's (factory-core-1) and the organisation's own
        (the hands of its product); each with name, status and labels."""
        out = (self._call("GET", "admin/actions/runners") or {}).get("runners", [])
        return out + (self._call("GET", f"orgs/{org}/actions/runners") or {}).get("runners", [])

    def has_path(self, repo, path, ref):
        try:
            self._call("GET", f"repos/{repo}/contents/{path}", query={"ref": ref})
            return True
        except HttpError as e:
            if e.status == 404:
                return False
            raise

    def pull(self, repo, number):
        return self._call("GET", f"repos/{repo}/pulls/{number}")

    def merge_pull(self, repo, number, sha, title, message):
        """Merge commit, only if the head is still `sha`; deletes the task branch."""
        return self._call("POST", f"repos/{repo}/pulls/{number}/merge",
                          {"Do": "merge", "head_commit_id": sha, "MergeTitleField": title,
                           "MergeMessageField": message, "delete_branch_after_merge": True})
