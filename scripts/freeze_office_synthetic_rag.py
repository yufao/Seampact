"""Freeze independent synthetic RAG reading cases, inputs and private scoring."""
import argparse, hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.office_rag_fixtures import TASKS

def digest(raw): return hashlib.sha256(raw).hexdigest()
def dump(path, obj):
    raw = (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.with_suffix(path.suffix + '.sha256').write_text(digest(raw) + '  ' + path.name + '\n', encoding='ascii')
    return digest(raw)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    root = args.output
    root.mkdir(parents=True, exist_ok=False)
    cases, grade, metadata = [], {}, []
    for i, (name, prompt, documents, answers) in enumerate(TASKS):
        split = 'train_pool' if i < 16 else 'development_pool' if i < 20 else 'evaluation'
        case_id = 'office-rag:' + name
        files = []
        for n, document in enumerate(documents):
            relative = f'documents/{name}-{n + 1}.txt'
            data = (document + '\n').encode('utf-8')
            path = root / 'runtime' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files.append(dict(path=relative, sha256=digest(data)))
        cases.append(dict(id=case_id, split=split, source='office_rag', family_id='office-rag:' + name,
                          original_task=case_id, variant='normal', prompt=prompt, documents=files, rag_adaptation=True))
        grade[case_id] = answers
        metadata.append(dict(id=case_id, split=split, category='rag', variant='normal',
                             family_id='office-rag:' + name, original_task=case_id,
                             novelty_stratum='fresh_family', exposure='new_fixture_not_run'))
    # These are separate questions over the same frozen source, not new families.
    second = {
        'purchase': ('When is delivery of the replacement scanner expected?', [['7 april', 'april 7']]),
        'travel': ('How much employer credit was applied to the train fare? Give dollars.', [['46']]),
        'room': ('Which room has 24 seats but is accessible only by stairs?', [['alder']]),
        'access': ('Who issues the temporary visitor badge after checking identity?', [['security']]),
        'inventory': ('How many headsets are not faulty, including those reserved for onboarding?', [['47']]),
        'incident': ('Which team coordinates the printer repair?', [['workplace support']]),
        'retention': ('In which month and year did the Elm contract end?', [['june 2024', '2024-06', '06/2024']]),
        'onboarding': ('What follows laptop handover in the onboarding checklist?', [['account orientation']]),
        'version': ('Which handbook version is the proposed revision awaiting review?', [['2.3']]),
        'support': ('Which support queue handles a broken screen?', [['device support']]),
        'survey': ('How many event respondents were undecided about attendance?', [['6', 'six']]),
        'delivery': ('Which loading dock is used for small parcels?', [['dock 1']]),
    }
    for name, (prompt, answers) in second.items():
        original = next(item for item in cases if item['id'] == 'office-rag:' + name)
        case_id = original['id'] + ':second-question'
        cases.append(dict(original, id=case_id, original_task=case_id, prompt=prompt))
        grade[case_id] = answers
        metadata.append(dict(id=case_id, split=original['split'], category='rag', variant='normal',
                             family_id=original['family_id'], original_task=case_id,
                             novelty_stratum='fresh_family', exposure='new_fixture_not_run'))
    grade_sha = dump(root / 'grading' / 'grading-manifest.json', dict(schema='seampact.office-rag-grading/1', answers=grade))
    runtime_sha = dump(root / 'runtime' / 'runtime-manifest.json', dict(schema='seampact.office-rag-runtime/1',
        source_identity='seampact-synthetic-office-rag/1', protocol=dict(max_model_turns=15, timeout_s=900), grading_sha256=grade_sha, cases=cases))
    dump(root / 'metadata.json', dict(schema='seampact.office-rag-metadata/1', source_identity='seampact-synthetic-office-rag/1',
        sources=dict(office_rag=dict(manifest=str((root / 'runtime' / 'runtime-manifest.json').resolve()), manifest_sha256=runtime_sha,
                                    grading_manifest=str((root / 'grading' / 'grading-manifest.json').resolve()), grading_sha256=grade_sha)), cases=metadata))
    print(json.dumps(dict(cases=len(cases), families=24, training=sum(c['split']=='train_pool' for c in cases), development=sum(c['split']=='development_pool' for c in cases), evaluation=sum(c['split']=='evaluation' for c in cases), status='PREPARED_NOT_RUN')))
if __name__ == '__main__': main()
