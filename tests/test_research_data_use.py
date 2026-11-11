import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from seampact.scenes.data_use import load_manifest, task_digest, validate_manifest, validate_tasks


def member(identity, split, *, allowed=None, groups=None, derived=None,
           dataset="example", revision="rev1", example_id=None, task=None, files=None):
    return dict(id=identity, upstream=dict(dataset=dataset, revision=revision,
                example_id=example_id or identity), group_ids=groups or ["group:" + identity],
                derived_from=derived or [], split=split,
                allowed_use=allowed or [split], expected_task_sha256=task_digest(task or {}),
                files=files or [])


def manifest(*rows):
    return dict(schema="research-data-manifest/1", members=list(rows))


class ResearchDataUseTests(unittest.TestCase):
    def test_purpose_and_allowed_use_are_both_required(self):
        data = manifest(member("train", "train"),
                        member("eval", "test", allowed=["validation", "test"]))
        self.assertEqual(validate_manifest(data, "train", ["train"])['selected_count'], 1)
        self.assertEqual(validate_manifest(data, "test", ["eval", "eval"])['selected_distinct_count'], 1)
        for purpose, selected in (("train", ["eval"]), ("validation", ["eval"]), ("test", ["train"])):
            with self.subTest(purpose=purpose, selected=selected), self.assertRaises(ValueError):
                validate_manifest(data, purpose, selected)

    def test_related_splits_are_rejected_transitively_across_all_members(self):
        data = manifest(member("a", "train", groups=["source:one"]),
                        member("b", "train", groups=["source:one", "family:one"]),
                        member("c", "test", groups=["family:one"]))
        with self.assertRaisesRegex(ValueError, "cross split"):
            validate_manifest(data, "train", ["a"])

    def test_lineage_and_upstream_identity_cross_split(self):
        for rows in (
            (member("original", "train"), member("modified", "test", derived=["original"])),
            (member("one", "train", derived=["external:source"]),
             member("two", "validation", derived=["external:source"])),
            (member("one", "train", revision="rev1", example_id="same"),
             member("two", "test", revision="rev2", example_id="same")),
        ):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "cross split"):
                validate_manifest(manifest(*rows), "train", [rows[0]["id"]])

    def test_missing_or_ambiguous_declarations_fail_closed(self):
        data = manifest(member("a", "train"))
        variants = []
        for field in ("derived_from", "group_ids", "upstream", "allowed_use", "split", "expected_task_sha256"):
            changed = copy.deepcopy(data)
            del changed["members"][0][field]
            variants.append(changed)
        variants.append(manifest(member("a", "train"), member("a", "train")))
        changed = copy.deepcopy(data)
        changed["members"][0]["allowed_use"] = ["train", "evaluation"]
        variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_manifest(changed, "train", ["a"])
        with self.assertRaises(ValueError):
            validate_manifest(data, "train", ["unknown"])
        with self.assertRaises(ValueError):
            validate_manifest(data, "unknown", ["a"])

    def test_manifest_bytes_are_checked_before_use(self):
        task = dict(id="request-1", data_member_id="a", text="示例")
        data = json.dumps(manifest(member("a", "train", task=task))).encode()
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_bytes(data)
            self.assertEqual(load_manifest(path, digest, "train", [task])["manifest_sha256"], digest)
            path.write_bytes(data + b" ")
            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                load_manifest(path, digest, "train", [task])

    def test_task_identity_fields_do_not_change_canonical_digest(self):
        first = {"id": "one", "data_member_id": "a", "text": "示例", "data": {"b": 2, "a": 1}}
        second = {"data": {"a": 1, "b": 2}, "text": "示例", "id": "two", "data_member_id": "a"}
        self.assertEqual(task_digest(first), task_digest(second))
        data = manifest(member("a", "train", task=first))
        self.assertEqual(validate_tasks(data, "train", [second])["selected_count"], 1)
        second["text"] = "更改"
        with self.assertRaisesRegex(ValueError, "task sha256 mismatch"):
            validate_tasks(data, "train", [second])
        with self.assertRaises(ValueError):
            task_digest({"value": float("nan")})

    def test_selected_declared_file_bytes_are_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.bin"
            path.write_bytes(b"input")
            entry = {"path": "input.bin", "sha256": hashlib.sha256(b"input").hexdigest()}
            task = {"id": "one", "data_member_id": "a", "input": "input.bin"}
            data = manifest(member("a", "train", task=task, files=[entry]))
            self.assertEqual(validate_tasks(data, "train", [task], base_path=tmp)["declared_files_checked"], 1)
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "file sha256 mismatch"):
                validate_tasks(data, "train", [task], base_path=tmp)


if __name__ == "__main__":
    unittest.main()
