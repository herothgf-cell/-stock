import base64
import copy
import importlib
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


STAMP = "2026-01-01T00:00:00Z"


def account(name="Base"):
    return {"id": "a", "updated_at": STAMP, "deleted": False, "name": name, "broker": "Broker", "kind": "taxable", "currency": "USD"}


class Response:
    def __init__(self, value, url="https://api.github.com/repos/owner/journal"):
        self.body = json.dumps(value).encode()
        self.url = url
    def read(self, *args):
        return self.body
    def geturl(self):
        return self.url
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class ScriptedHTTP:
    """A strict HTTP transport substitute; actual merge/CAS code remains real."""
    def __init__(self, test, steps):
        self.test, self.steps, self.requests = test, list(steps), []
    def open(self, request, **kwargs):
        self.requests.append(request)
        self.test.assertTrue(self.steps, "Unexpected additional network request")
        method, suffix, response = self.steps.pop(0)
        self.test.assertEqual(request.get_method(), method)
        self.test.assertTrue(request.full_url.endswith(suffix), request.full_url)
        if isinstance(response, Exception):
            raise response
        return Response(response, request.full_url)


class SyncTests(unittest.TestCase):
    def setUp(self):
        try:
            self.d = importlib.import_module("stockjournal.domain")
            self.m = importlib.import_module("stockjournal.sync")
        except ModuleNotFoundError:
            self.fail("The synchronization implementation is missing")
        self.base = self.d.empty_state()
        self.base["accounts"]["a"] = account()

    def payload(self, s, sha="old-sha"):
        return {"type": "file", "encoding": "base64", "content": base64.b64encode(json.dumps(s).encode()).decode(), "sha": sha, "size": len(json.dumps(s))}

    def client(self, steps):
        transport = ScriptedHTTP(self, steps)
        with patch("stockjournal.sync.urllib.request.build_opener", return_value=transport):
            client = self.m.GitHubSync("owner/journal", "ghp_secret")
        return client, transport

    def test_merge_preserves_disjoint_edits_and_ignores_timestamp_only_changes(self):
        local, remote = copy.deepcopy(self.base), copy.deepcopy(self.base)
        local["accounts"]["a"]["name"] = "Local"
        remote["accounts"]["a"]["updated_at"] = "2026-02-01T00:00:00Z"
        remote["accounts"]["b"] = {**account("Remote"), "id": "b"}
        result = self.m.merge_states(self.base, local, remote)
        self.assertEqual(result["accounts"]["a"]["name"], "Local")
        self.assertIn("b", result["accounts"])

    def test_missing_record_does_not_delete_baseline(self):
        local = copy.deepcopy(self.base)
        local["accounts"].clear()
        self.assertIn("a", self.m.merge_states(self.base, local, self.base)["accounts"])

    def test_tombstone_propagates_and_conflicts_with_concurrent_edit(self):
        local, remote = copy.deepcopy(self.base), copy.deepcopy(self.base)
        local["accounts"]["a"] = {"id": "a", "updated_at": STAMP, "deleted": True}
        self.assertTrue(self.m.merge_states(self.base, local, remote)["accounts"]["a"]["deleted"])
        remote["accounts"]["a"]["name"] = "Edited"
        with self.assertRaises(self.m.SyncConflict) as cm:
            self.m.merge_states(self.base, local, remote)
        self.assertEqual(cm.exception.conflicts[0]["path"], "accounts/a")
        merged = self.m.merge_states(self.base, local, remote, {"accounts/a": "remote"})
        self.assertEqual(merged["accounts"]["a"]["name"], "Edited")

    def test_private_guard_blocks_all_contents_reads_on_public_repo(self):
        client, transport = self.client([("GET", "/repos/owner/journal", {"private": False, "default_branch": "main"})])
        with self.assertRaises(self.m.SyncError):
            client.synchronize(self.base, None)
        self.assertEqual(len(transport.requests), 1)

    def test_cas_upload_includes_latest_remote_sha_and_merged_state(self):
        local = copy.deepcopy(self.base)
        local["accounts"]["a"]["name"] = "Changed"
        client, transport = self.client([
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", self.payload(self.base)),
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("PUT", "/contents/journal.json", {"content": {"sha": "new-sha"}}),
        ])
        result = client.synchronize(local, self.base)
        self.assertEqual(result["sha"], "new-sha")
        body = json.loads(transport.requests[-1].data)
        self.assertEqual(body["sha"], "old-sha")
        written = json.loads(base64.b64decode(body["content"]))
        self.assertEqual(written["accounts"]["a"]["name"], "Changed")

    def test_deleted_remote_file_never_recreates_over_known_baseline(self):
        missing = HTTPError("https://api.github.com", 404, "Not Found", {}, io.BytesIO(b""))
        client, transport = self.client([
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", missing),
        ])
        with self.assertRaises(self.m.SyncError):
            client.synchronize(self.base, self.base)
        self.assertEqual(len(transport.requests), 2)

    def test_resolution_is_bound_to_local_content_and_remote_sha(self):
        local, remote = copy.deepcopy(self.base), copy.deepcopy(self.base)
        local["accounts"]["a"]["name"] = "Local"
        remote["accounts"]["a"]["name"] = "Remote"
        client, _ = self.client([
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", self.payload(remote)),
        ])
        with self.assertRaises(self.m.SyncConflict) as cm:
            client.synchronize(local, self.base)
        conflict = cm.exception
        self.assertEqual(conflict.remote_sha, "old-sha")
        self.assertTrue(conflict.local_fingerprint)
        envelope = {"choices": {"accounts/a": "local"}, "remote_sha": conflict.remote_sha, "local_fingerprint": conflict.local_fingerprint}
        client, _ = self.client([
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", self.payload(remote, "changed-sha")),
        ])
        with self.assertRaises(self.m.SyncError):
            client.synchronize(local, self.base, envelope)

    def test_cas_failure_is_not_silently_retried_or_overwritten(self):
        local = copy.deepcopy(self.base)
        local["accounts"]["a"]["name"] = "Local"
        collision = HTTPError("https://api.github.com", 409, "Conflict", {}, io.BytesIO(b"secret server data"))
        client, transport = self.client([
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", self.payload(self.base)),
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("PUT", "/contents/journal.json", collision),
        ])
        with self.assertRaises(self.m.SyncError) as cm:
            client.synchronize(local, self.base)
        self.assertNotIn("secret", str(cm.exception))
        self.assertEqual(len(transport.requests), 4)

    def test_network_errors_never_expose_token_or_server_body(self):
        client, _ = self.client([("GET", "/repos/owner/journal", URLError("ghp_secret leaked diagnostic"))])
        with self.assertRaises(self.m.SyncError) as cm:
            client.synchronize(self.base, None)
        self.assertNotIn("ghp_secret", str(cm.exception))

    def test_repo_names_cannot_change_api_host(self):
        for repo in ("https://evil.test/a/b", "a/b/c", "../x", "a/b?token=1"):
            with self.subTest(repo=repo), self.assertRaises(self.m.SyncError):
                self.m.GitHubSync(repo, "secret")

    def test_repository_becoming_public_blocks_the_write(self):
        local = copy.deepcopy(self.base)
        local["accounts"]["a"]["name"] = "Local"
        client, transport = self.client([
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", self.payload(self.base)),
            ("GET", "/repos/owner/journal", {"private": False, "default_branch": "main"}),
        ])
        with self.assertRaises(self.m.SyncError):
            client.synchronize(local, self.base)
        self.assertEqual(len(transport.requests), 3)

    def test_empty_repository_initializes_only_the_default_branch(self):
        missing = HTTPError("https://api.github.com", 404, "Not Found", {}, io.BytesIO(b""))
        empty = HTTPError("https://api.github.com", 409, "Empty", {}, io.BytesIO(b""))
        client, transport = self.client([
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", missing),
            ("GET", "/branches/main", missing),
            ("GET", "/git/ref/heads/main", empty),
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("PUT", "/contents/journal.json", {"content": {"sha": "first-sha"}}),
        ])
        self.assertEqual(client.synchronize(self.base, None)["sha"], "first-sha")
        self.assertNotIn("sha", json.loads(transport.requests[-1].data))

    def test_missing_default_reference_is_not_proof_of_an_empty_repo(self):
        missing = HTTPError("https://api.github.com", 404, "Not Found", {}, io.BytesIO(b""))
        client, transport = self.client([
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", missing),
            ("GET", "/branches/main", missing),
            ("GET", "/git/ref/heads/main", missing),
        ])
        with self.assertRaises(self.m.SyncError):
            client.synchronize(self.base, None)
        self.assertEqual(len(transport.requests), 4)

    def test_redirect_handler_refuses_to_forward_credentials(self):
        request = self.m.urllib.request.Request("https://api.github.com/repos/owner/journal", headers={"Authorization": "Bearer secret"})
        with self.assertRaises(self.m.SyncError):
            self.m._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://evil.test/capture")

    def test_malformed_remote_state_is_not_replaced(self):
        bad = copy.deepcopy(self.base)
        bad["access_token"] = "private"
        client, transport = self.client([
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", self.payload(bad)),
        ])
        with self.assertRaises(self.m.SyncError):
            client.synchronize(self.base, None)
        self.assertEqual(len(transport.requests), 2)


if __name__ == "__main__":
    unittest.main()
