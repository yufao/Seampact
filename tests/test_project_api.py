"""Project file API round trips without models or deployment services."""
import base64
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.project_spaces import ProjectSpaces
from seampact.server import make_server
from seampact.store import Store


class ProjectAPIChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.projects = ProjectSpaces(self.temp.name, self.store)
        self.projects.default()
        platform = SimpleNamespace(projects=self.projects,
            cleanup_project=lambda body: self.projects.cleanup(body["project_id"], body))
        self.server = make_server(platform, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, body=None):
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            data = response.read()
            return json.loads(data) if response.headers.get_content_type() == "application/json" else data

    def test_binary_project_file_roundtrip_and_explicit_delete(self):
        project = self.request("/api/projects", {"name": "Office"})
        self.assertIn(project["id"], [p["id"] for p in self.request("/api/projects")["projects"]])
        query = "?project_id=" + project["id"] + "&area=data"
        raw = b"PK\x00\x01\xff workbook bytes"
        payload = dict(project_id=project["id"], area="data", path="input.xlsx",
                       content_base64=base64.b64encode(raw).decode())
        self.request("/api/project-files", payload)
        listing = self.request("/api/project-files" + query)
        self.assertEqual(listing["entries"][0]["path"], "input.xlsx")
        self.assertEqual(self.request("/api/project-file" + query + "&path=input.xlsx"), raw)
        self.request("/api/project-files/delete", {k: payload[k] for k in ("project_id", "area", "path")})
        self.assertEqual(self.request("/api/project-files" + query)["entries"], [])

    def test_upload_rejects_active_session_and_path_escape(self):
        self.projects.begin_task("default", "session", "task")
        payload = dict(project_id="default", conversation_id="session", area="workspace",
                       path="report.txt", content_base64=base64.b64encode(b"hello").decode())
        with self.assertRaises(HTTPError) as blocked:
            self.request("/api/project-files", payload)
        self.assertEqual(blocked.exception.code, 400)
        self.projects.finish_task("task")
        self.request("/api/project-files", payload)
        payload["path"] = "../../escape.txt"
        with self.assertRaises(HTTPError) as escaped:
            self.request("/api/project-files", payload)
        self.assertEqual(escaped.exception.code, 400)

    def test_history_filter_includes_legacy_only_in_default(self):
        for identity, project_id in (("old", None), ("one", "alpha"), ("two", "beta")):
            task = dict(id=identity, conversation_id=identity, messages=[], status="COMPLETED")
            if project_id:
                task["project_id"] = project_id
            self.store.save("task", identity, task)
        self.assertEqual([h["id"] for h in self.store.conversation_heads("default")], ["old"])
        self.assertEqual([h["id"] for h in self.store.conversation_heads("alpha")], ["one"])


if __name__ == "__main__":
    unittest.main()
