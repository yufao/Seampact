import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from seampact.build import build_package
from seampact.evaluation import PackageRejected, canonical_hash, inspect_package, match_discovery_record
from seampact.inspection import Inspector


CODE = '''class Owner:
    async def evaluate(self, ctx, event):
        values = []
        for item in event.items:
            model = await ctx.get("model")
            result = await model(item)
            values.append(result)
        return values
'''


class PackageIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        book = dict(cells=[dict(cell_type="markdown", source=["original data explanation"]),
                           dict(cell_type="code", source=CODE.splitlines(keepends=True),
                                outputs=[{"text": "preserved data"}], execution_count=7)],
                    metadata={"original": True})
        (self.root / "original.ipynb").write_text(json.dumps(book), encoding="utf-8")
        inspector = Inspector.freeze(self.root, self.root / "snapshots", "case", ["original.ipynb"],
                                     "Owner.evaluate", "original sequential observations")
        self.built = build_package(inspector, dict(boundaries=[dict(id="item", mode="R", path="original.ipynb",
                    unit="cell:1", symbol="Owner.evaluate", loop_index=0)]), self.root, "fixture")
        self.archive = self.root / self.built["archive"]

    def tearDown(self):
        self.temporary.cleanup()

    def changed(self, mutate, extra=None):
        with zipfile.ZipFile(self.archive) as source:
            payloads = {name: source.read(name) for name in source.namelist() if name != "manifest.json"}
            manifest = json.loads(source.read("manifest.json"))
        mutate(payloads, manifest)
        manifest["artifact_hashes"] = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
        target = self.root / "changed.zip"
        with zipfile.ZipFile(target, "w") as bundle:
            for name, data in payloads.items():
                bundle.writestr(name, data)
            bundle.writestr("manifest.json", json.dumps(manifest))
            if extra:
                bundle.writestr(*extra)
        return target, hashlib.sha256(target.read_bytes()).hexdigest()

    def test_original_notebook_and_data_survive_independent_replay(self):
        package = inspect_package(self.archive, self.built["sha256"])
        self.assertEqual(package.receipt()["modes"], {"R": 1})
        self.assertFalse(package.receipt()["production_admitted"])
        original = json.loads(package.payloads["source/original.ipynb"])
        patched = json.loads(package.payloads["patched/original.ipynb"])
        self.assertEqual(original["metadata"], patched["metadata"])
        self.assertEqual(original["cells"][0], patched["cells"][0])
        self.assertEqual(original["cells"][1]["outputs"], patched["cells"][1]["outputs"])

    def test_wrong_external_digest_and_rehashed_adapter_are_rejected(self):
        with self.assertRaisesRegex(PackageRejected, "archive digest"):
            inspect_package(self.archive, "0" * 64)
        def mutate(payloads, manifest):
            payloads["adapter.py"] += b"\nraise RuntimeError('untrusted executable')\n"
        archive, expected = self.changed(mutate)
        with self.assertRaisesRegex(PackageRejected, "engine-owned"):
            inspect_package(archive, expected)

    def test_rehashed_algorithm_or_recipe_change_cannot_self_attest(self):
        def algorithm(payloads, manifest):
            book = json.loads(payloads["patched/original.ipynb"])
            book["cells"][1]["source"] = [line.replace("return values", "return [999]") for line in book["cells"][1]["source"]]
            payloads["patched/original.ipynb"] = json.dumps(book).encode()
        archive, expected = self.changed(algorithm)
        with self.assertRaisesRegex(PackageRejected, "independent replay"):
            inspect_package(archive, expected)
        def recipe(payloads, manifest):
            manifest["transformations"][0]["recipe"]["helper"] = "forged_helper"
        archive, expected = self.changed(recipe)
        with self.assertRaisesRegex(PackageRejected, "recipe"):
            inspect_package(archive, expected)

    def test_forged_verification_and_admission_are_rejected(self):
        def verification(payloads, manifest):
            value = json.loads(payloads["verification.json"])
            value["boundaries"][0]["runtime_admitted"] = True
            payloads["verification.json"] = json.dumps(value).encode()
        archive, expected = self.changed(verification)
        with self.assertRaisesRegex(PackageRejected, "candidate check"):
            inspect_package(archive, expected)
        archive, expected = self.changed(lambda payloads, manifest: manifest.update(admitted=True))
        with self.assertRaisesRegex(PackageRejected, "draft manifest"):
            inspect_package(archive, expected)

    def test_inventory_escape_duplicate_and_symlink_are_rejected(self):
        for name, data in (("undeclared.txt", b"extra"), ("../escape.py", b"extra"), ("adapter.py", b"duplicate")):
            with self.subTest(name=name):
                archive, expected = self.changed(lambda p, m: None, (name, data))
                with self.assertRaises(PackageRejected):
                    inspect_package(archive, expected)
        member = zipfile.ZipInfo("link")
        member.create_system = 3
        member.external_attr = 0o120777 << 16
        archive, expected = self.changed(lambda p, m: None, (member, b"target"))
        with self.assertRaisesRegex(PackageRejected, "symlink"):
            inspect_package(archive, expected)

    def test_discovery_record_must_match_the_actual_candidate(self):
        package = inspect_package(self.archive, self.built["sha256"])
        record = dict(status="COMPLETED", analysis_id="analysis", request_id="case", source_id="source",
            package={"id": self.built["id"], "sha256": package.sha256},
            candidate_sha256=canonical_hash(package.manifest["candidate"]),
            report_sha256=canonical_hash(package.report), final_source_version=package.request["source_version"])
        record["public_record_sha256"] = canonical_hash(record)
        self.assertEqual(match_discovery_record(package, record)["origin"], "API_SOURCE_AGENT")
        changed = dict(record, candidate_sha256="0" * 64)
        changed.pop("public_record_sha256")
        changed["public_record_sha256"] = canonical_hash(changed)
        with self.assertRaisesRegex(PackageRejected, "candidate or report"):
            match_discovery_record(package, changed)


if __name__ == "__main__":
    unittest.main()
