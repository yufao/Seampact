"""Validate transfer of PPO state between frozen experiments.

Callers must independently bind the parent to a completed run and verify the
source fingerprints against an authenticated record of that run. No training
or checkpoint write occurs here.
"""
import copy
import ast
import hashlib
from pathlib import Path

from seampact.scheduling.capability_ppo import CHECKPOINT
from seampact.scheduling.capability_scheduler import CANDIDATE_DIM, FEATURE_SCHEMA, OBSERVATION_DIM

SEMANTIC_MODULES = ('capability_scheduler.py', 'capability_perception.py', 'learners.py')


def _semantic_digest(name, data):
    if name == 'capability_scheduler.py':
        # The parent-loading entry point is not part of feature or action
        # semantics; excluding only that method permits this migration itself.
        tree = ast.parse(data.decode('utf-8-sig'))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                node.body = [item for item in node.body if not (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == 'start_workload')]
        data = ast.dump(tree, include_attributes=False).encode()
    return hashlib.sha256(data).hexdigest()


def source_fingerprints():
    base = Path(__file__).resolve().parent
    return {name: _semantic_digest(name, (base / name).read_bytes()) for name in SEMANTIC_MODULES}


def transfer(policy, spec):
    """Load identical full state, or only actor weights after a reward change."""
    required = {'path', 'sha256', 'mode', 'parent_config_id', 'parent_objective_id',
                'parent_feature_schema', 'semantic_sources'}
    if not isinstance(spec, dict) or set(spec) != required:
        raise ValueError('Parent transfer specification is incomplete')
    if spec['mode'] not in {'full', 'actor'}:
        raise ValueError('Transfer mode must be full or actor')
    if spec['parent_feature_schema'] != FEATURE_SCHEMA or spec['semantic_sources'] != source_fingerprints():
        raise ValueError('Parent feature/action source identity differs from this implementation')
    learner = policy.learner
    if (not policy.training or learner.algorithm != 'ppo' or learner.rollout or learner.steps
            or learner.updates or learner.policy_version or learner.optimizers['policy'].state):
        raise ValueError('Transfer requires a freshly initialized training policy')
    path = Path(spec['path'])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    if before != spec['sha256']:
        raise ValueError('Parent checkpoint checksum differs')
    payload = learner.torch.load(path, map_location=learner.device, weights_only=True)
    if hashlib.sha256(path.read_bytes()).hexdigest() != before:
        raise ValueError('Parent checkpoint changed during loading')
    parent_identity = dict(schema=CHECKPOINT, config_id=spec['parent_config_id'],
                           objective_id=spec['parent_objective_id'], feature_schema=FEATURE_SCHEMA)
    if payload.get('checkpoint_metadata') != parent_identity:
        raise ValueError('Parent checkpoint metadata differs from the completed source protocol')
    if (payload.get('format_version') != 1 or payload.get('algorithm') != 'ppo'
            or payload.get('observation_dim') != OBSERVATION_DIM or payload.get('candidate_dim') != CANDIDATE_DIM
            or (payload.get('config') or {}).get('hidden_sizes') != learner.config['hidden_sizes']):
        raise ValueError('Parent actor dimensions or network shape differ')
    if spec['mode'] == 'full':
        if parent_identity != policy.identity:
            raise ValueError('Full transfer requires identical scheduler objective identity')
        if payload.get('config') != learner.config:
            raise ValueError('Full transfer requires identical learner configuration')
        policy.load(path, optimizer=True)
    else:
        if parent_identity['objective_id'] == policy.identity['objective_id']:
            raise ValueError('Use full transfer for an unchanged objective')
        networks = payload.get('networks') or {}
        if not isinstance(networks.get('actor'), dict):
            raise ValueError('Parent PPO actor is missing')
        learner.networks['actor'].load_state_dict(networks['actor'], strict=True)
        # Fresh value network, optimizer, counters, RNG and empty rollout remain
        # the new experiment's, never borrowed from the old reward.
    if hashlib.sha256(path.read_bytes()).hexdigest() != before:
        raise ValueError('Parent checkpoint changed during transfer')
    return dict(mode=spec['mode'], parent_sha256=before, parent_identity=copy.deepcopy(parent_identity),
                semantic_sources=copy.deepcopy(spec['semantic_sources']),
                transferred=('actor,value,optimizer,counters,rng' if spec['mode']=='full' else 'actor only'))
