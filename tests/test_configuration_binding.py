"""Finite original CLI configuration bindings, including refusal cases."""

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.dependencies import check_requirements
from seampact.inspection import Inspector


MAIN = '''import argparse
import torch
from settings import config, update_config
parser = argparse.ArgumentParser()
parser.add_argument('--image', type=str)
parser.add_argument('--experiment', type=str, default='example')
parser.add_argument('opts', nargs=argparse.REMAINDER)
args = parser.parse_args()
update_config(config, args)
if config.TEST.MODEL_FILE:
    model_path = config.TEST.MODEL_FILE
else:
    raise ValueError('checkpoint missing')
checkpoint = torch.load(model_path)
'''
SETTINGS = '''from yacs.config import CfgNode as CN
config = CN()
config.TEST = CN()
config.TEST.MODEL_FILE = ''
def update_config(cfg, args):
    cfg.defrost()
    cfg.merge_from_file(f'{args.experiment}.yaml')
    if cfg.TEST.MODEL_FILE == '':
        cfg.merge_from_list(['TEST.MODEL_FILE', 'fallback.bin'])
    if args.opts:
        cfg.merge_from_list(args.opts)
    cfg.freeze()
'''


class ConfigurationBindingChecks(unittest.TestCase):
    def fixture(self, files=None, command=None, evidence=None, mapping=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        files = files or {'main.py': MAIN, 'settings.py': SETTINGS, 'example.yaml': "TEST:\n  MODEL_FILE: 'yaml.bin'\n"}
        for name, content in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', list(files), 'goal', 'result')
        entry = dict(id='target', kind='cli', path='main.py', symbol='__main__',
            command=command or ['python', 'main.py', '--image', '{image}', 'TEST.MODEL_FILE', '{checkpoint}'],
            fixed_arguments={'checkpoint': {'$dependency': dict(id='weights', field='path')}})
        requirement = dict(id='weights', kind='weight', configurable=True, required=True,
            read_time='process_start', owner_scope='original process', entry_ids=['target'],
            evidence=evidence if evidence is not None else [dict(path=p, start=1, end=len(s.splitlines())) for p, s in files.items()],
            mappings=[dict(entry_id='target', location='entry.fixed_arguments', parameter='checkpoint', field='path',
                **dict(dict(configuration_key='TEST.MODEL_FILE', cli_override={'parameter': 'opts'}), **(mapping or {})))])
        candidate = dict(entries=[entry], dependencies=[requirement])
        return inspector, candidate, root

    def check(self, **kwargs):
        inspector, candidate, _ = self.fixture(**kwargs)
        original = copy.deepcopy(candidate)
        row = check_requirements(inspector, candidate)[0]
        self.assertEqual(candidate, original)
        return row

    def test_final_original_override_has_ordered_proof(self):
        row = self.check()
        self.assertEqual(row['status'], 'MAPPING_CHECKED', row['reason'])
        proof = row['entry_checks']['target']['mapping_proofs'][0]['proof']
        self.assertEqual(proof['rule'], 'original_cli_final_yacs_override/1')
        self.assertEqual([f['method'] for f in proof['facts']], ['merge_from_file', 'merge_from_list', 'merge_from_list'])
        self.assertEqual(proof['facts'][-1]['selected_value'], 'SELECTED_WEIGHT_PATH')

    def test_other_literal_key_and_original_torch_import_alias_are_supported(self):
        replace = lambda text: text.replace('TEST', 'EVAL').replace('MODEL_FILE', 'CHECKPOINT')
        main = replace(MAIN).replace('import torch', 'from torch import load as read_weights').replace('torch.load(', 'read_weights(')
        files = {'main.py': main, 'settings.py': replace(SETTINGS), 'example.yaml': 'EVAL:\n  CHECKPOINT: yaml.bin\n'}
        row = self.check(files=files, command=['python', 'main.py', '--image', '{image}', 'EVAL.CHECKPOINT', '{checkpoint}'],
                         mapping={'configuration_key': 'EVAL.CHECKPOINT'})
        self.assertEqual(row['status'], 'MAPPING_CHECKED', row['reason'])

    def test_later_merge_assignment_and_branch_override_are_rejected(self):
        for tail in ("    cfg.merge_from_list(['TEST.MODEL_FILE', 'later.bin'])\n", "    cfg.TEST.MODEL_FILE = 'later.bin'\n", "    if args.experiment:\n        cfg.TEST.MODEL_FILE = 'later.bin'\n"):
            files = {'main.py': MAIN, 'settings.py': SETTINGS.replace('    cfg.freeze()', tail + '    cfg.freeze()'), 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
            self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_remainder_merge_in_unknown_branch_is_rejected(self):
        files = {'main.py': MAIN, 'settings.py': SETTINGS.replace('if args.opts:', 'if args.experiment:'), 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
        self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_alias_reassignment_and_misleading_yacs_names_are_rejected(self):
        for main, settings in ((MAIN.replace('update_config(config, args)', 'config = other\nupdate_config(config, args)'), SETTINGS),
                               (MAIN, SETTINGS.replace('from yacs.config import CfgNode as CN', 'from other import CfgNode as CN')),
                               (MAIN.replace('checkpoint =', "model_path = 'later.bin'\ncheckpoint ="), SETTINGS)):
            files = {'main.py': main, 'settings.py': settings, 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
            self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_later_source_mutation_and_config_escape_are_rejected(self):
        for statement in ("config.TEST.MODEL_FILE = 'later.bin'", 'change(config)', 'config.merge_from_file("extra.yaml")', 'change(config.TEST)', "args.opts.extend(['TEST.MODEL_FILE', 'later.bin'])"):
            files = {'main.py': MAIN.replace('checkpoint =', statement + '\ncheckpoint ='), 'settings.py': SETTINGS, 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
            self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_loader_must_be_the_complete_original_expression(self):
        for expression in (
                "(config.defrost(), config.merge_from_list(['TEST.MODEL_FILE', 'bad.bin']), torch.load(config.TEST.MODEL_FILE))",
                "(config.defrost(), setattr(config.TEST, 'MODEL_FILE', 'bad.bin'), torch.load(config.TEST.MODEL_FILE))",
                "(False and torch.load(model_path))",
                "[torch.load(model_path) for model_path in ['bad.bin']]",
                "(lambda: torch.load(model_path))()"):
            with self.subTest(expression=expression):
                files = {'main.py': MAIN.replace('torch.load(model_path)', expression), 'settings.py': SETTINGS,
                         'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
                self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_loader_options_cannot_rewrite_path_during_argument_evaluation(self):
        for option in (
                "map_location=(model_path := 'bad.bin')",
                "map_location=(config.defrost(), setattr(config.TEST, 'MODEL_FILE', 'bad.bin'))",
                "map_location=change(config.TEST)",
                "map_location=torch.device(change(config))",
                "**options"):
            with self.subTest(option=option):
                files = {'main.py': MAIN.replace('torch.load(model_path)', 'torch.load(model_path, ' + option + ')'),
                         'settings.py': SETTINGS, 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
                self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_direct_alias_and_original_device_loader_options_stay_supported(self):
        guard = "if config.TEST.MODEL_FILE:\n    model_path = config.TEST.MODEL_FILE\nelse:\n    raise ValueError('checkpoint missing')"
        for replacement in (guard, 'model_path = config.TEST.MODEL_FILE', ''):
            for option in ("map_location='cpu'", 'map_location=torch.device(device)'):
                main = MAIN.replace(guard, replacement).replace('checkpoint =', "device = 'cpu'\ncheckpoint =")
                if not replacement:
                    main = main.replace('torch.load(model_path)', 'torch.load(config.TEST.MODEL_FILE)')
                main = main.replace('torch.load(model_path)', 'torch.load(model_path, ' + option + ')')
                main = main.replace('torch.load(config.TEST.MODEL_FILE)', 'torch.load(config.TEST.MODEL_FILE, ' + option + ')')
                with self.subTest(replacement=replacement, option=option):
                    files = {'main.py': main, 'settings.py': SETTINGS, 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
                    self.assertEqual(self.check(files=files)['status'], 'MAPPING_CHECKED')

    def test_original_remainder_and_parser_behavior_cannot_be_replaced(self):
        for main in (
                MAIN.replace('nargs=argparse.REMAINDER', 'nargs=argparse.REMAINDER, type=change'),
                MAIN.replace('nargs=argparse.REMAINDER', "nargs=argparse.REMAINDER, action='append'"),
                MAIN.replace('nargs=argparse.REMAINDER', "nargs=argparse.REMAINDER, dest='other'"),
                MAIN.replace('ArgumentParser()', "ArgumentParser(prefix_chars='T')"),
                MAIN.replace('args = parser.parse_args()', 'parser.parse_args = other\nargs = parser.parse_args()'),
                MAIN.replace('args = parser.parse_args()', "changed = parser.set_defaults(opts=['TEST.MODEL_FILE', 'bad.bin'])\nargs = parser.parse_args()"),
                MAIN.replace('args = parser.parse_args()', 'action = parser._actions[-1]\naction.type = change\nargs = parser.parse_args()'),
                MAIN.replace('args = parser.parse_args()', 'alias = [parser]\nchange(alias[0])\nargs = parser.parse_args()'),
                MAIN.replace('args = parser.parse_args()', "setattr(argparse, 'REMAINDER', '*')\nargs = parser.parse_args()"),
                MAIN.replace('checkpoint =', "setattr(torch, 'load', other)\ncheckpoint =")):
            with self.subTest(main=main):
                files = {'main.py': main, 'settings.py': SETTINGS, 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
                self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_original_configuration_schema_and_freeze_methods_cannot_be_replaced(self):
        for statement in ("config.freeze = other", "config.merge_from_list = other",
                          "config.__dict__['freeze'] = other", "config.TEST.MODEL_FILE += 'bad'",
                          "if True:\n    config.TEST = CN()"):
            with self.subTest(statement=statement):
                settings = SETTINGS.replace('def update_config', statement + '\ndef update_config')
                files = {'main.py': MAIN, 'settings.py': settings, 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
                self.assertEqual(self.check(files=files)['status'], 'MISSING')
        for key in ('TEST.freeze', '__dict__.MODEL_FILE', 'items.MODEL_FILE'):
            files = {'main.py': MAIN.replace('TEST.MODEL_FILE', key), 'settings.py': SETTINGS.replace('TEST.MODEL_FILE', key),
                     'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
            self.assertEqual(self.check(files=files, command=['python', 'main.py', '--image', '{image}', key, '{checkpoint}'],
                                        mapping={'configuration_key': key})['status'], 'MISSING')

    def test_mutable_parent_aliases_and_import_monkeypatches_are_rejected(self):
        for main, settings in (
                (MAIN.replace('checkpoint =', "alias = config.TEST\nalias.MODEL_FILE = 'later.bin'\ncheckpoint ="), SETTINGS),
                (MAIN.replace('checkpoint =', "alias = [config.TEST]\nalias[0].MODEL_FILE = 'later.bin'\ncheckpoint ="), SETTINGS),
                (MAIN.replace('checkpoint =', 'change([config.TEST])\ncheckpoint ='), SETTINGS),
                (MAIN.replace('checkpoint =', 'alias: list = [args]\nchange(alias)\ncheckpoint ='), SETTINGS),
                (MAIN.replace('checkpoint =', 'torch.load = other\ncheckpoint ='), SETTINGS),
                (MAIN.replace('checkpoint =', 'alias = args\ncheckpoint ='), SETTINGS),
                (MAIN, SETTINGS.replace('def update_config', 'alias = config.TEST\nalias.MODEL_FILE = other\ndef update_config'))):
            files = {'main.py': main, 'settings.py': settings, 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
            self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_missing_evidence_is_not_replaced_by_source_availability(self):
        files = {'main.py': MAIN, 'settings.py': SETTINGS, 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
        for excluded in files:
            evidence = [dict(path=p, start=1, end=len(s.splitlines())) for p, s in files.items() if p != excluded]
            row = self.check(files=files, evidence=evidence)
            self.assertEqual(row['status'], 'MISSING')
            self.assertEqual(row['entry_checks']['target']['issues'][0]['issue_code'], 'evidence_incomplete')

    def test_dynamic_conflicting_or_nonfinal_remainder_is_rejected(self):
        for argv in (["{opts}", 'TEST.MODEL_FILE', '{checkpoint}'],
                     ['TEST.MODEL_FILE', 'old.bin', 'TEST.MODEL_FILE', '{checkpoint}'],
                     ['TEST.MODEL_FILE', '{checkpoint}', 'TEST.MODEL_FILE', '{other}'],
                     ['TEST.MODEL_FILE', '{checkpoint}', '--image', '{image}']):
            self.assertEqual(self.check(command=['python', 'main.py'] + argv)['status'], 'MISSING')

    def test_parser_and_namespace_identity_cannot_be_substituted(self):
        for main in (MAIN.replace('args = parser.parse_args()', 'args = other.parse_args()'),
                     MAIN.replace('update_config(config, args)', "args.opts = ['TEST.MODEL_FILE', 'bad.bin']\nupdate_config(config, args)"),
                     MAIN.replace("parser.add_argument('opts'", "other.add_argument('opts'"),
                     MAIN.replace('args = parser.parse_args()', "alias = parser\nalias.set_defaults(opts=['TEST.MODEL_FILE', 'bad.bin'])\nargs = parser.parse_args()")):
            files = {'main.py': main, 'settings.py': SETTINGS, 'example.yaml': 'TEST:\n  MODEL_FILE: yaml.bin\n'}
            self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_yaml_custom_tags_are_not_interpreted(self):
        files = {'main.py': MAIN, 'settings.py': SETTINGS, 'example.yaml': 'TEST:\n  MODEL_FILE: !!python/object:other {}\n'}
        self.assertEqual(self.check(files=files)['status'], 'MISSING')

    def test_lightweight_original_cli_receives_selected_path_and_keeps_loading(self):
        # Optional original worker dependencies; the platform checker needs neither.
        try:
            import yacs.config
        except ImportError:
            self.skipTest('optional original worker dependency yacs is unavailable')
        files = {'main.py': MAIN, 'settings.py': SETTINGS, 'example.yaml': "TEST:\n  MODEL_FILE: 'yaml.bin'\n"}
        inspector, candidate, root = self.fixture(files=files)
        self.assertEqual(check_requirements(inspector, candidate)[0]['status'], 'MAPPING_CHECKED')
        # A local torch stub observes the real original call; no model is downloaded.
        (root / 'torch.py').write_text("def load(path):\n    print('ORIGINAL_LOADER=' + path)\n    return {}\n", encoding='utf-8')
        selected = str(root / 'selected.bin')
        command = [sys.executable, str(root / 'main.py'), '--image', 'image.png', 'TEST.MODEL_FILE', selected]
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
        self.assertIn('ORIGINAL_LOADER=' + selected, result.stdout)
        self.assertEqual((root / 'example.yaml').read_text(encoding='utf-8'), files['example.yaml'])


if __name__ == '__main__':
    unittest.main()
