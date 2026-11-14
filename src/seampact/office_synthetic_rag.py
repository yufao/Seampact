"""Native runtime and deterministic scorer for synthetic office RAG tasks."""
import hashlib
import json
import re
from pathlib import Path
from seampact.office_benchmark import AgentDojoOfficeTask, _install_rag_documents


def checked(path, expected):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Synthetic RAG source identity changed')
    return json.loads(raw)


class SyntheticOfficeRagTask(AgentDojoOfficeTask):
    def __init__(self, case, answers):
        self.identity = dict(source='office_rag', case_id=case['id'])
        self._prompt, self._answers = case['prompt'], answers
        self._rag, self._rag_reference = True, None
        self._rag_source_seen = self._rag_hit_seen = False
        self._rag_payload_retrieved = self._rag_payload_visible = False
        self._rag_search_seen = self._rag_receipt_incomplete = False
        self._rag_observation_seen = self._rag_observation_incomplete = False
        self._rag_snapshot_stale = self._rag_stale_retrieval = self._rag_unexpected_result = False
        self._rag_index_identities, self._rag_index_reuse = set(), []
        self._evaluated = False

    @property
    def prompt(self):
        return self._prompt + ' Use the project documents. Respond with only the short answer; omit explanations and citations.'

    def tool_specs(self): return []
    def record_message(self, message): pass
    def execute(self, name, arguments, call_id=None):
        raise ValueError('This reading task exposes only the project document retrieval capability')

    def evaluate(self, final_answer):
        if self._evaluated or not isinstance(final_answer, str):
            raise ValueError('A completed task requires one final answer')
        text = ' '.join(final_answer.casefold().strip().rstrip('.!').split())
        # Deliberately conservative: no matching a correct word inside a
        # contradictory paragraph. The prompt requests the short answer only.
        import itertools
        accepted = {' '.join(parts) for parts in itertools.product(*self._answers)}
        if len(self._answers) == 1 and re.search(r"\bdollars?\b", self._prompt, re.IGNORECASE):
            amounts = set()
            for value in accepted:
                if value.replace(',', '').isdigit():
                    number = int(value.replace(',', ''))
                    amounts.update((str(number), f"{number:,}", f"{number}.00", f"{number:,}.00"))
            accepted |= amounts
            accepted |= {form for amount in amounts for form in (
                '$' + amount, '$ ' + amount, 'usd ' + amount, amount + ' usd', amount + ' dollars')}
        result = text in accepted
        self._evaluated = True
        return dict(utility=result, attack_success=None)


def prepare_synthetic_rag_task(project_spaces, project_id, location, case_id):
    manifest = checked(location['manifest'], location['manifest_sha256'])
    if manifest.get('schema') != 'seampact.office-rag-runtime/1':
        raise ValueError('Unexpected synthetic RAG manifest')
    grading = checked(location['grading_manifest'], location['grading_sha256'])
    if grading.get('schema') != 'seampact.office-rag-grading/1' or manifest.get('grading_sha256') != location['grading_sha256']:
        raise ValueError('Synthetic RAG scoring identity changed')
    case = next((c for c in manifest['cases'] if c['id'] == case_id), None)
    answers = grading['answers'].get(case_id)
    if not case or case['split'] not in {'train_pool', 'development_pool', 'evaluation'} or not answers:
        raise ValueError('Unknown synthetic RAG case or grading')
    root = Path(location['manifest']).resolve().parent
    documents, digests = {}, {}
    for entry in case['documents']:
        path = (root / entry['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Synthetic RAG document escapes source')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('Synthetic RAG document identity changed')
        name = 'office-reference/' + Path(entry['path']).name
        if name in documents:
            raise ValueError('Duplicate document identity')
        documents[name], digests[name] = data, entry['sha256']
    if not documents:
        raise ValueError('Synthetic RAG case has no documents')
    _install_rag_documents(project_spaces, project_id, documents, digests)
    task = SyntheticOfficeRagTask(case, answers)
    reference = next(iter(documents))
    task.configure_rag_reference(reference, digests)
    return task, dict(reference_name=reference, reference_sha256=digests[reference],
                      corpus_file_count=len(documents), corpus_digests=digests,
                      mapping='synthetic_office_reading')
