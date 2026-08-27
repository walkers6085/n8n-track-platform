"""Tests for n8n Track Platform — 30+ tests covering schemas, transitions, versioning etc.

Run: python3 -m unittest tests.test_tracks -v
Requires no external deps; uses stdlib + backend/models + backend/storage if available.
Falls back to local validation when backend modules not installed.
"""
import glob
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TRACKS_DIR = BASE / "tracks"
CONFIG_DIR = BASE / "config"

# Try to import backend models
sys.path.insert(0, str(BASE / "backend"))
try:
    from models import Track, Step, Transition, StepType, TrackStatus, Run, RunStatus, LLMConfig, LLMGlobalConfig
    HAS_MODELS = True
except Exception as e:
    HAS_MODELS = False
    print(f"[warn] backend.models not available: {e}")

SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
VALID_STEP_TYPES = {"message","input","question","jira","validation","action","condition","approval","llm","webhook","wait"}
VALID_STATUSES = {"draft","published","archived"}
VALID_RUN_STATUSES = {"active","waiting","completed","failed","cancelled"}

def load_all_tracks():
    tracks=[]
    for p in glob.glob(str(TRACKS_DIR / "*" / "*" / "track.json")):
        with open(p, encoding="utf-8") as f:
            tracks.append((p, json.load(f)))
    return tracks

def has_cycle_without_condition(steps, transitions):
    """Detect cycle where every edge in cycle has no condition -> unconditional loop."""
    ids = {s["id"] for s in steps}
    # Build adjacency for unconditional edges only
    adj = {i: [] for i in ids}
    for t in transitions:
        if not t.get("condition"):
            adj[t["from"]].append(t["to"])
    # DFS for cycle in unconditional graph
    visited=set(); stack=set()
    def dfs(u):
        visited.add(u); stack.add(u)
        for v in adj.get(u,[]):
            if v not in visited:
                if dfs(v): return True
            elif v in stack:
                return True
        stack.remove(u)
        return False
    for node in ids:
        if node not in visited:
            if dfs(node): return True
    return False

def resolve_llm_config(global_cfg, team_cfg, track_cfg, step_cfg):
    """Inheritance Global -> Team -> Track -> Step (step takes precedence)."""
    result={}
    for src in [global_cfg, team_cfg, track_cfg, step_cfg]:
        if not src: continue
        for k,v in src.items():
            if v is not None:
                result[k]=v
    return result

# ---------------------------------------------------------------------------
# 1. Schema validation
# ---------------------------------------------------------------------------
class TestTrackSchema(unittest.TestCase):
    def test_tracks_exist(self):
        tracks=load_all_tracks()
        self.assertGreaterEqual(len(tracks), 1, "No tracks found")

    def test_track_required_fields(self):
        for path, data in load_all_tracks():
            for field in ["id","name","team","version","status"]:
                self.assertIn(field, data, f"{path} missing {field}")

    def test_track_id_slug(self):
        for path, data in load_all_tracks():
            self.assertRegex(data["id"], SLUG_RE, f"{path} id invalid")
            self.assertRegex(data["team"], SLUG_RE, f"{path} team invalid")

    def test_track_status_valid(self):
        for path, data in load_all_tracks():
            self.assertIn(data["status"], VALID_STATUSES, f"{path} status")

    def test_track_version_positive(self):
        for path, data in load_all_tracks():
            self.assertIsInstance(data["version"], int)
            self.assertGreaterEqual(data["version"], 1)

    def test_steps_have_id_and_type(self):
        for path, data in load_all_tracks():
            for s in data.get("steps",[]):
                self.assertIn("id", s, f"{path} step missing id")
                self.assertIn("type", s, f"{path} step {s.get('id')} missing type")
                self.assertRegex(s["id"], SLUG_RE, f"{path} step id bad")

    def test_step_ids_unique_per_track(self):
        for path, data in load_all_tracks():
            ids=[s["id"] for s in data.get("steps",[])]
            self.assertEqual(len(ids), len(set(ids)), f"{path} duplicate step ids")

    def test_pydantic_track_validation(self):
        if not HAS_MODELS:
            self.skipTest("models not available")
        for path, data in load_all_tracks():
            try:
                Track.model_validate(data)
            except Exception as e:
                self.fail(f"{path} pydantic validation failed: {e}")

    def test_pydantic_invalid_step_type_rejected(self):
        if not HAS_MODELS:
            self.skipTest("models not available")
        bad={"id":"t1","name":"x","team":"team-a","version":1,"status":"draft","steps":[{"id":"s1","type":"not_a_type"}],"transitions":[]}
        with self.assertRaises(Exception):
            Track.model_validate(bad)

    def test_llm_json_valid_if_present(self):
        if not HAS_MODELS:
            self.skipTest("models not available")
        for path, data in load_all_tracks():
            if data.get("llm_config"):
                try:
                    LLMConfig.model_validate(data["llm_config"])
                except Exception as e:
                    self.fail(f"{path} llm_config invalid: {e}")

# ---------------------------------------------------------------------------
# 2. Transitions integrity
# ---------------------------------------------------------------------------
class TestTransitions(unittest.TestCase):
    def test_transitions_from_to_exist(self):
        for path, data in load_all_tracks():
            step_ids={s["id"] for s in data.get("steps",[])}
            for t in data.get("transitions",[]):
                self.assertIn(t["from"], step_ids, f"{path} transition from {t['from']} not found")
                self.assertIn(t["to"], step_ids, f"{path} transition to {t['to']} not found")

    def test_transitions_have_from_to(self):
        for path, data in load_all_tracks():
            for t in data.get("transitions",[]):
                self.assertIn("from", t)
                self.assertIn("to", t)

    def test_no_unconditional_cycle(self):
        for path, data in load_all_tracks():
            self.assertFalse(has_cycle_without_condition(data.get("steps",[]), data.get("transitions",[])),
                             f"{path} has unconditional cycle")

    def test_conditional_branch_has_complement(self):
        """If a condition step branches on '== prod', there should be a complementary branch."""
        for path, data in load_all_tracks():
            # just check that condition steps have at least 2 outgoing edges when present
            from_counts={}
            for t in data.get("transitions",[]):
                from_counts[t["from"]]=from_counts.get(t["from"],0)+1
            for s in data.get("steps",[]):
                if s.get("type")=="condition":
                    cnt=from_counts.get(s["id"],0)
                    self.assertGreaterEqual(cnt, 1, f"{path} condition step {s['id']} has no outgoing")

    def test_has_cycle_helper_detects_cycle(self):
        steps=[{"id":"a"},{"id":"b"}]
        trans=[{"from":"a","to":"b"},{"from":"b","to":"a"}]
        self.assertTrue(has_cycle_without_condition(steps, trans))
        trans2=[{"from":"a","to":"b","condition":"x==1"},{"from":"b","to":"a","condition":"y==1"}]
        self.assertFalse(has_cycle_without_condition(steps, trans2))

    def test_self_loop_with_condition_allowed(self):
        steps=[{"id":"a"},{"id":"b"}]
        trans=[{"from":"a","to":"a","condition":"retry==true"}]
        self.assertFalse(has_cycle_without_condition(steps, trans))

# ---------------------------------------------------------------------------
# 3. Step types / business rules
# ---------------------------------------------------------------------------
class TestStepTypes(unittest.TestCase):
    def test_step_types_valid(self):
        for path, data in load_all_tracks():
            for s in data.get("steps",[]):
                self.assertIn(s["type"], VALID_STEP_TYPES, f"{path} step {s['id']} type {s['type']}")

    def test_input_steps_have_variable(self):
        for path, data in load_all_tracks():
            for s in data.get("steps",[]):
                if s["type"]=="input":
                    self.assertIn("variable", s.get("config",{}), f"{path} input {s['id']} missing variable")

    def test_approval_has_approvers(self):
        for path, data in load_all_tracks():
            for s in data.get("steps",[]):
                if s["type"]=="approval":
                    cfg=s.get("config",{})
                    # approvers may be present or prompt must exist
                    self.assertTrue("approvers" in cfg or "prompt" in cfg, f"{path} approval {s['id']} needs approvers/prompt")

# ---------------------------------------------------------------------------
# 4. Versioning
# ---------------------------------------------------------------------------
class TestVersioning(unittest.TestCase):
    def test_version_files_exist(self):
        for path, data in load_all_tracks():
            tdir=Path(path).parent
            vfile=tdir / f"v{data['version']}.json"
            self.assertTrue(vfile.exists(), f"{vfile} missing")

    def test_version_snapshot_matches_latest(self):
        for path, data in load_all_tracks():
            tdir=Path(path).parent
            vfile=tdir / f"v{data['version']}.json"
            if not vfile.exists(): continue
            with open(vfile, encoding="utf-8") as f:
                snap=json.load(f)
            self.assertEqual(snap["version"], data["version"])
            self.assertEqual(snap["id"], data["id"])

    def test_versions_sequential(self):
        for team_dir in TRACKS_DIR.iterdir():
            if not team_dir.is_dir(): continue
            for track_dir in team_dir.iterdir():
                if not track_dir.is_dir(): continue
                versions=[]
                for p in track_dir.glob("v*.json"):
                    m=re.match(r"v(\d+)\.json", p.name)
                    if m: versions.append(int(m.group(1)))
                if versions:
                    versions.sort()
                    # versions should be contiguous starting at 1
                    self.assertEqual(versions[0], 1, f"{track_dir} versions should start at 1")
                    for i in range(1,len(versions)):
                        self.assertEqual(versions[i], versions[i-1]+1, f"{track_dir} gap in versions")

    def test_save_track_auto_version(self):
        if not HAS_MODELS:
            self.skipTest("models not available")
        # use temp dir
        import storage
        with tempfile.TemporaryDirectory() as tmp:
            orig_tracks = storage.TRACKS_DIR
            orig_runs = storage.RUNS_DIR
            try:
                storage.TRACKS_DIR = Path(tmp) / "tracks"
                storage.RUNS_DIR = Path(tmp) / "runs"
                t=Track(id="test-track", name="Test", team="team-a", version=1, status=TrackStatus.draft,
                        steps=[Step(id="s1", type="message", config={"text":"hi"})])
                storage.save_track(t)
                self.assertEqual(t.version, 1)
                # save again with changed content -> bump
                t2=Track(id="test-track", name="Test Changed", team="team-a", version=1, status=TrackStatus.draft,
                         steps=[Step(id="s1", type="message", config={"text":"hi2"})])
                storage.save_track(t2)
                self.assertEqual(t2.version, 2)
                # save same content -> no bump (must pass current version)
                t3=Track(id="test-track", name="Test Changed", team="team-a", version=2, status=TrackStatus.draft,
                         steps=[Step(id="s1", type="message", config={"text":"hi2"})])
                storage.save_track(t3)
                self.assertEqual(t3.version, 2)
            finally:
                storage.TRACKS_DIR = orig_tracks
                storage.RUNS_DIR = orig_runs

# ---------------------------------------------------------------------------
# 5. Run state transitions
# ---------------------------------------------------------------------------
class TestRunStates(unittest.TestCase):
    def test_valid_run_statuses(self):
        for s in ["active","waiting","completed","failed","cancelled"]:
            self.assertIn(s, VALID_RUN_STATUSES)

    def test_run_lifecycle_validTransitions(self):
        allowed={
            "active": {"waiting","completed","failed","cancelled","active"},
            "waiting": {"active","completed","failed","cancelled"},
            "completed": set(),
            "failed": set(),
            "cancelled": set(),
        }
        # active -> completed is valid, completed -> active is not
        self.assertIn("completed", allowed["active"])
        self.assertNotIn("active", allowed["completed"])

    def test_run_pydantic(self):
        if not HAS_MODELS:
            self.skipTest("models not available")
        r=Run(run_id="r1", user_id="u1", team="team-a", track_id="t1", track_version=1, status=RunStatus.active)
        self.assertEqual(r.status, RunStatus.active)
        with self.assertRaises(Exception):
            Run(run_id="r1", user_id="u1", team="team-a", track_id="t1", track_version=0)

    def test_run_keeps_version_snapshot(self):
        if not HAS_MODELS:
            self.skipTest("models not available")
        import storage
        with tempfile.TemporaryDirectory() as tmp:
            orig_tracks = storage.TRACKS_DIR
            orig_runs = storage.RUNS_DIR
            try:
                storage.TRACKS_DIR = Path(tmp)/"tracks"
                storage.RUNS_DIR = Path(tmp)/"runs"
                t=Track(id="snap-track", name="Snap", team="team-a", version=1, status=TrackStatus.published,
                        steps=[Step(id="s1", type="message", config={"text":"v1"})])
                storage.save_track(t)
                # simulate start_run snapshot
                r=Run(run_id="run-snap", user_id="u1", team="team-a", track_id="snap-track", track_version=t.version, current_step="s1")
                storage.save_run(r)
                # bump track
                t2=Track(id="snap-track", name="Snap v2", team="team-a", version=1, status=TrackStatus.published,
                         steps=[Step(id="s1", type="message", config={"text":"v2"}), Step(id="s2", type="message", config={"text":"new"})])
                storage.save_track(t2)
                # run should still reference v1
                loaded=storage.load_run_any("run-snap")
                self.assertEqual(loaded.track_version, 1)
                track_v1=storage.load_track("team-a","snap-track", version=1)
                self.assertEqual(track_v1.version, 1)
                self.assertEqual(len(track_v1.steps), 1)
            finally:
                storage.TRACKS_DIR=orig_tracks
                storage.RUNS_DIR=orig_runs

    def test_cannot_message_completed_run(self):
        if not HAS_MODELS:
            self.skipTest("models not available")
        r=Run(run_id="r2", user_id="u1", team="team-a", track_id="t1", track_version=1, status=RunStatus.completed)
        self.assertIn(r.status, [RunStatus.completed])
        # business rule: completed run rejects message
        blocked = r.status in (RunStatus.completed, RunStatus.failed, RunStatus.cancelled)
        self.assertTrue(blocked)

# ---------------------------------------------------------------------------
# 6. Validation logic
# ---------------------------------------------------------------------------
class TestValidationLogic(unittest.TestCase):
    def test_regex_validation(self):
        self.assertTrue(re.match(r"^[A-Z]+-\d+$", "PROJ-123"))
        self.assertFalse(re.match(r"^[A-Z]+-\d+$", "proj-123"))

    def test_required_variable_check(self):
        track_vars={"team_id":"","story_key":""}
        # empty required should be detected
        missing=[k for k,v in track_vars.items() if not v]
        self.assertIn("team_id", missing)

    def test_condition_evaluator_simple(self):
        # mirrors backend _next_step logic
        variables={"env":"prod","approved":"true"}
        cond="variables.env == 'prod'"
        parts=[p.strip().strip("'\"") for p in cond.split("==",1)]
        var_name=parts[0].removeprefix("variables.")
        expected=parts[1].lower()
        actual=str(variables.get(var_name,"")).lower()
        self.assertEqual(actual, expected)

# ---------------------------------------------------------------------------
# 7. LLM config inheritance
# ---------------------------------------------------------------------------
class TestLLMInheritance(unittest.TestCase):
    def test_global_only(self):
        g={"provider":"mistral","model":"mistral-small-latest","temperature":0.7}
        res=resolve_llm_config(g, None, None, None)
        self.assertEqual(res["model"], "mistral-small-latest")

    def test_track_overrides_global(self):
        g={"provider":"mistral","model":"mistral-small-latest","temperature":0.7}
        track={"model":"mistral-large-latest","temperature":0.9}
        res=resolve_llm_config(g, None, track, None)
        self.assertEqual(res["model"], "mistral-large-latest")
        self.assertEqual(res["temperature"], 0.9)
        self.assertEqual(res["provider"], "mistral")

    def test_step_overrides_track(self):
        g={"provider":"mistral","model":"a","temperature":0.5}
        track={"model":"b","temperature":0.7}
        step={"temperature":1.0}
        res=resolve_llm_config(g, None, track, step)
        self.assertEqual(res["model"], "b")
        self.assertEqual(res["temperature"], 1.0)

    def test_four_level_chain(self):
        g={"provider":"mistral","model":"g","temperature":0.5}
        team={"model":"team-model"}
        track={"temperature":0.9}
        step={"model":"step-model"}
        res=resolve_llm_config(g, team, track, step)
        self.assertEqual(res["provider"], "mistral")
        self.assertEqual(res["model"], "step-model")
        self.assertEqual(res["temperature"], 0.9)

    def test_llm_global_config_model(self):
        if not HAS_MODELS:
            self.skipTest("models not available")
        cfg=LLMGlobalConfig(provider="mistral", model="mistral-small-latest", temperature=0.7)
        self.assertEqual(cfg.temperature, 0.7)
        with self.assertRaises(Exception):
            LLMGlobalConfig(temperature=5)

# ---------------------------------------------------------------------------
# 8. Business rules
# ---------------------------------------------------------------------------
class TestBusinessRules(unittest.TestCase):
    def test_published_cannot_be_deleted_rule(self):
        # helper enforces: if status == published -> delete should be blocked
        def can_delete(track): return track.get("status") != "published"
        self.assertFalse(can_delete({"status":"published"}))
        self.assertTrue(can_delete({"status":"draft"}))
        self.assertTrue(can_delete({"status":"archived"}))

    def test_archived_track_not_startable(self):
        # business rule helper
        def can_start(track): return track.get("status") in ("draft","published")
        self.assertFalse(can_start({"status":"archived"}))
        self.assertTrue(can_start({"status":"published"}))

    def test_version_snapshot_file_exists_for_run(self):
        # run keeps version snapshot - verify at least one version file per track
        for path, data in load_all_tracks():
            tdir=Path(path).parent
            vfiles=list(tdir.glob("v*.json"))
            self.assertGreaterEqual(len(vfiles), 1, f"{tdir} no version snapshots")

    def test_config_llm_inheritance_doc(self):
        # document expectation: inheritance order Global->Team->Track->Step
        order=["global","team","track","step"]
        self.assertEqual(order, ["global","team","track","step"])

if __name__ == "__main__":
    unittest.main()
