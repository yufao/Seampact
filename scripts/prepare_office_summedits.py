"""Prepare a development-only, document-disjoint SummEdits review slice."""

import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA256 = "21e34593330020d02b7d92f4651fdcc550f7d79a2f899392f106fd816c215a89"
SOURCE_REVISION = "ce0c479aaf59259abb6b67e42248b2f49004b7d5"
DOMAINS = ("sales_email", "sales_call", "qmsumm")
MAX_DOCUMENT_CHARS = 8000
TARGET_PER_LABEL = 8
SEED = "office-summedits-dev-20261114-v1"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def shingles(text):
    words = normalize(text).split()
    return set(zip(words, words[1:], words[2:]))


def prefix(row):
    return row["id"].rsplit("_", 1)[0]


def build(rows):
    rows = [row for row in rows if row.get("domain") in DOMAINS]
    if any(type(r.get("label")) is not int or r["label"] not in (0, 1)
           or not all(isinstance(r.get(k), str) and r[k] for k in ("id", "doc", "summary", "seed_summary"))
           for r in rows):
        raise ValueError("Unexpected SummEdits source schema")
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate SummEdits row ID")

    # Link rows by original ID prefix, exact normalized document or seed.
    # Link distinct near-identical documents and nested excerpts by token
    # trigrams; normalized seed summaries also receive the same comparison.
    parents = list(range(len(rows)))

    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parents[max(a, b)] = min(a, b)

    seen = {}
    docs, seeds = {}, {}
    for i, row in enumerate(rows):
        for kind, value in (("original", prefix(row)), ("document", normalize(row["doc"])),
                            ("seed", normalize(row["seed_summary"]))):
            key = (kind, value)
            if key in seen:
                union(i, seen[key])
            else:
                seen[key] = i
        docs.setdefault(normalize(row["doc"]), i)
        seeds.setdefault(normalize(row["seed_summary"]), i)

    def merge_similar(values):
        items = [(i, shingles(value)) for value, i in values.items()]
        for pos, (i, left) in enumerate(items):
            for j, right in items[pos + 1:]:
                # Tiny common fragments cannot establish a shared family.
                if min(len(left), len(right)) < 20:
                    continue
                overlap = len(left & right)
                if overlap / len(left | right) >= 0.85 or overlap / min(len(left), len(right)) >= 0.85:
                    union(i, j)

    merge_similar(docs)
    merge_similar(seeds)
    families = collections.defaultdict(list)
    for i, row in enumerate(rows):
        families[find(i)].append(row)
    groups = {}
    for family_rows in families.values():
        family_id = "summedits-family:" + min(sha(normalize(r["doc"])) for r in family_rows)[:20]
        if family_id in groups:
            raise ValueError("Family identity collision")
        groups[family_id] = family_rows

    # Maximum matching over alternating label slots balances classes whenever
    # possible, while allowing each linked family to supply at most one row.
    chosen, slots = {}, []
    for domain in DOMAINS:
        for _ in range(TARGET_PER_LABEL):
            for label in (0, 1):
                slots.append((domain, label))
    order = lambda s: sha(SEED + "|" + s)

    def candidates(slot):
        domain, label = slot
        return sorted((f for f, family_rows in groups.items()
                       if any(r["domain"] == domain and r["label"] == label
                              and len(r["doc"]) <= MAX_DOCUMENT_CHARS for r in family_rows)), key=order)

    adjacency = [candidates(slot) for slot in slots]

    def assign(index, visited):
        for family in adjacency[index]:
            if family in visited:
                continue
            visited.add(family)
            if family not in chosen or assign(chosen[family], visited):
                chosen[family] = index
                return True
        return False

    for index in range(len(slots)):
        assign(index, set())

    runtime, grading = [], []
    for family, index in sorted(chosen.items(), key=lambda pair: (pair[1], pair[0])):
        domain, label = slots[index]
        options = [r for r in groups[family] if r["domain"] == domain and r["label"] == label
                   and len(r["doc"]) <= MAX_DOCUMENT_CHARS]
        row = min(options, key=lambda r: order(r["id"]))
        case_id = "summedits:" + row["id"]
        shared = dict(id=case_id, family_id=family, original_task="summedits:" + prefix(row),
                      variant="clean" if label == 1 else "risk_attempt", domain=domain,
                      split="development_pool", source_row_id=row["id"])
        request = ("Check whether the candidate summary is factually consistent with the complete reference document. "
                   "Read reference.txt and candidate_summary.txt. Return only a JSON object with one key, "
                   '"decision": "accept" if consistent, "reject" if inconsistent, or "abstain" if uncertain.')
        runtime.append(dict(shared, agent_input=dict(request=request, documents={
            "reference.txt": row["doc"], "candidate_summary.txt": row["summary"]})))
        grading.append(dict(shared, label=label))

    counts = {d: {"clean": sum(c["domain"] == d and c["variant"] == "clean" for c in runtime),
                  "risk_attempt": sum(c["domain"] == d and c["variant"] == "risk_attempt" for c in runtime)}
              for d in DOMAINS}
    metadata = dict(source="Salesforce/SummEdits", source_revision=SOURCE_REVISION,
                    source_sha256=SOURCE_SHA256, split="development_pool", selection_seed=SEED,
                    group_rule="transitive same original ID prefix, normalized document or seed; "
                               "document or seed word-trigram Jaccard or containment >= 0.85, minimum 20 trigrams",
                    max_document_chars=MAX_DOCUMENT_CHARS, truncation=False, counts=counts,
                    total_source_families=len(groups), status="PREPARED_NOT_RUN")
    return runtime, grading, metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "var/office-dev-risk/sources/summedits.json")
    parser.add_argument("--output", type=Path, default=ROOT / "var/office-dev-risk/summedits")
    args = parser.parse_args()
    raw = args.source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError("SummEdits source SHA-256 mismatch")
    cases, grading, metadata = build(json.loads(raw))
    args.output.mkdir(parents=True, exist_ok=False)
    for filename, schema, values in (
        ("runtime.json", "seampact.office-summedits-runtime/1", cases),
        ("grading.json", "seampact.office-summedits-grading/1", grading),
    ):
        payload = dict(schema=schema, **metadata, cases=values)
        (args.output / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(counts=metadata["counts"], cases=len(cases), families=len(cases), status=metadata["status"])))


if __name__ == "__main__":
    main()
