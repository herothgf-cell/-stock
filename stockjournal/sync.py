"""Private GitHub Contents synchronization with three-way record reconciliation.

No remote operation follows a redirect. Tokens remain only in memory and HTTP
headers; journal payloads have a credential-field guard. Changes use GitHub's
content SHA compare-and-swap, with no automatic overwrite after a race.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .domain import TABLES, ValidationError, empty_state, validate_state


class SyncError(RuntimeError):
    pass


class SyncConflict(SyncError):
    def __init__(self, conflicts, remote_sha=None, local_fingerprint=None):
        super().__init__(f"{len(conflicts)} journal record(s) need a conflict choice")
        self.conflicts = conflicts
        self.remote_sha = remote_sha
        self.local_fingerprint = local_fingerprint


def _canonical(state):
    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _meaning(record):
    if record is None:
        return None
    if record.get("deleted"):
        return {"id": record["id"], "deleted": True}
    return {key: value for key, value in record.items() if key != "updated_at"}


def merge_states(base, local, remote, choices=None):
    """Merge whole records; omission cannot delete a record in the baseline.

    choices maps e.g. ``accounts/uuid`` to ``local`` or ``remote``. Callers must
    validate resulting financial state, because individually valid edits can
    interact (for example independent sales may jointly oversell a position).
    """
    base = empty_state() if base is None else base
    for state in (base, local, remote):
        validate_state(state)
    choices = {} if choices is None else choices
    if not isinstance(choices, dict) or any(value not in ("local", "remote") for value in choices.values()):
        raise SyncError("Conflict choices must select local or remote records")
    result, conflicts = empty_state(), []
    for table in TABLES:
        for id in sorted(set(base[table]) | set(local[table]) | set(remote[table])):
            baseline = base[table].get(id)
            left = local[table].get(id, baseline)
            right = remote[table].get(id, baseline)
            b, l, r = _meaning(baseline), _meaning(left), _meaning(right)
            path = f"{table}/{id}"
            if l == r:
                chosen = max((value for value in (left, right) if value is not None), key=lambda value: value.get("updated_at", ""), default=None)
            elif l == b:
                chosen = right
            elif r == b:
                chosen = left
            elif path in choices:
                chosen = left if choices[path] == "local" else right
            else:
                conflicts.append({"path": path, "local": copy.deepcopy(left), "remote": copy.deepcopy(right)})
                continue
            if chosen is not None:
                result[table][id] = copy.deepcopy(chosen)
    if conflicts:
        raise SyncConflict(conflicts)
    validate_state(result)
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise SyncError("GitHub API redirected the request; synchronization was stopped")


class _HTTPFailure(SyncError):
    def __init__(self, code):
        self.code = code
        message = "GitHub changed during synchronization; fetch and review again" if code in (409, 422) else f"GitHub API request failed (HTTP {code})"
        super().__init__(message)


class GitHubSync:
    MAX_BYTES = 20_000_000

    def __init__(self, repo, token, branch="main"):
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
            raise SyncError("Repository must be an owner/name pair on github.com")
        if not isinstance(token, str) or not token or any(char in token for char in "\r\n\x00"):
            raise SyncError("A valid GitHub token is required")
        if not isinstance(branch, str) or not branch or len(branch) > 200 or any(char in branch for char in "\r\n\x00"):
            raise SyncError("A valid branch name is required")
        self.repo, self.branch, self._token = repo, branch, token
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._root = f"https://api.github.com/repos/{repo}"

    def _request(self, method, path="", payload=None, *, accept="application/vnd.github+json"):
        url = self._root + path
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise SyncError("Only the HTTPS GitHub API is permitted")
        data = None if payload is None else _canonical(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json", "User-Agent": "PersonalStockJournal/1",
        })
        try:
            with self._opener.open(request, timeout=25) as response:
                final_url = urllib.parse.urlsplit(response.geturl())
                if final_url.scheme != "https" or final_url.netloc != "api.github.com" or response.geturl() != url:
                    raise SyncError("Unexpected GitHub API response destination")
                raw = response.read(self.MAX_BYTES * 2 + 1)
                if len(raw) > self.MAX_BYTES * 2:
                    raise SyncError("GitHub response exceeds the supported journal size")
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict):
                    raise SyncError("GitHub returned an invalid response")
                return result
        except urllib.error.HTTPError as exc:
            raise _HTTPFailure(exc.code) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise SyncError("Unable to reach the GitHub API; check the connection and retry") from None
        except (UnicodeError, json.JSONDecodeError):
            raise SyncError("GitHub returned an invalid JSON response") from None

    def _private(self):
        info = self._request("GET")
        if info.get("private") is not True:
            raise SyncError("Synchronization requires a private GitHub repository")
        if not isinstance(info.get("default_branch"), str) or not info["default_branch"]:
            raise SyncError("GitHub returned incomplete repository information")
        return info

    def _read_remote(self, base, info):
        try:
            content = self._request("GET", "/contents/journal.json?ref=" + urllib.parse.quote(self.branch, safe=""),
                                    accept="application/vnd.github.object+json")
        except _HTTPFailure as exc:
            if exc.code != 404:
                raise
            if base is not None:
                raise SyncError("The remote journal is missing despite an existing synchronization baseline") from None
            # A missing file and missing branch both return 404.  Before creating,
            # prove the branch exists; only an actually empty repository may use
            # its default branch without a branch object.
            try:
                self._request("GET", "/branches/" + urllib.parse.quote(self.branch, safe=""))
            except _HTTPFailure as branch_error:
                if branch_error.code not in (404, 409):
                    raise
                if self.branch != info["default_branch"]:
                    raise SyncError("An empty repository can only initialize its default branch") from None
                try:
                    self._request("GET", "/git/ref/heads/" + urllib.parse.quote(info["default_branch"], safe=""))
                except _HTTPFailure as default_error:
                    # GitHub documents 409 for an empty Git database. A 404
                    # alone may mean a missing branch in a nonempty repository.
                    if default_error.code != 409:
                        raise
                else:
                    raise SyncError("The selected branch is unavailable")
            return empty_state(), None
        sha = content.get("sha")
        if content.get("type") != "file" or not isinstance(sha, str) or not sha:
            raise SyncError("Remote journal must be a regular JSON file with a content SHA")
        size = content.get("size")
        if size is not None and (type(size) is not int or not 0 <= size <= self.MAX_BYTES):
            raise SyncError("Remote journal has an invalid or unsupported size")
        from_blob = content.get("encoding") == "none"
        if from_blob:
            if not re.fullmatch(r"[0-9a-fA-F]{40}", sha) or type(size) is not int:
                raise SyncError("Remote journal has invalid Git blob metadata")
            # Contents omits the body above 1 MB. Fetch that exact immutable
            # blob; never use remote-provided download_url or git_url values.
            content = self._request("GET", "/git/blobs/" + sha.lower())
            blob_sha = content.get("sha")
            if not isinstance(blob_sha, str) or blob_sha.lower() != sha.lower():
                raise SyncError("GitHub returned a different journal blob SHA")
            if type(content.get("size")) is not int or content["size"] != size:
                raise SyncError("GitHub returned inconsistent journal blob size metadata")
        if content.get("encoding") != "base64":
            raise SyncError("Remote journal content must use base64 encoding")
        try:
            raw = base64.b64decode("".join(content["content"].split()), validate=True)
            if len(raw) > self.MAX_BYTES:
                raise SyncError("Remote journal exceeds the supported size")
            if from_blob:
                if len(raw) != size:
                    raise SyncError("Decoded journal blob size differs from its metadata")
                digest = hashlib.sha1()
                digest.update(f"blob {len(raw)}\0".encode("ascii"))
                digest.update(raw)
                if digest.hexdigest() != sha.lower():
                    raise SyncError("Decoded journal content does not match its Git blob SHA")
            state = json.loads(raw.decode("utf-8"))
            validate_state(state)
        except (KeyError, TypeError, AttributeError, binascii.Error, UnicodeError, json.JSONDecodeError, ValidationError):
            raise SyncError("Remote journal is malformed or failed financial validation") from None
        return state, sha

    def synchronize(self, local, base, resolutions=None):
        validate_state(local)
        if base is not None:
            validate_state(base)
        fingerprint = hashlib.sha256(_canonical(local).encode("utf-8")).hexdigest()
        info = self._private()
        remote, remote_sha = self._read_remote(base, info)
        choices = None
        if resolutions is not None:
            if (not isinstance(resolutions, dict) or set(resolutions) != {"choices", "remote_sha", "local_fingerprint"}
                    or resolutions["remote_sha"] != remote_sha or resolutions["local_fingerprint"] != fingerprint):
                raise SyncError("Conflict choices are stale; synchronize and review the current records again")
            choices = resolutions["choices"]
        try:
            merged = merge_states(base, local, remote, choices)
        except SyncConflict as conflict:
            conflict.remote_sha, conflict.local_fingerprint = remote_sha, fingerprint
            raise
        except ValidationError:
            raise SyncError("Combined changes failed financial validation; review transactions before retrying") from None
        if merged == remote and remote_sha is not None:
            return {"state": merged, "sha": remote_sha}
        raw = (_canonical(merged) + "\n").encode("utf-8")
        if len(raw) > self.MAX_BYTES:
            raise SyncError("Journal exceeds the supported synchronization size")
        payload = {"message": "Synchronize personal stock journal", "content": base64.b64encode(raw).decode("ascii"), "branch": self.branch}
        if remote_sha is not None:
            payload["sha"] = remote_sha
        self._private()
        result = self._request("PUT", "/contents/journal.json", payload)
        sha = result.get("content", {}).get("sha") if isinstance(result.get("content"), dict) else None
        if not isinstance(sha, str) or not sha:
            raise SyncError("GitHub did not confirm the written journal; synchronize again before continuing")
        return {"state": merged, "sha": sha}
