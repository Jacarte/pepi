"""Offline contract tests for the frozen custom Verified20 v1 preset."""
import copy
import io
import json
import random
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr
from collections import Counter
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from evals.swebench import __main__ as cli

from evals.swebench.common import (DATASET, fingerprint, read_json, read_manifest,
                                    runtime_record, sha256_file, write_json)
from evals.swebench.prepare import prepare_run, sanitize_tasks, selected_ids
from evals.swebench.preset import (DEFAULT_IDS, DEFINITION, IDS_SHA256, METADATA, definition,
                                   select_metadata, selection_record, tasklist_hash)
from evals.swebench.reporting import compare, file_hash, predictions, summarize_run
from evals.swebench.tests.test_reporting import ReportingTests
from evals.swebench.tests.test_runner import TASK, VERSIONS, prepare_fixture


def preset_tasks():
    repos = {row['instance_id']: row['repo'] for row in read_json(METADATA)}
    return [dict(TASK, instance_id=instance_id, repo=repos[instance_id])
            for instance_id in selected_ids(DEFAULT_IDS)]


class PresetTests(unittest.TestCase):
    def test_frozen_membership_hash_count_and_five_by_four(self):
        frozen = definition()
        ids = selected_ids(DEFAULT_IDS)
        metadata = read_json(METADATA)
        self.assertEqual((len(ids), len(set(ids))), (20, 20))
        self.assertEqual(len({row['instance_id'] for row in metadata}), 500)
        self.assertTrue(set(ids) <= {row['instance_id'] for row in metadata})
        self.assertEqual(tasklist_hash(ids), IDS_SHA256)
        self.assertEqual(sha256_file(DEFAULT_IDS), IDS_SHA256)
        self.assertEqual(list(Counter(row['repo'] for row in preset_tasks()).values()), [4] * 5)
        self.assertEqual(list(dict.fromkeys(row['repo'] for row in preset_tasks())),
                         frozen['selected_repositories'])
        self.assertEqual(frozen['task_repo_revision'], '3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d')
        self.assertEqual(frozen['id'], 'custom-verified20-v1')
        self.assertIn('not an official', frozen['label'])

    def test_metadata_selection_reproduces_under_shuffle_and_ignores_other_fields(self):
        metadata = read_json(METADATA)
        for row in metadata:
            self.assertEqual(set(row), {'instance_id', 'repo', 'datasets', 'split'})
            row.update(patch=object(), test_patch=object(), outcome=object())
        metadata.append({'instance_id': 'ignored', 'repo': 'ignored', 'datasets': [], 'split': 'test'})
        for seed in (1, 42, 999):
            random.Random(seed).shuffle(metadata)
            self.assertEqual(select_metadata(metadata), selected_ids(DEFAULT_IDS))

    def test_universe_drift_duplicates_dataset_and_split_are_rejected(self):
        metadata = read_json(METADATA)
        variations = [metadata[:-1], metadata + metadata[:1], metadata[:-1] + metadata[:1]]
        for key, value in [('datasets', []), ('split', 'train')]:
            changed = copy.deepcopy(metadata)
            changed[0][key] = value
            variations.append(changed)
        for rows in variations:
            with self.subTest(size=len(rows)), self.assertRaisesRegex(ValueError, 'universe drift'):
                select_metadata(rows)
        for task in [dict(TASK, datasets=[]), dict(TASK, split='train')]:
            with self.assertRaises(ValueError):
                sanitize_tasks([task], [task['instance_id']])

    def test_repository_eligibility_top_five_and_lexical_ties(self):
        # 500 unique metadata rows; ties must be lexical, <4 is ineligible.
        counts = {'z/top': 471, 'b/tie': 5, 'a/tie': 5, 'c/tie': 5,
                  'd/tie': 5, 'e/tie': 5, 'small/repo': 3, 'tiny/repo': 1}
        rows = [dict(instance_id=f'{repo.replace("/", "__")}-{i}', repo=repo,
                     datasets=[DATASET], split='test')
                for repo, count in counts.items() for i in range(count)]
        chosen = select_metadata(list(reversed(rows)))
        by_id = {row['instance_id']: row['repo'] for row in rows}
        self.assertEqual(list(dict.fromkeys(by_id[i] for i in chosen)),
                         ['z/top', 'a/tie', 'b/tie', 'c/tie', 'd/tie'])
        # Force digest ties to exercise the instance_id tie-break explicitly.
        with patch('evals.swebench.preset.hashlib.sha256') as digest:
            digest.return_value.hexdigest.return_value = '0' * 64
            selected = select_metadata(rows)
        self.assertEqual(selected[:4], sorted(row['instance_id'] for row in rows
                                              if row['repo'] == 'z/top')[:4])

    def test_frozen_files_tamper_is_rejected(self):
        for symbol, source in [('DEFAULT_IDS', DEFAULT_IDS), ('DEFINITION', DEFINITION), ('METADATA', METADATA)]:
            with tempfile.TemporaryDirectory() as directory:
                altered = Path(directory) / source.name
                altered.write_bytes(source.read_bytes() + b' ')
                with patch(f'evals.swebench.preset.{symbol}', altered), self.assertRaisesRegex(ValueError, 'hash mismatch'):
                    definition()

    def test_source_dataset_repo_and_order_classification(self):
        tasks, frozen = preset_tasks(), definition()
        ids = selected_ids(DEFAULT_IDS)
        selection = selection_record(ids, tasks, DATASET, frozen['task_repo_revision'])
        self.assertEqual(selection['preset'], frozen)
        for dataset, revision in [(DATASET, 'b' * 40), ('other', frozen['task_repo_revision'])]:
            with self.assertRaises(ValueError):
                selection_record(ids, tasks, dataset, revision)
        wrong_repo = copy.deepcopy(tasks)
        wrong_repo[0]['repo'] = 'other/repo'
        with self.assertRaises(ValueError):
            selection_record(ids, wrong_repo, DATASET, frozen['task_repo_revision'])
        self.assertNotIn('preset', selection_record(ids[::-1], tasks[::-1], DATASET, 'b' * 40))
        self.assertNotIn('preset', selection_record(ids[:1], tasks[:1], DATASET, None))

    def prepared(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        prepare_fixture(base)
        run = base / 'verified20'
        prepare_run(base / 'pepi', run, preset_tasks(), selected_ids(DEFAULT_IDS), VERSIONS,
                    'node:22-fixed', task_revision=definition()['task_repo_revision'])
        return run

    def test_prepare_freezes_selection_without_gold_and_requires_fresh_run(self):
        run = self.prepared()
        manifest = read_manifest(run)
        self.assertEqual(manifest['selection'], read_json(run / 'build/selection.json'))
        self.assertEqual(manifest['selection']['tasklist_sha256'], IDS_SHA256)
        self.assertEqual([task['instance_id'] for task in manifest['tasks']], selected_ids(DEFAULT_IDS))
        self.assertNotIn('SECRET', json.dumps(manifest))
        with self.assertRaises(FileExistsError):
            prepare_run(run.parent / 'pepi', run, preset_tasks(), selected_ids(DEFAULT_IDS), VERSIONS,
                        'node:22-fixed', task_revision=definition()['task_repo_revision'])

    def test_manifest_classification_source_hash_and_repo_tamper_are_rejected(self):
        for change in ('label', 'source', 'hash', 'repo', 'drop'):
            run = self.prepared()
            manifest = read_json(run / 'manifest.json')
            if change == 'label':
                manifest['selection']['preset']['label'] = 'Official score'
            elif change == 'source':
                manifest['task_repo_revision'] = 'b' * 40
            elif change == 'hash':
                manifest['selection']['tasklist_sha256'] = 'b' * 64
            elif change == 'repo':
                manifest['tasks'][0]['repo'] = 'other/repo'
            else:
                del manifest['selection']
            write_json(run / 'manifest.json', manifest)
            with self.subTest(change=change), self.assertRaises(ValueError):
                read_manifest(run)


class PresetEntryPointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ids = self.root / DEFAULT_IDS.name
        self.definition = self.root / DEFINITION.name
        self.metadata = self.root / METADATA.name
        for destination, source in ((self.ids, DEFAULT_IDS), (self.definition, DEFINITION),
                                    (self.metadata, METADATA)):
            destination.write_bytes(source.read_bytes())
        self.tasks = preset_tasks()
        self.source_revision = definition()['task_repo_revision']
        prepare_fixture(self.root)
        self.argv = ['prepare', '--pepi-root', str(self.root / 'pepi'),
                     '--task-repo', str(self.root / 'task-source'), '--out', str(self.root / 'new-run'),
                     '--pi-version', VERSIONS['pi'], '--litellm-version', VERSIONS['pi-provider-litellm'],
                     '--subagents-version', VERSIONS['pi-subagents'], '--node-image', 'node:22-fixed']
        self.loader = Mock(side_effect=lambda _root, ids: [task for task in self.tasks
                                                         if task['instance_id'] in ids])
        repo_module = ModuleType('swebench.task.repo')
        repo_module.load_task_repo = self.loader
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(sys.modules, {'swebench': ModuleType('swebench'),
                                                     'swebench.task': ModuleType('swebench.task'),
                                                     'swebench.task.repo': repo_module}))
        stack.enter_context(patch.object(cli, 'DEFAULT_IDS', self.ids))
        stack.enter_context(patch('evals.swebench.preset.DEFAULT_IDS', self.ids))
        stack.enter_context(patch('evals.swebench.preset.DEFINITION', self.definition))
        stack.enter_context(patch('evals.swebench.preset.METADATA', self.metadata))
        stack.enter_context(patch.object(cli, 'git_revision', return_value=self.source_revision))
        stack.enter_context(patch.object(cli.signal, 'signal'))

    def test_removed_or_reordered_default_fails_before_task_loading_for_all_path_routes(self):
        original = self.ids.read_text().splitlines()
        alias = self.root / 'default-alias.ids'
        alias.symlink_to(self.ids)
        # Explicit absolute DEFAULT_IDS is also the shell's default forwarding route.
        for ids in (original[:-1], list(reversed(original))):
            self.ids.write_text('\n'.join(ids) + '\n')
            for flags in ([], ['--ids', str(self.ids)], ['--ids', str(alias)]):
                stderr = io.StringIO()
                with self.subTest(count=len(ids), flags=flags), redirect_stderr(stderr):
                    self.assertEqual(cli.main(self.argv + flags), 2)
                self.assertIn('Frozen Verified20 IDs hash mismatch', stderr.getvalue())
                self.loader.assert_not_called()
                self.assertFalse((self.root / 'new-run').exists())

    def test_default_definition_and_metadata_corruption_fail_before_task_loading(self):
        for path in (self.definition, self.metadata):
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            for flags in ([], ['--ids', str(self.ids)]):
                stderr = io.StringIO()
                with self.subTest(path=path.name, flags=flags), redirect_stderr(stderr):
                    self.assertEqual(cli.main(self.argv + flags), 2)
                self.assertIn('hash mismatch', stderr.getvalue())
                self.loader.assert_not_called()
                self.assertFalse((self.root / 'new-run').exists())
            path.write_bytes(original)

    def test_intact_default_prepares_the_frozen_twenty_task_selection(self):
        self.assertEqual(cli.main(self.argv), 0)
        ids = selected_ids(self.ids)
        self.loader.assert_called_once_with(self.root / 'task-source', ids)
        manifest = read_manifest(self.root / 'new-run')
        self.assertEqual([task['instance_id'] for task in manifest['tasks']], ids)
        self.assertEqual(manifest['selection']['tasklist_sha256'], IDS_SHA256)
        self.assertEqual(manifest['selection']['preset']['id'], 'custom-verified20-v1')

    def test_explicit_custom_files_remain_supported_even_when_default_is_corrupted(self):
        original = self.ids.read_text().splitlines()
        self.ids.write_text('\n'.join(original[:-1]) + '\n')
        custom = self.root / 'custom.ids'
        for ids in (original[:-1], list(reversed(original))):
            custom.write_text('\n'.join(ids) + '\n')
            run = self.root / f'custom-{len(ids)}'
            argv = self.argv + ['--ids', str(custom), '--out', str(run)]
            with self.subTest(count=len(ids)):
                self.assertEqual(cli.main(argv), 0)
                self.loader.assert_called_with(self.root / 'task-source', ids)
                manifest = read_manifest(run)
                self.assertEqual([task['instance_id'] for task in manifest['tasks']], ids)
                self.assertNotIn('preset', manifest['selection'])
                self.assertEqual(manifest['selection']['tasklist_sha256'], tasklist_hash(ids))


class PresetReportingTests(unittest.TestCase):
    def fixture(self):
        run, manifest = ReportingTests.fixture(self)
        tasks = sanitize_tasks(preset_tasks(), selected_ids(DEFAULT_IDS))
        manifest.update(tasks=tasks, task_repo_revision=definition()['task_repo_revision'])
        manifest['selection'] = selection_record(selected_ids(DEFAULT_IDS), tasks, DATASET,
                                                  manifest['task_repo_revision'])
        write_json(run / 'build/runtime.json', runtime_record(manifest['versions'], manifest['node_image'], tasks))
        write_json(run / 'build/selection.json', manifest['selection'])
        manifest['profile_sha256'] = fingerprint(run / 'build')
        write_json(run / 'manifest.json', manifest)
        (run / 'predictions.jsonl').write_text(''.join(json.dumps({
            'instance_id': task['instance_id'], 'model_name_or_path': manifest['config_id'],
            'model_patch': 'partial diff'}) + '\n' for task in tasks))
        grading = read_json(run / 'grading.json')
        grading['predictions_sha256'] = file_hash(run / 'predictions.jsonl')
        write_json(run / 'grading.json', grading)
        write_json(run / grading['reports'] / manifest['config_id'] / tasks[0]['instance_id'] / 'report.json',
                   {tasks[0]['instance_id']: {'resolved': True}})
        write_json(run / 'builds.json', {task['instance_id']: {'platform': 'linux/amd64'} for task in tasks})
        return run, manifest

    def test_custom_label_raw_fixed_denominator_unknown_and_exact_predictions(self):
        run, manifest = self.fixture()
        summary = summarize_run(run)
        self.assertEqual((summary['resolved'], summary['selected'], summary['unknown_grades']), (1, 20, 19))
        text = (run / 'summary.md').read_text()
        self.assertIn('Custom SWE-bench Verified 20-task subset; not an official Verified score', text)
        self.assertIn('1/20', text)
        self.assertIn('Unknown grades: 19', text)
        self.assertEqual(len(predictions(run, manifest)), 20)
        path = run / 'predictions.jsonl'
        path.write_text('\n'.join(path.read_text().splitlines()[:-1]) + '\n')
        with self.assertRaises(ValueError):
            predictions(run, manifest)

    def test_codex_bedrock_pairing_and_runtime_mismatch_guards(self):
        run, _ = self.fixture()
        bedrock = summarize_run(run)
        codex = copy.deepcopy(bedrock)
        codex['run_id'] = 'codex'
        del codex['comparison_runtime']['versions']['pi-provider-litellm']
        result = compare(codex, bedrock)
        self.assertEqual(result['selected'], 20)
        self.assertEqual(result['baseline_resolved'], 1)
        self.assertEqual(result['candidate_resolved'], 1)
        self.assertEqual(len(result['inconclusive']), 19)
        for key in ('pi', 'pi-subagents'):
            candidate = copy.deepcopy(bedrock)
            candidate['comparison_runtime']['versions'][key] = '2.0.0'
            with self.assertRaises(ValueError):
                compare(codex, candidate)
        candidate = copy.deepcopy(bedrock)
        candidate['comparison_runtime']['versions']['pi-provider-litellm'] = '2.0.0'
        with self.assertRaises(ValueError):
            compare(bedrock, candidate)
        for key in ('node_image', 'platforms'):
            candidate = copy.deepcopy(bedrock)
            candidate['comparison_runtime'][key] = 'different'
            with self.assertRaises(ValueError):
                compare(codex, candidate)
        candidate = copy.deepcopy(bedrock)
        candidate['selection']['task_repo_revision'] = 'b' * 40
        with self.assertRaises(ValueError):
            compare(codex, candidate)

    def test_legacy_reports_keep_schema_and_pairing(self):
        run, _ = ReportingTests.fixture(self)
        legacy = summarize_run(run)
        self.assertNotIn('selection', legacy)
        self.assertNotIn('comparison_runtime', legacy)
        self.assertNotIn('Custom SWE-bench', (run / 'summary.md').read_text())
        self.assertEqual(compare(legacy, copy.deepcopy(legacy))['resolved_delta'], 0)
