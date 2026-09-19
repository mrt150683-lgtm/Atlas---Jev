"""Captured-provider assertions verify actual evidence crossing prompt boundaries."""
import json

import pytest

from cms.features import Feature, build_features, discover_features_llm, prepare_known
from cms.graph_builder import build_graph
from cms.providers import MockProvider
from cms.scanner import scan
from cms.summarizer import file_purpose, generate_summaries
from cms.update import incremental_update


@pytest.mark.parametrize("summary", [
    "1. **File Purpose**\nProcesses invoices and charges accounts.\n\n2. **Key Components**\n- charge()",
    "## File Purpose\nProcesses invoices and charges accounts.\n## Key Components\n- charge()",
    "**File Purpose:** Processes invoices and charges accounts.\n\n**Key Components**\n- charge()",
    "Processes invoices and charges accounts.",
])
def test_discovery_receives_purpose_not_heading(tmp_path, summary):
    (tmp_path / "main.py").write_text("def charge():\n    pass\n")
    graph = build_graph(scan(tmp_path))
    graph.nodes["file:main.py"]["summary"] = summary
    class Capture:
        name = "capture"
        def summarize(self, prompt, context): self.prompt = prompt; return "[]"
    provider = Capture()
    discover_features_llm(graph, provider, known=[])
    assert "Processes invoices and charges accounts." in provider.prompt
    assert "main.py: 1. **File Purpose**" not in provider.prompt
    assert "func:main.py::charge" in provider.prompt


def test_independent_features_can_share_a_file_and_keep_component_members(tmp_path):
    (tmp_path / "account.py").write_text("def login():\n    pass\ndef reset_password():\n    pass\n")
    graph = build_graph(scan(tmp_path))
    generate_summaries(graph, tmp_path, MockProvider())
    class Capture:
        name = "capture"
        def summarize(self, prompt, context):
            return json.dumps([
                {"name": "Login", "description": "Logs in", "files": ["account.py"], "members": ["func:account.py::login"]},
                {"name": "PasswordReset", "description": "Resets password", "files": ["account.py"], "members": ["func:account.py::reset_password"]},
            ])
    features = discover_features_llm(graph, Capture(), known=[])
    found, _, _ = prepare_known(graph, features)
    assert set(found) == {"Login", "PasswordReset"}
    assert found["PasswordReset"].members == ["func:account.py::reset_password"]


def test_existing_file_behavior_edit_refreshes_discovery_but_unchanged_skips(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("def charge():\n    pass\n")
    class Provider(MockProvider):
        name = "offline-real"
        def __init__(self): self.discovery_calls = 0; self.prompts = []
        def summarize(self, prompt, context):
            self.prompts.append(prompt)
            if "named FEATURES" in prompt:
                self.discovery_calls += 1
                return "[]"
            if "systems" in prompt.lower():
                return '{"systems": []}'
            return super().summarize(prompt, context)
    provider = Provider()
    incremental_update(tmp_path, provider, echo=lambda *a: None)
    assert provider.discovery_calls == 1
    incremental_update(tmp_path, provider, echo=lambda *a: None)
    assert provider.discovery_calls == 1
    target.write_text("def charge():\n    pass\ndef remind():\n    pass\n")
    incremental_update(tmp_path, provider, echo=lambda *a: None)
    assert provider.discovery_calls == 2
    assert any("func:main.py::remind" in prompt for prompt in provider.prompts if "named FEATURES" in prompt)


def test_summary_prompt_separates_intent_and_preserves_parse_warning(tmp_path):
    (tmp_path / "broken.py").write_text("# @memory:summary:Always safe\ndef broken(:\n    pass\n")
    graph = build_graph(scan(tmp_path))
    class Capture(MockProvider):
        def summarize(self, prompt, context): self.prompt = prompt; return "File summary"
    provider = Capture()
    generate_summaries(graph, tmp_path, provider)
    assert "Developer comments describe" in provider.prompt
    assert "Structural parsing failed" in provider.prompt
    assert "Analysis limitations (Atlas)" in graph.nodes["file:broken.py"]["summary"]


def test_truncated_source_limitations_survive_provider_output(tmp_path):
    (tmp_path / "large.py").write_text("# visible start\n" + "# filler evidence\n" * 1600 + "def tail():\n    pass\n")
    graph = build_graph(scan(tmp_path))
    class Capture(MockProvider):
        def summarize(self, prompt, context): self.prompt = prompt; return "1. **File Purpose**\nA source file."
    provider = Capture()
    generate_summaries(graph, tmp_path, provider)
    attrs = graph.nodes["file:large.py"]
    assert "tail resumes within ORIGINAL line" in provider.prompt
    assert attrs["summary_meta"]["source_truncated"] is True
    assert "omitted middle section was not analyzed" in attrs["summary"]
