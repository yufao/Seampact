"""Source-backed process context checks, using actual model-free subprocesses."""

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.execution_context import ContextIssue, explicit_import_roots, process_context
from seampact.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import NativeBindings, UnitCatalog


class ExecutionContextTests(unittest.TestCase):
    def freeze(self, files):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        root = base / "source"
        root.mkdir()
        for path, source in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        inspector = Inspector.freeze(root, base / "snapshots", "test", list(files), "test", "static")
        inspector.request["source_id"] = "test"
        return root, inspector

    def entry(self, **extra):
        return dict(id="run", kind="cli", path="app/run.py", symbol="__main__",
                    command=["python", "app/run.py", "--input", "{image}"], boundary_ids=[], **extra)

    def test_actual_cli_keeps_directory_argv_and_inherited_environment(self):
        root, inspector = self.freeze({"app/run.py": """
            import argparse, json, os, sys
            parser = argparse.ArgumentParser()
            parser.add_argument('--input', required=True)
            args = parser.parse_args()
            print(json.dumps(dict(cwd=os.getcwd(), argv=sys.argv[1:],
                                 env=os.environ['SEAMPACT_CONTEXT_FIXTURE'])))
        """, "app/README.md": "python run.py --input example.png\n"})
        entry = self.entry(cwd=dict(path="app", evidence=[dict(path="app/README.md", start=1, end=1)]))
        store = Store(":memory:")
        runtime = OwnerRuntime(Coordinator(store))
        previous = os.environ.get("SEAMPACT_CONTEXT_FIXTURE")
        os.environ["SEAMPACT_CONTEXT_FIXTURE"] = "literal $x & data"
        try:
            catalog = UnitCatalog(root, store, runtime)
            candidate = dict(entries=[entry], boundaries=[])
            checked = catalog.inspect_candidate(inspector, candidate)["entries"][0]
            self.assertEqual(checked["status"], "READY", checked["reason"])
            self.assertEqual(checked["execution_context"]["cwd"], "app")
            unit = catalog.install(inspector, candidate, "analysis", "install")[0]
            result = catalog.invoke(unit["id"], {"image": "space name; $(literal).png"})
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            self.assertEqual(Path(actual["cwd"]), root / "app")
            self.assertEqual(actual["argv"], ["--input", "space name; $(literal).png"])
            self.assertEqual(actual["env"], "literal $x & data")
        finally:
            if previous is None:
                os.environ.pop("SEAMPACT_CONTEXT_FIXTURE", None)
            else:
                os.environ["SEAMPACT_CONTEXT_FIXTURE"] = previous
            runtime.close()
            store.close()

    def test_docker_copy_maps_container_working_directory_to_original_source(self):
        _, inspector = self.freeze({"app/run.py": "print('fixture')\n", "Dockerfile": """
            FROM fixture
            ADD ./app /
            WORKDIR /
            ENTRYPOINT ["python", "run.py"]
        """})
        entry = self.entry(cwd=dict(path="app", evidence=[dict(path="Dockerfile", start=2, end=4)]))
        self.assertEqual(process_context(inspector, entry)["cwd"], "app")
        entry["cwd"]["path"] = "."
        with self.assertRaises(ContextIssue):
            process_context(inspector, entry)

    def test_missing_wrong_out_of_scope_and_partial_evidence_stay_missing(self):
        _, inspector = self.freeze({"app/run.py": "pass\n", "app/README.md": "python different.py\n"})
        specs = [dict(path="app"), dict(path="/app", evidence=[]), dict(path="../app", evidence=[]),
                 dict(path="C:/app", evidence=[]), dict(path="app", evidence=[dict(path="unknown", start=1, end=1)]),
                 dict(path="app", evidence=[dict(path="app/README.md", start=1, end=9)]),
                 dict(path="app", evidence=[dict(path="app/README.md", start=1, end=1)])]
        for spec in specs:
            with self.subTest(spec=spec), self.assertRaises(ContextIssue):
                process_context(inspector, self.entry(cwd=spec))

    def test_cli_shape_precedes_resources_with_typed_field_error(self):
        _, inspector = self.freeze({"app/run.py": "pass\n"})
        resources = Mock()
        resources.check_entry.side_effect = AssertionError("must not resolve resources")
        binding = NativeBindings(inspector, dict(entries=[], boundaries=[]), None, resource_binding=resources)
        for change, code, field in [(dict(command=[]), "cli_command_invalid", "command"),
                                    (dict(symbol="main"), "cli_symbol_invalid", "symbol"),
                                    (dict(command=["python", "app/run.py", "--input={image}"]), "cli_placeholder_invalid", "command")]:
            entry = self.entry()
            entry.update(change)
            with self.subTest(code=code), self.assertRaises(ContextIssue) as context:
                binding.describe(entry)
            self.assertEqual(context.exception.code, code)
            self.assertEqual(context.exception.field_paths, [field])
        resources.check_entry.assert_not_called()

    def test_unproven_completion_contract_precedes_resources(self):
        _, inspector = self.freeze({"app/run.py": "pass\n"})
        resources = Mock()
        resources.check_entry.side_effect = AssertionError("must not resolve resources")
        binding = NativeBindings(inspector, dict(entries=[], boundaries=[]), None, resource_binding=resources)
        with self.assertRaises(ContextIssue) as caught:
            binding.describe(self.entry(completion={"schema": "invalid"}))
        self.assertEqual(caught.exception.code, "completion_contract_unproven")
        self.assertEqual(caught.exception.field_paths, ["completion"])
        resources.check_entry.assert_not_called()

    def test_explicit_local_import_is_frozen_and_invoked_without_third_party_lookup(self):
        root, inspector = self.freeze({"scripts/stat.py": """
            import os, sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from seampact_fixture_statistics_metrics import score
            def run(value: str) -> str:
                return score(value)
        """, "scripts/seampact_fixture_statistics_metrics.py": "def score(value: str) -> str:\n    return 'local:' + value\n"})
        entry = dict(id="stats", path="scripts/stat.py", symbol="run", kind="function", boundary_ids=[])
        store = Store(":memory:")
        runtime = OwnerRuntime(Coordinator(store))
        try:
            catalog = UnitCatalog(root, store, runtime)
            candidate = dict(entries=[entry], boundaries=[])
            unit = catalog.install(inspector, candidate, "analysis", "install")[0]
            self.assertEqual(unit["status"], "READY", unit["reason"])
            self.assertEqual(catalog.invoke(unit["id"], {"value": "x"}), "local:x")
        finally:
            runtime.close()
            store.close()

    def test_pathlib_form_is_supported_but_shadowed_or_conditional_insert_is_not(self):
        _, inspector = self.freeze({"scripts/stat.py": """
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent))
        """, "scripts/shadowed.py": """
            import os, sys
            os = object()
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        """, "scripts/conditional.py": """
            import os, sys
            if False:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        """})
        self.assertEqual(explicit_import_roots(inspector, "scripts/stat.py")[0]["path"], "scripts")
        self.assertEqual(explicit_import_roots(inspector, "scripts/shadowed.py"), [])
        self.assertEqual(explicit_import_roots(inspector, "scripts/conditional.py"), [])


if __name__ == "__main__":
    unittest.main()
