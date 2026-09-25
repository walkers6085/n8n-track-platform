"""Tests: python3 -m unittest discover -s tests -t . -v   (from the repo root, no network needed)."""

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend import agent, bpmn, engine, main, storage, tracks
from backend.models import JiraIssueRef, Link, RunStatus, Settings, Track

REPO = Path(__file__).resolve().parent.parent
# the partner system's example: scheme (3 copy-paste breaks repaired), tasks.json, one activity
# local only (.gitignore): contains internal links and names, so these tests skip in a clean clone
PARTNER = REPO / "tests" / "fixtures" / "partner"
PARTNER_ACTIVITY = "Activity_1huk1tj"
needs_partner = unittest.skipUnless((PARTNER / "scheme.bpmn").exists(),
                                    "partner example is local only (tests/fixtures/partner)")


def demo_spec(status="published"):
    """a -> b -> gateway(env) -> c (prod) | end (test); c is jira-send -> d(install_ok) -> end."""
    return ({"id": "t1", "name": "Тест", "status": status},
            [{"id": "a", "name": "Данные",
              "content": '<p>Смотри <a href="https://wiki.example/doc">инструкцию</a> и '
                         '<a class="confluence-userlink" href="https://wiki.example/display/~42">Иванова</a></p>',
              "properties": [{"codeName": "env", "name": "Окружение", "valueType": "select",
                              "valueVariants": ["test", "prod"]},
                             {"codeName": "note", "name": "Заметка", "required": False}]},
             {"id": "b", "name": "Проверки", "requirements": [{"id": "ok", "text": "Тесты зелёные"}],
              "next": [{"to": "c", "prop": "env", "value": "prod", "name": "Прод"},
                       {"to": "end", "prop": "env", "value": "test", "name": "Тест"}]},
             {"id": "c", "name": "Заявка", "type": "jira-send",
              "jira": {"project": "REL", "summary": "Релиз на {env} {window}"}},
             {"id": "d", "name": "Установка",
              "properties": [{"codeName": "install_ok", "name": "Успешно", "valueType": "boolean"}]}])


def make_track(status="published") -> Track:
    return tracks.from_spec(*demo_spec(status))


def ids(track: Track):
    return {track.activities[t].name: t for t in track.tasks}


class TempStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = storage.root()
        storage.set_root(self.tmp)

    def tearDown(self):
        storage.set_root(self._old)
        shutil.rmtree(self.tmp)


# ---------------------------------------------------------------------------
# BPMN
# ---------------------------------------------------------------------------

class TestBpmn(unittest.TestCase):
    @needs_partner
    def test_partner_scheme(self):
        g = bpmn.parse((PARTNER / "scheme.bpmn").read_text(encoding="utf-8"))
        self.assertEqual(len(g.tasks), 46)
        self.assertEqual({gr.name for gr in g.groups}, {"Архитектура", "Заявки", "Выпуск", "Разработка"})
        self.assertTrue(all(gr.members for gr in g.groups))
        self.assertEqual(sum(1 for f in g.flows.values() if f.condition), 24)
        self.assertFalse([f for f in g.flows.values() if f.condition_error])
        jira_send = [n for n in g.tasks if n.task_type == "jira-send"]
        self.assertEqual(len(jira_send), 12)

    @needs_partner
    def test_partner_traversal_needs_then_branches(self):
        g = bpmn.parse((PARTNER / "scheme.bpmn").read_text(encoding="utf-8"))
        first = bpmn.first_task(g, {})
        self.assertEqual(first.kind, "task")
        second = bpmn.follow(g, first.task, {}).task
        self.assertEqual(bpmn.follow(g, second, {}).need, ["_os__new_team"])
        yes = bpmn.follow(g, second, {"_os__new_team": True})
        no = bpmn.follow(g, second, {"_os__new_team": False})
        self.assertEqual(g.nodes[yes.task].name, "OpenShift")
        self.assertEqual(g.nodes[no.task].name, "Онбординг")

    @needs_partner
    def test_leading_blank_line_and_damage(self):
        xml = (PARTNER / "scheme.bpmn").read_text(encoding="utf-8")
        self.assertEqual(len(bpmn.parse("\n\n" + xml).tasks), 46)
        broken = xml.replace('</bpmn:process>', '<bpmn:task id="x"\n\n</bpmn:process>', 1)
        with self.assertRaises(bpmn.BpmnError) as cm:
            bpmn.parse(broken)
        self.assertIn("строка", str(cm.exception))

    def test_conditions(self):
        c = bpmn.parse_condition('${objProps.prop("ai").value() == true}')
        self.assertEqual((c.prop, c.value), ("ai", True))
        self.assertTrue(c.matches("да"))
        self.assertFalse(c.matches(False))
        c = bpmn.parse_condition('${objProps.prop("_os__prod").value() == "dev"}')
        self.assertTrue(c.matches("DEV"))
        self.assertEqual(bpmn.format_condition("_os__prod", "dev"),
                         '${objProps.prop("_os__prod").value() == "dev"}')
        self.assertEqual(bpmn.parse_condition(bpmn.format_condition("n", 3)).value, 3)
        with self.assertRaises(bpmn.BpmnError):
            bpmn.parse_condition("${x > 1}")

    @needs_partner
    def test_normalize_plain_task_and_keep_conforming(self):
        xml = (PARTNER / "scheme.bpmn").read_text(encoding="utf-8")
        self.assertIs(bpmn.normalize(xml, {}), xml)  # partner file untouched
        plain = xml.replace('<bpmn:startEvent id="StartEvent_1">',
                            '<bpmn:task id="Activity_new" name="Новый" />\n<bpmn:startEvent id="StartEvent_1">')
        g = bpmn.parse(bpmn.normalize(plain, {"Activity_new": "jira-send"}))
        self.assertEqual(g.nodes["Activity_new"].task_type, "jira-send")
        self.assertIn('camunda:topic="xray"', bpmn.normalize(plain, {}))

    def test_build_roundtrip(self):
        t = make_track()
        g = t.graph
        self.assertEqual(len(g.tasks), 4)
        self.assertTrue(all(n.task_type in ("task", "jira-send") for n in g.tasks))
        self.assertTrue(all(i in g.bounds for i in g.nodes))
        self.assertTrue(all(t.startswith("Activity_") for t in t.tasks))


# ---------------------------------------------------------------------------
# Track format, storage, import/export
# ---------------------------------------------------------------------------

class TestFormat(TempStorage):
    @needs_partner
    def test_partner_files_byte_identical_after_load_save(self):
        t = storage.read_track_dir(PARTNER)
        storage.save_track(t)
        out = self.tmp / "tracks" / t.id / PARTNER_ACTIVITY
        for name in ("metadata.json", "properties.json", "attachments.json"):
            self.assertEqual((out / name).read_bytes().strip(),
                             (PARTNER / PARTNER_ACTIVITY / name).read_bytes().strip(), name)
        self.assertEqual((self.tmp / "tracks" / t.id / "scheme.bpmn").read_text(encoding="utf-8"),
                         (PARTNER / "scheme.bpmn").read_text(encoding="utf-8"))

    def test_version_bumps_only_on_change_and_snapshots(self):
        t = storage.save_track(make_track())
        self.assertEqual(t.version, 1)
        self.assertEqual(storage.save_track(storage.load_track("t1")).version, 1)
        t.activities[t.tasks[0]].metadata["content"] = "<p>новое</p>"
        self.assertEqual(storage.save_track(t).version, 2)
        self.assertEqual(storage.list_versions("t1"), [1, 2])
        self.assertNotIn("новое", storage.load_track("t1", 1).activities[t.tasks[0]].content)

    def test_removed_activity_folder_deleted(self):
        t = storage.save_track(make_track())
        gone = t.tasks[-1]
        t.activities.pop(gone)
        storage.save_track(t)
        self.assertFalse((self.tmp / "tracks" / "t1" / gone).exists())

    @needs_partner
    def test_zip_roundtrip_pure_export(self):
        t = tracks.sync(storage.read_track_dir(PARTNER))
        data = tracks.export_zip(t, pure=True)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("scheme.bpmn", names)
        self.assertIn(f"{PARTNER_ACTIVITY}/metadata.json", names)
        self.assertFalse([n for n in names if n.endswith(("agent.json", "track.json"))])
        back = tracks.import_zip(data, "copy")
        self.assertEqual(back.activities[PARTNER_ACTIVITY].metadata,
                         json.loads((PARTNER / PARTNER_ACTIVITY / "metadata.json").read_text("utf-8")))
        self.assertEqual(back.track_uuid, "69c3fb3f-7340-427c-aa53-6f57ad452b50")

    def test_zip_import_nested_folder_and_garbage(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("export/scheme.bpmn", "\n" + make_track().bpmn)
            z.writestr("export/tasks.json", "[]")
            z.writestr("__MACOSX/export/._scheme.bpmn", "junk")
        t = tracks.sync(tracks.import_zip(buf.getvalue(), "nested"))
        self.assertEqual(len(t.activities), 4)
        with self.assertRaises(ValueError):
            tracks.import_zip(b"not a zip")

    @needs_partner
    def test_sync_creates_activities_and_keeps_partner_fields(self):
        t = storage.read_track_dir(PARTNER)
        self.assertEqual(len(t.activities), 1)
        s = tracks.sync(t)
        self.assertEqual(len(s.activities), 46)
        new = next(a for k, a in s.activities.items() if k != PARTNER_ACTIVITY)
        self.assertEqual(set(new.metadata), set(t.activities[PARTNER_ACTIVITY].metadata))
        self.assertEqual(s.bpmn, t.bpmn)

    def test_lint(self):
        self.assertEqual(tracks.lint(make_track())["errors"], [])

    @needs_partner
    def test_lint_partner_missing_props(self):
        lint = tracks.lint(tracks.sync(storage.read_track_dir(PARTNER)))
        self.assertTrue(any("New_FP" in e for e in lint["errors"]))  # props of missing activities

    def test_fixtures_load(self):
        for d in sorted((REPO / "tracks").iterdir()):
            t = storage.read_track_dir(d)
            if t.status.value == "published":
                self.assertEqual(tracks.lint(t)["errors"], [], d.name)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TestEngine(TempStorage):
    def setUp(self):
        super().setUp()
        self.track = storage.save_track(make_track())
        self.ids = ids(self.track)
        self.run = engine.start_run(self.track, "u1", "Иван")

    def test_start(self):
        self.assertEqual(self.run.current_step, self.ids["Данные"])
        with self.assertRaises(engine.RuleError):
            engine.start_run(make_track("draft"), "u1")

    def test_required_property_blocks(self):
        with self.assertRaises(engine.RuleError) as cm:
            engine.complete(self.run, self.track)
        self.assertIn("env", str(cm.exception))
        self.run.facts["env"] = "prod"
        self.assertEqual(engine.complete(self.run, self.track), self.ids["Проверки"])

    def test_requirement_then_branch_by_condition(self):
        self.run.facts["env"] = "prod"
        engine.complete(self.run, self.track)
        with self.assertRaises(engine.RuleError):
            engine.complete(self.run, self.track)
        self.run.confirmed[self.run.current_step] = {"ok": "да"}
        self.assertEqual(engine.complete(self.run, self.track), self.ids["Заявка"])

    def test_branch_to_end(self):
        self.run.facts["env"] = "test"
        engine.complete(self.run, self.track)
        self.run.confirmed[self.run.current_step] = {"ok": "да"}
        self.assertIsNone(engine.complete(self.run, self.track))
        self.assertEqual(self.run.status, RunStatus.completed)

    def test_missing_decision_value_reported(self):
        self.run.facts["env"] = "prod"
        engine.complete(self.run, self.track)
        del self.run.facts["env"]
        self.run.confirmed[self.run.current_step] = {"ok": "да"}
        missing = engine.missing_for_step(self.run, self.track)
        self.assertTrue(any("для выбора следующего шага" in m and "env" in m for m in missing))

    def test_jira_send_requires_issue(self):
        self.run.facts["env"] = "prod"
        engine.complete(self.run, self.track)
        self.run.confirmed[self.run.current_step] = {"ok": "да"}
        engine.complete(self.run, self.track)
        self.assertIn("не создана задача Jira этого шага", engine.missing_for_step(self.run, self.track))
        self.run.jira_issues.append(JiraIssueRef(key="REL-1", step_id=self.run.current_step))
        self.assertEqual(engine.complete(self.run, self.track), self.ids["Установка"])

    def test_go_back_only_to_visited(self):
        with self.assertRaises(engine.RuleError):
            engine.go_back(self.run, self.track, self.ids["Заявка"])
        self.run.facts["env"] = "prod"
        engine.complete(self.run, self.track)
        engine.go_back(self.run, self.track, self.ids["Данные"])
        self.assertEqual(self.run.current_step, self.ids["Данные"])

    def test_coerce(self):
        props = self.track.props()
        self.assertEqual(engine.coerce(props["env"], "PROD"), ("prod", None))
        self.assertIsNotNone(engine.coerce(props["env"], "dev")[1])
        self.assertEqual(engine.coerce(props["install_ok"], "да"), (True, None))
        self.assertIsNotNone(engine.coerce({"valueType": "link"}, "confluence")[1])
        self.assertEqual(engine.coerce({"valueVariants": [{"value": "up", "label": "Доработка"}]},
                                       "доработка"), ("up", None))

    def test_links_skip_people_and_progress(self):
        urls = [l.url for l in engine.step_links(self.track, self.ids["Данные"])]
        self.assertEqual(urls, ["https://wiki.example/doc"])
        p = engine.progress(self.run, self.track)
        self.assertEqual(p["current"]["title"], "Данные")
        self.assertEqual([s["state"] for s in p["steps"]][0], "current")
        self.assertGreater(p["percent"], -1)

    def test_pinned_version(self):
        t = storage.load_track("t1")
        t.name = "Новое имя"
        storage.save_track(t)
        self.assertEqual(engine.track_for_run(self.run).name, "Тест")


# ---------------------------------------------------------------------------
# Agent with a scripted model
# ---------------------------------------------------------------------------

def call(name, **args):
    return {"id": f"c{name[:5]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


class ScriptedLLM:
    """Returns the queued assistant messages in order and records what it was shown."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, cfg, messages, tools=None, model="", json_mode=False):
        self.seen.append(messages)
        return self.replies.pop(0)


class TestAgent(TempStorage):
    def setUp(self):
        super().setUp()
        self.track = storage.save_track(make_track())
        self.ids = ids(self.track)
        self.run = engine.start_run(self.track, "u1")
        self.settings = Settings()

    def to_request_step(self):
        self.run.facts["env"] = "prod"
        engine.complete(self.run, self.track)
        self.run.confirmed[self.run.current_step] = {"ok": "да"}
        engine.complete(self.run, self.track)

    def test_save_and_complete(self):
        llm = ScriptedLLM({"tool_calls": [call("save_info", data={"Окружение": "Prod"}),
                                          call("complete_step", summary="данные есть")]},
                          {"content": "Теперь проверки"})
        agent.run_turn(self.run, "ставим на прод", self.settings, chat=llm)
        self.assertEqual(self.run.facts["env"], "prod")
        self.assertEqual(self.run.current_step, self.ids["Проверки"])
        self.assertIn("Текущий шаг: Проверки", llm.seen[-1][0]["content"])
        self.assertIn("Окружение", llm.seen[-1][0]["content"])

    def test_bad_value_rejected_with_hint(self):
        out = agent.run_tool("save_info", {"data": {"env": "dev"}}, self.run, self.track, self.settings)
        self.assertNotIn("env", self.run.facts)
        self.assertIn("допустимые значения", out)

    def test_refusal_reaches_model(self):
        llm = ScriptedLLM({"tool_calls": [call("complete_step")]}, {"content": "Какое окружение?"})
        agent.run_turn(self.run, "дальше", self.settings, chat=llm)
        self.assertEqual(self.run.current_step, self.ids["Данные"])
        self.assertIn("отказано", llm.seen[-1][-1]["content"])

    def test_decision_listed_in_prompt(self):
        self.run.facts["env"] = "prod"
        engine.complete(self.run, self.track)
        prompt = agent.system_prompt(self.run, self.track, self.settings)
        self.assertIn("развилка", prompt)
        self.assertIn("(env) = prod", prompt)

    def test_jira_template_asks_then_creates(self):
        self.to_request_step()
        out = agent.run_tool("create_jira_issue", {}, self.run, self.track, self.settings)
        self.assertIn("window", out)
        self.run.facts["window"] = "пт 20:00"
        out = agent.run_tool("create_jira_issue", {}, self.run, self.track, self.settings)
        self.assertEqual(self.run.jira_issues[0].summary, "Релиз на prod пт 20:00")
        self.assertIn("REL-1", out)

    def test_jira_send_without_template_uses_agent_summary(self):
        self.to_request_step()
        self.track.activities[self.run.current_step].agent.jira = None
        self.settings.jira.default_project = "OPS"
        self.assertIn("summary", agent.run_tool("create_jira_issue", {}, self.run, self.track, self.settings))
        agent.run_tool("create_jira_issue", {"summary": "Заявка"}, self.run, self.track, self.settings)
        self.assertEqual(self.run.jira_issues[0].key, "OPS-1")

    def test_link_manual_issue_when_jira_unavailable(self):
        self.to_request_step()
        self.settings.jira.mode = "server"  # not configured: no base_url
        self.run.facts["window"] = "пт"
        self.assertIn("не настроена", agent.run_tool("create_jira_issue", {}, self.run, self.track, self.settings))
        self.assertIn("ПРОЕКТ-123", agent.run_tool("link_jira_issue", {"issue_key": "rel 5"}, self.run, self.track, self.settings))
        agent.run_tool("link_jira_issue", {"issue_key": "rel-77"}, self.run, self.track, self.settings)
        self.assertEqual(self.run.jira_issues[0].key, "REL-77")
        self.assertEqual(engine.complete(self.run, self.track), self.ids["Установка"])

    def test_link_checks_existence_in_mock(self):
        self.to_request_step()
        out = agent.run_tool("link_jira_issue", {"issue_key": "REL-404"}, self.run, self.track, self.settings)
        self.assertIn("не найдена", out)

    def test_nudge_when_ready_but_not_completed(self):
        llm = ScriptedLLM({"tool_calls": [call("save_info", data={"env": "test"})]},
                          {"content": "Идём дальше."},
                          {"tool_calls": [call("complete_step")]},
                          {"content": "Проверки: тесты зелёные?"})
        agent.run_turn(self.run, "test", self.settings, chat=llm)
        self.assertEqual(self.run.current_step, self.ids["Проверки"])
        self.assertIn("Служебное", llm.seen[2][-1]["content"])

    def test_no_nudge_when_not_ready(self):
        llm = ScriptedLLM({"content": "Какое окружение?"})
        agent.run_turn(self.run, "привет", self.settings, chat=llm)
        self.assertEqual(len(llm.seen), 1)

    def test_keys_normalized(self):
        out = agent.run_tool("save_info", {"data": {"target_env": "test", "extra": 1}},
                             self.run, self.track, self.settings)
        self.assertEqual(self.run.facts["env"], "test")
        self.assertEqual(self.run.facts["extra"], 1)
        self.assertIn("target_env → env", out)

    def test_read_link_only_track_links(self):
        self.assertIn("только ссылки", agent.run_tool(
            "read_link", {"url": "http://169.254.169.254/"}, self.run, self.track, self.settings))
        self.assertIn("только ссылки", agent.run_tool(
            "read_link", {"url": "https://wiki.example/display/~42"}, self.run, self.track, self.settings))

    def test_content_and_links_in_prompt(self):
        with mock.patch.object(agent.links, "fetch_text", return_value="СЕКРЕТНОЕ ПРАВИЛО"):
            prompt = agent.system_prompt(self.run, self.track, self.settings)
        self.assertIn("СЕКРЕТНОЕ ПРАВИЛО", prompt)
        self.assertIn("Смотри инструкцию и Иванова", prompt)

    def test_llm_error_becomes_event(self):
        def boom(*a, **k):
            raise agent.llm.LLMError("нет ключа")
        agent.run_turn(self.run, "привет", self.settings, chat=boom)
        self.assertEqual(self.run.messages[-1].role, "event")

    def test_remember_goes_to_profile(self):
        llm = ScriptedLLM({"tool_calls": [call("save_info", data={"team": "core"}, remember=True)]},
                          {"content": "ok"})
        agent.run_turn(self.run, "я из core", self.settings, chat=llm)
        self.assertEqual(storage.load_profile("u1"), {"team": "core"})

    def test_draft_from_spec(self):
        spec = {"id": "draft-x", "name": "Черновик", "steps": [
            {"id": "s1", "name": "Вопрос", "properties": [{"codeName": "ok", "valueType": "boolean"}],
             "next": [{"to": "s2", "prop": "ok", "value": True, "name": "Да"},
                      {"to": "end", "prop": "ok", "value": False, "name": "Нет"}]},
            {"id": "s2", "name": "Дальше", "type": "jira-send"}]}
        t = agent.draft_track("процесс", self.settings,
                              chat=ScriptedLLM({"content": json.dumps(spec, ensure_ascii=False)}))
        self.assertEqual(len(t.graph.tasks), 2)
        self.assertEqual(tracks.lint(t)["errors"], [])


class TestLinks(unittest.TestCase):
    def test_html_to_text(self):
        text = agent.links.html_to_text("<ol><li><p>раз</p></li><li><p>два</p></li></ol><script>x()</script>")
        self.assertIn("1. раз", text)
        self.assertIn("2. два", text)
        self.assertNotIn("x()", text)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class TestAPI(TempStorage):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app)
        self.llm = ScriptedLLM(*[{"content": f"ответ {i}"} for i in range(10)])
        self.patch = mock.patch.object(agent.llm, "chat", self.llm)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def body(self, t: Track):
        d = t.model_dump(mode="json")
        return {k: d[k] for k in ("id", "name", "description", "agent_instructions", "model",
                                  "bpmn", "tasks", "activities")}

    def test_create_from_template_edit_publish(self):
        c = self.client
        r = c.post("/api/tracks", json={"id": "new", "name": "Новый"})
        self.assertEqual(r.status_code, 201)
        t = r.json()["track"]
        self.assertEqual(len(t["tasks"]), 1)
        self.assertEqual(c.post("/api/tracks", json={"id": "new", "name": "x"}).status_code, 409)
        # the editor adds a plain bpmn:task and marks it jira-send
        xml = t["bpmn"].replace('<bpmn:startEvent id="StartEvent_1">',
                                '<bpmn:task id="Activity_0added1" name="Добавлен" />\n    <bpmn:startEvent id="StartEvent_1">')
        body = {**{k: t[k] for k in ("id", "name", "bpmn", "tasks", "activities")},
                "bpmn": xml, "task_types": {"Activity_0added1": "jira-send"}}
        r = c.put("/api/tracks/new", json=body).json()
        self.assertEqual(r["track"]["version"], 2)
        self.assertIn("Activity_0added1", r["track"]["activities"])
        self.assertEqual(r["track"]["activities"]["Activity_0added1"]["metadata"]["type"], "jira-send")
        self.assertIn("serviceTask id=\"Activity_0added1\"", r["track"]["bpmn"])
        self.assertTrue(any("исходящий" in e for e in r["lint"]["errors"]))  # not connected yet
        self.assertEqual(c.post("/api/tracks/new/publish").status_code, 400)
        self.assertEqual(c.delete("/api/tracks/new").status_code, 200)

    def test_publish_rejects_lint_errors(self):
        t = make_track("draft")
        xml = t.bpmn.replace('<bpmn:startEvent id="StartEvent_1">', '<bpmn:startEvent id="StartEvent_x">', 1)
        self.client.post("/api/tracks", json={**self.body(t), "bpmn": xml})
        self.assertEqual(self.client.post("/api/tracks/t1/publish").status_code, 400)

    def test_import_export(self):
        c = self.client
        data = tracks.export_zip(make_track(), pure=True)
        r = c.post("/api/tracks/import?id=partner", content=data)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len(r.json()["track"]["activities"]), 4)
        self.assertEqual(c.post("/api/tracks/import?id=partner", content=data).status_code, 409)
        self.assertEqual(c.post("/api/tracks/import?id=partner&replace=true", content=data).status_code, 201)
        self.assertEqual(c.post("/api/tracks/import", content=b"junk").status_code, 422)
        z = c.get("/api/tracks/partner/export?pure=true")
        self.assertEqual(z.headers["content-type"], "application/zip")
        self.assertIn("scheme.bpmn", zipfile.ZipFile(io.BytesIO(z.content)).namelist())

    def test_run_chat_stats(self):
        c = self.client
        c.post("/api/tracks", json=self.body(make_track()))
        c.post("/api/tracks/t1/publish")
        self.assertEqual(len(c.get("/api/catalog").json()), 1)
        v = c.post("/api/runs", json={"track_id": "t1", "user_id": "u1", "user_name": "Иван"}).json()
        rid = v["run"]["run_id"]
        self.assertEqual(v["progress"]["current"]["title"], "Данные")
        v = c.post(f"/api/runs/{rid}/messages", json={"text": "привет"}).json()
        self.assertEqual(v["run"]["messages"][-1]["text"], "ответ 1")
        ov = c.get("/api/stats/overview").json()
        self.assertEqual(ov["in_progress"][0]["current_step_title"], "Данные")
        st = c.get("/api/stats/tracks/t1").json()
        self.assertEqual(st["steps"][0]["active_now"], 1)
        self.assertEqual(c.post(f"/api/runs/{rid}/cancel").json()["run"]["status"], "cancelled")

    def test_lint_endpoint_and_bad_scheme(self):
        t = make_track()
        self.assertEqual(self.client.post("/api/tracks/lint", json=self.body(t)).json()["errors"], [])
        bad = self.client.post("/api/tracks/lint", json={**self.body(t), "bpmn": "<x"}).json()
        self.assertTrue(bad["errors"])

    def test_settings_masked_and_token(self):
        s = Settings().model_dump()
        s["agent"]["api_key"] = "k"
        self.assertEqual(self.client.put("/api/settings", json=s).json()["agent"]["api_key"], "••••••••")
        with mock.patch.dict("os.environ", {"ANALYST_TOKEN": "t0k"}):
            self.assertEqual(self.client.get("/api/tracks").status_code, 401)
            self.assertEqual(self.client.get("/api/catalog").status_code, 200)


if __name__ == "__main__":
    unittest.main()
