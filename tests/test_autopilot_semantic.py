"""Optional Gemini shortlist refinement: no-op absent, cached, bounded.

automation/ never imports google-genai — SemanticEvaluatorPort is the seam
app.py uses to register the real Gemini-backed adapter, exactly like
ClipGeneratorPort and PublisherPort. These tests install a fake port and
never touch the network.
"""
from datetime import datetime, timezone

import pytest

from automation import discovery, ports
from automation.config import POLICY_CREATIVE_COMMONS, normalise
from automation.db import AutopilotDB
from automation.models import SourceState
from automation.ports import SemanticEvaluatorPort
from autopilot_fakes import FakeYouTubeClient, make_record, run_async

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    database = AutopilotDB(":memory:").connect()
    yield database
    database.close()


@pytest.fixture(autouse=True)
def clean_ports():
    ports.reset()
    yield
    ports.reset()


def _config(**overrides):
    base = {"rights": {"policy": POLICY_CREATIVE_COMMONS},
           "discovery": {"lanes": ["TRENDING_NOW"], "semantic_shortlist_size": 5}}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return normalise(base)


class FakeSemanticEvaluator:
    def __init__(self, scores):
        self.scores = scores
        self.calls = 0
        self.seen_video_ids = []

    async def evaluate(self, candidates):
        self.calls += 1
        self.seen_video_ids.extend(c["video_id"] for c in candidates)
        return {c["video_id"]: {"overall_score": self.scores.get(c["video_id"], 0.0)}
               for c in candidates}

    def port(self):
        return SemanticEvaluatorPort(evaluate=self.evaluate, model_version="test-v1")


class TestNoOpWithoutAPort:
    def test_discovery_works_unchanged_with_no_semantic_evaluator_registered(self, db):
        records = [make_record("vid00000001", view_count=1000, now=NOW)]
        client = FakeYouTubeClient(records)
        result = run_async(discovery.run_discovery(db, _config(), client, run_id="r1", now=NOW))
        assert result["eligible"] == 1
        assert result.get("semantic_evaluated") == 0


class TestSemanticRefinement:
    def test_a_registered_evaluator_is_called_for_eligible_candidates(self, db):
        evaluator = FakeSemanticEvaluator({"vid00000001": 0.9})
        ports.register(semantic_evaluator=evaluator.port())
        records = [make_record("vid00000001", view_count=1000, now=NOW)]
        client = FakeYouTubeClient(records)
        run_async(discovery.run_discovery(db, _config(), client, run_id="r1", now=NOW))
        assert evaluator.calls == 1
        assert "vid00000001" in evaluator.seen_video_ids

    def test_the_semantic_score_is_merged_into_the_breakdown(self, db):
        evaluator = FakeSemanticEvaluator({"vid00000001": 0.9})
        ports.register(semantic_evaluator=evaluator.port())
        records = [make_record("vid00000001", view_count=1000, now=NOW)]
        client = FakeYouTubeClient(records)
        run_async(discovery.run_discovery(db, _config(), client, run_id="r1", now=NOW))
        source = db.get_source_by_video_id("vid00000001")
        assert source.score_breakdown["components"]["semantic"] == 0.9

    def test_a_cached_evaluation_is_never_requested_twice(self, db):
        evaluator = FakeSemanticEvaluator({"vid00000001": 0.9})
        ports.register(semantic_evaluator=evaluator.port())
        records = [make_record("vid00000001", view_count=1000, now=NOW)]
        client = FakeYouTubeClient(records)
        run_async(discovery.run_discovery(db, _config(), client, run_id="r1", now=NOW))
        assert evaluator.calls == 1
        # A second run re-discovers the same (already-known) video; nothing
        # new to score, so the evaluator must not be called again for it.
        run_async(discovery.run_discovery(db, _config(), client, run_id="r2", now=NOW))
        assert evaluator.calls == 1

    def test_shortlist_size_zero_disables_the_pass_even_with_a_port_registered(self, db):
        evaluator = FakeSemanticEvaluator({"vid00000001": 0.9})
        ports.register(semantic_evaluator=evaluator.port())
        records = [make_record("vid00000001", view_count=1000, now=NOW)]
        client = FakeYouTubeClient(records)
        run_async(discovery.run_discovery(
            db, _config(discovery={"semantic_shortlist_size": 0}), client,
            run_id="r1", now=NOW))
        assert evaluator.calls == 0

    def test_an_evaluator_exception_does_not_break_discovery(self, db):
        class Broken:
            async def evaluate(self, candidates):
                raise RuntimeError("gemini is down")

        ports.register(semantic_evaluator=SemanticEvaluatorPort(
            evaluate=Broken().evaluate, model_version="test-v1"))
        records = [make_record("vid00000001", view_count=1000, now=NOW)]
        client = FakeYouTubeClient(records)
        result = run_async(discovery.run_discovery(db, _config(), client, run_id="r1", now=NOW))
        assert result["eligible"] == 1
        source = db.get_source_by_video_id("vid00000001")
        assert source.state == SourceState.ELIGIBLE
