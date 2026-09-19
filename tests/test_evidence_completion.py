"""Behavioral negative controls for completion and stale-evidence boundaries."""
import json

import pytest

from cms.annotations import AnnotationStore
from cms.features import build_features
from cms.fidelity import intent_fidelity
from cms.flowreview import FlowReviewError, build_flow_review, content_hash
from cms.graph_builder import build_graph
from cms.hierarchy import HierarchyError, write_hierarchy
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.scanner import scan
from cms.sentinel.ledger import audit_ledger, init_ledger
from cms.verify import behavioral_status, run_verification_plan


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'app.py').write_text('# @memory:feature:Greeting\ndef greet(name):\n    return name.strip()\n')
    (tmp_path / 'test_app.py').write_text('from app import greet\ndef test_greet():\n    assert greet(" Ada ") == "Ada"\n')
    (tmp_path / '.memory').mkdir()
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    plan = {'goal': 'strip greeting whitespace', 'features': [{'feature': 'Greeting', 'criteria': [
        {'id': 'trim', 'expectation': 'leading and trailing spaces are removed',
         'test_ids': ['test_app.py::test_greet']}]}]}
    return tmp_path, graph, plan


def test_real_criterion_run_has_viable_completion_then_test_edit_invalidates(project):
    root, graph, plan = project
    result = run_verification_plan(root, graph, plan)
    assert result['passed'], result
    evidence = graph.nodes['feature:Greeting']['behavioral_evidence']
    assert behavioral_status(root, evidence)['passed']
    assert intent_fidelity(root, graph, 'Greeting')['overall'] == 'on_track'
    CodebaseMemory(graph).save(root / '.memory' / 'graph.json')
    ledger = json.loads(init_ledger(root).read_text())
    assert ledger['features'][0]['status'] == 'complete'
    assert not [f for f in audit_ledger(root) if f['area'] == 'ledger_completion']
    (root / 'test_app.py').write_text('from app import greet\ndef test_greet():\n    assert greet(" Ada ") == "WRONG"\n')
    assert not behavioral_status(root, evidence)['current']
    assert intent_fidelity(root, graph, 'Greeting')['overall'] == 'insufficient_evidence'
    assert any(f['pattern'] == 'complete-without-current-criteria' for f in audit_ledger(root))
    failed = run_verification_plan(root, graph, plan)
    assert failed['passed'] is False
    assert intent_fidelity(root, graph, 'Greeting')['overall'] == 'attention'


def test_plan_validates_all_entries_before_any_run_or_mutation(project, monkeypatch):
    root, graph, plan = project
    plan['features'].append({'feature': 'Missing', 'criteria': []})
    monkeypatch.setattr('cms.verify.verify_feature', lambda *a: pytest.fail('must not run'))
    with pytest.raises(ValueError):
        run_verification_plan(root, graph, plan)
    assert 'behavioral_evidence' not in graph.nodes['feature:Greeting']


def test_goal_mismatch_cannot_reuse_success(project):
    root, graph, plan = project
    run_verification_plan(root, graph, plan)
    status = behavioral_status(root, graph.nodes['feature:Greeting']['behavioral_evidence'], goal='encrypt names')
    assert status['current'] and not status['passed'] and not status['goal_matches']


def test_alignment_requires_explicit_goal_and_real_current_criterion_run(project, monkeypatch):
    from cms.align import build_alignment
    root, graph, plan = project
    intent = {'task': plan['goal'], 'intent_source': 'explicit', 'declared_paths': ['app.py']}
    monkeypatch.setattr('cms.align.git_changed_files', lambda *a, **kw: ['app.py'])
    assert build_alignment(CodebaseMemory(graph), root, intent)['verdict'] == 'unverified'
    run_verification_plan(root, graph, plan)
    assert build_alignment(CodebaseMemory(graph), root, intent)['verdict'] == 'aligned'
    intent['intent_source'] = 'branch'
    assert build_alignment(CodebaseMemory(graph), root, intent)['verdict'] == 'unverified'


def test_ninth_annotation_still_blocks_fidelity_and_changes_flow_hash(project):
    root, graph, _ = project
    store = AnnotationStore(root / '.memory', root=root)
    for index in range(8):
        store.add('feature:Greeting', 'note', f'ordinary note {index}', feature='Greeting')
    before = content_hash(graph, root, 'Greeting')
    store.add('feature:Greeting', 'contradiction', 'Does not preserve required whitespace', feature='Greeting')
    assert content_hash(graph, root, 'Greeting') != before
    fidelity = intent_fidelity(root, graph, 'Greeting')
    assert fidelity['overall'] == 'attention'
    assert fidelity['dimensions']['open_contradictions'] == 1


def test_empty_provider_analysis_cannot_be_verified(project):
    root, graph, _ = project
    class EmptyProvider:
        name, model = 'fake', 'empty'
        def summarize(self, prompt, context):
            return json.dumps({'status': 'partially_verified', 'steps': []})
    with pytest.raises(FlowReviewError):
        build_flow_review(root, graph, EmptyProvider(), 'Greeting')
    assert 'flow_review' not in graph.nodes['feature:Greeting']


def test_components_with_same_label_keep_distinct_parents(project):
    root, graph, _ = project
    graph.add_node('feature:Other', type='feature', name='Other', members=[])
    spec = {'systems': [
        {'name': 'Alpha', 'components': [{'name': 'Shared', 'features': ['Greeting']}]},
        {'name': 'Beta', 'components': [{'name': 'Shared', 'features': ['Other']}]}]}
    write_hierarchy(graph, spec, provenance='heuristic')
    assert graph.has_edge('feature:Greeting', 'component:Alpha/Shared')
    assert graph.has_edge('feature:Other', 'component:Beta/Shared')
    prior = set(graph.nodes)
    spec['systems'][0]['components'].append({'name': 'Shared', 'features': []})
    with pytest.raises(HierarchyError):
        write_hierarchy(graph, spec, provenance='heuristic')
    assert set(graph.nodes) == prior


def test_sentinel_crash_fails_default_gate_and_resolves_on_recovery(project, monkeypatch):
    root, _, _ = project
    from cms.sentinel.runner import run_scan
    def crash(_root):
        raise RuntimeError('injected audit failure')
    monkeypatch.setattr('cms.sentinel.ledger.audit_ledger', crash)
    scan, findings = run_scan(root, modules=('ledger',))
    assert scan['gate']['failed'] and not scan['gate']['scan_complete']
    errors = [f for f in findings.values() if f['pattern'] == 'module-error-ledger']
    assert len(errors) == 1 and errors[0]['status'] == 'open'
    monkeypatch.setattr('cms.sentinel.ledger.audit_ledger', lambda _root: [])
    recovered, findings = run_scan(root, modules=('ledger',))
    assert not recovered['gate']['failed']
    assert all(f['status'] == 'resolved' for f in findings.values() if f['pattern'] == 'module-error-ledger')


def test_corrupt_sentinel_findings_are_preserved(project):
    from cms.sentinel.store import SentinelStore
    root, _, _ = project
    store = SentinelStore(root / '.memory')
    store.dir.mkdir()
    store.findings_path.write_text('{broken')
    with pytest.raises(ValueError, match='preserved'):
        store.merge_scan({'findings': [], 'modules_run': ['ledger']})
    assert store.findings_path.read_text() == '{broken'
