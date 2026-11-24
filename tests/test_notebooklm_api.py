import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ntu_cool_materials import cli
from ntu_cool_materials.notebooklm import NotebookLMError, STATE_NAME, build_import_plan, import_plan
from ntu_cool_materials.notebooklm_api import (
    NotebookLMAPIAdapter, run_api_import, resolve_api_storage, remember_api_storage,
)
from ntu_cool_materials.storage import atomic_write_text

URL = "https://notebooklm.google.com/notebook/owned"
STATUS = SimpleNamespace(READY=2, PREPARING=5, PROCESSING=1, ERROR=3)


class FakeAPI:
    def __init__(self):
        self.rows = []
        self.uploads = 0
        self.created = 0
        self.fail_wait = False
        self.fail_upload = False
        self.renames = 0
        self.settings = SimpleNamespace(
            get_account_limits=AsyncMock(return_value=SimpleNamespace(source_limit=None))
        )
        self.notebooks = SimpleNamespace(list=self.list_notebooks, create=self.create)
        self.sources = SimpleNamespace(list=self.list_sources, add_file=self.add_file,
                                       wait_until_ready=self.wait, rename=self.rename_source, delete=self.delete_source)

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    async def delete_source(self, notebook_id, source_id):
        self.rows = [r for r in self.rows if r.id != source_id]

    async def rename_source(self, notebook_id, source_id, new_title):
        self.renames += 1
        row = next(r for r in self.rows if r.id == source_id)
        row.title = new_title
        return row

    async def list_notebooks(self):
        return [SimpleNamespace(id="owned", title="My course", is_owner=True),
                SimpleNamespace(id="featured", title="Recommendation", is_owner=False)]

    async def create(self, title):
        self.created += 1
        return SimpleNamespace(id="new", title=title, is_owner=True)

    async def list_sources(self, notebook_id, *, strict):
        assert strict
        return list(self.rows)

    async def add_file(self, notebook_id, path, *, title):
        self.uploads += 1
        assert path.read_bytes()
        if self.fail_upload:
            raise RuntimeError("secret-cookie-value")
        row = SimpleNamespace(id=f"source-{self.uploads}", title=title, status=1)
        self.rows.append(row)
        return row

    async def wait(self, notebook_id, source_id, *, timeout):
        if self.fail_wait:
            raise RuntimeError("secret-cookie-value")
        row = next(r for r in self.rows if r.id == source_id)
        row.status = 2
        return row


class StorageTests(unittest.TestCase):
    def test_remembered_login_works_from_another_directory(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder).resolve()
            login = root / "project/storage_state.json"
            login.parent.mkdir()
            login.write_text("synthetic-secret")
            with patch("ntu_cool_materials.notebooklm_api.Path.home", return_value=root), \
                 patch("ntu_cool_materials.notebooklm_api.secrets_dir", return_value=root / "elsewhere/.secrets"):
                remember_api_storage(login)
                default_login = root / "elsewhere/.secrets/notebooklm-api/profiles/default/storage_state.json"
                default_login.parent.mkdir(parents=True)
                default_login.write_text("{}")
                self.assertEqual(resolve_api_storage(), login)
                config = root / ".ntu-cool-gcm/notebooklm-storage.json"
                self.assertNotIn("synthetic-secret", config.read_text())
                with patch.dict(os.environ, {"NOTEBOOKLM_PROFILE": "other"}):
                    self.assertIsNone(resolve_api_storage())
                login.unlink()
                self.assertEqual(resolve_api_storage(), default_login)
                config.write_text("broken")
                self.assertEqual(resolve_api_storage(), default_login)

    def test_existing_local_login_and_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder).resolve()
            login = root / ".secrets/notebooklm-api/profiles/default/storage_state.json"
            login.parent.mkdir(parents=True)
            login.write_text("{}")
            with patch("ntu_cool_materials.notebooklm_api.secrets_dir", return_value=root / ".secrets"), \
                 patch("ntu_cool_materials.notebooklm_api.Path.home", return_value=root):
                self.assertEqual(resolve_api_storage(), login)
                explicit = root / "explicit.json"
                self.assertEqual(resolve_api_storage(explicit), explicit)
                for name in ("NOTEBOOKLM_HOME", "NOTEBOOKLM_PROFILE", "NOTEBOOKLM_AUTH_JSON"):
                    with patch.dict(os.environ, {name: "configured"}):
                        self.assertIsNone(resolve_api_storage())
                        self.assertEqual(resolve_api_storage(explicit), explicit)

    def test_home_fallback_and_sdk_default(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder).resolve()
            with patch("ntu_cool_materials.notebooklm_api.secrets_dir", return_value=root / ".ntu-cool-gcm/.secrets"), \
                 patch("ntu_cool_materials.notebooklm_api.Path.home", return_value=root):
                self.assertIsNone(resolve_api_storage())
                login = root / ".ntu-cool-gcm/.secrets/notebooklm-api/profiles/default/storage_state.json"
                login.parent.mkdir(parents=True)
                login.write_text("{}")
                self.assertEqual(resolve_api_storage(), login)


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "lecture.md").write_text("Course material", encoding="utf-8")
        self.plan = build_import_plan(self.root)
        self.api = FakeAPI()
        self.enterContext(patch.dict("sys.modules", {"notebooklm.types": SimpleNamespace(SourceStatus=STATUS)}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def adapter(self, **kwargs):
        return NotebookLMAPIAdapter({s.title for s in self.plan.sources},
                                     context_factory=lambda path: self.api, **kwargs)

    def run_import(self):
        with self.adapter() as adapter:
            return import_plan(self.plan, adapter, notebook_url=URL)

    def test_upload_verified_then_rerun_deduplicates(self):
        self.assertEqual(self.run_import().uploaded, 1)
        self.assertEqual(self.run_import().unchanged, 1)
        self.assertEqual(self.api.uploads, 1)
        self.assertIn(URL, json.loads((self.root / STATE_NAME).read_text())["notebooks"])

    def test_timeout_keeps_pending_then_reconciles_without_upload(self):
        self.api.fail_wait = True
        with self.assertRaisesRegex(NotebookLMError, "pending"):
            self.run_import()
        self.api.fail_wait = False
        self.assertEqual(self.run_import().unchanged, 1)
        self.assertEqual(self.api.uploads, 1)

    def test_missing_pending_upload_is_retried_only_on_next_run(self):
        self.api.fail_upload = True
        with self.assertRaises(NotebookLMError) as failure:
            self.run_import()
        self.assertNotIn("secret", str(failure.exception))
        self.assertEqual(self.api.uploads, 1)
        self.api.fail_upload = False
        self.assertEqual(self.run_import().uploaded, 1)
        self.assertEqual(self.api.uploads, 2)

    def test_pending_without_id_is_recovered_from_verified_remote_content(self):
        self.api.fail_upload = True
        with self.assertRaises(NotebookLMError):
            self.run_import()
        self.api.fail_upload = False
        source = self.plan.sources[0]
        self.api.rows.append(SimpleNamespace(id="recovered", title=source.title, status=2))
        with patch.object(NotebookLMAPIAdapter, "matches_original", return_value=True) as verify:
            self.assertEqual(self.run_import().unchanged, 1)
        verify.assert_called_once_with("recovered", source)
        state = json.loads((self.root / STATE_NAME).read_text())
        self.assertEqual(state["notebooks"][URL]["sources"][source.digest]["source_id"], "recovered")
        self.assertEqual(self.api.uploads, 1)

    def test_failed_revision_does_not_match_original_title_in_import_or_verify(self):
        self.assertEqual(self.run_import().uploaded, 1)
        (self.root / "lecture.md").write_text("Revised material", encoding="utf-8")
        self.plan = build_import_plan(self.root)
        self.api.fail_upload = True
        with self.assertRaises(NotebookLMError):
            self.run_import()
        self.api.fail_upload = False
        self.api.rows.append(SimpleNamespace(id="unverified", title="lecture (2).md", status=2))
        with self.assertRaisesRegex(NotebookLMError, "先前上傳結果不明"):
            self.run_import()
        with patch("ntu_cool_materials.notebooklm_api.api_client_context", return_value=self.api):
            with self.assertRaises(NotebookLMError):
                run_api_import(self.root, notebook_url=URL, verify_only=True)
        records = json.loads((self.root / STATE_NAME).read_text())["notebooks"][URL]["sources"]
        self.assertEqual(records[self.plan.sources[0].digest]["status"], "pending")
        self.assertEqual(self.api.uploads, 2)

    def test_unowned_notebook_refused_before_upload(self):
        with self.adapter() as adapter:
            with self.assertRaisesRegex(NotebookLMError, "我的筆記本"):
                import_plan(self.plan, adapter, notebook_url=URL.replace("owned", "featured"))
        self.assertEqual(self.api.uploads, 0)

    def test_same_title_error_source_refuses_duplicate(self):
        self.api.rows = [SimpleNamespace(id="failed", title=self.plan.sources[0].title, status=3)]
        with self.assertRaisesRegex(NotebookLMError, "狀態錯誤"):
            self.run_import()
        self.assertEqual(self.api.uploads, 0)

    def test_capacity_counts_all_api_sources(self):
        self.api.rows = [SimpleNamespace(id=str(i), title=str(i), status=2) for i in range(50)]
        with self.assertRaisesRegex(NotebookLMError, "來源數"):
            self.run_import()
        self.assertEqual(self.api.uploads, 0)

    def test_api_import_detects_account_limit_by_default(self):
        self.api.rows = [SimpleNamespace(id=str(i), title=str(i), status=2) for i in range(50)]
        self.api.settings.get_account_limits.return_value.source_limit = 300
        with patch(
            "ntu_cool_materials.notebooklm_api.api_client_context", return_value=self.api
        ):
            self.assertEqual(run_api_import(self.root, notebook_url=URL).uploaded, 1)
        self.api.settings.get_account_limits.assert_awaited_once()

    def test_api_import_falls_back_when_account_limit_is_missing(self):
        self.api.rows = [SimpleNamespace(id=str(i), title=str(i), status=2) for i in range(50)]
        with patch("ntu_cool_materials.notebooklm_api.api_client_context", return_value=self.api):
            with self.assertRaisesRegex(NotebookLMError, "來源數"):
                run_api_import(self.root, notebook_url=URL)
        self.assertEqual(self.api.uploads, 0)

    def test_api_import_explicit_limit_skips_detection(self):
        with patch("ntu_cool_materials.notebooklm_api.api_client_context", return_value=self.api):
            self.assertEqual(run_api_import(self.root, notebook_url=URL, max_sources=300).uploaded, 1)
        self.api.settings.get_account_limits.assert_not_awaited()

    def test_user_cancellation_never_creates_or_uploads(self):
        with self.adapter(interactive=True) as adapter, patch("builtins.input", return_value="q"):
            with self.assertRaisesRegex(NotebookLMError, "取消"):
                import_plan(self.plan, adapter)
        self.assertEqual(self.api.created, 0)
        self.assertEqual(self.api.uploads, 0)

    def test_dry_run_never_initializes_api(self):
        with patch("ntu_cool_materials.notebooklm_api.api_client_context") as factory:
            run_api_import(self.root, dry_run=True)
        factory.assert_not_called()
        self.assertFalse((self.root / STATE_NAME).exists())

    def test_cli_defaults_to_api_and_passes_storage(self):
        with patch("ntu_cool_materials.notebooklm_api.run_api_import") as run:
            self.assertEqual(cli.main(["notebooklm", "--course-dir", str(self.root),
                                       "--notebooklm-storage", "auth.json"]), 0)
        self.assertEqual(run.call_args.kwargs["storage_path"], Path("auth.json"))

    def test_course_id_selects_exact_folder_without_prompt(self):
        current = self.root / 'Current course (64660)'
        current.mkdir()
        (self.root / 'Old course (6466)').mkdir()
        with patch('builtins.input', side_effect=AssertionError('must not prompt')), \
             patch('ntu_cool_materials.notebooklm_api.run_api_import') as run:
            code = cli.main(['notebooklm', '--course-id', '64660', '--out', str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], current)

    def test_ambiguous_course_id_stops_without_prompt_or_upload(self):
        (self.root / 'One (64660)').mkdir()
        (self.root / 'Two (64660)').mkdir()
        with patch('builtins.input', side_effect=AssertionError('must not prompt')), \
             patch('ntu_cool_materials.notebooklm_api.run_api_import') as run:
            self.assertEqual(cli.main(['notebooklm', '--course-id', '64660', '--out', str(self.root)]), 1)
        run.assert_not_called()

    def test_verify_only_missing_source_cannot_upload_or_write_journal(self):
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            with self.assertRaisesRegex(NotebookLMError, '未重送'):
                run_api_import(self.root, notebook_url=URL, verify_only=True)
        self.assertEqual(self.api.uploads, 0)
        self.assertEqual(self.api.created, 0)
        self.assertFalse((self.root / STATE_NAME).exists())

    def test_verify_only_ready_sources_preserves_journal_bytes(self):
        self.run_import()
        before = (self.root / STATE_NAME).read_bytes()
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            result = run_api_import(self.root, verify_only=True)
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual((self.root / STATE_NAME).read_bytes(), before)

    def test_manually_replaced_source_with_same_title_does_not_verify(self):
        self.run_import()
        self.api.rows[0].id = "manual"
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            with self.assertRaisesRegex(NotebookLMError, "來源 ID"):
                run_api_import(self.root, verify_only=True)
        self.assertEqual(self.api.uploads, 1)

    def test_legacy_complete_record_is_retained_without_claiming_verified(self):
        self.run_import()
        journal = self.root / STATE_NAME
        state = json.loads(journal.read_text())
        entry = state["notebooks"][URL]["sources"][self.plan.sources[0].digest]
        entry.pop("source_id")
        atomic_write_text(journal, json.dumps(state))

        result = self.run_import()
        self.assertEqual((result.uploaded, result.unchanged, result.unverified), (0, 0, 1))
        self.assertEqual(self.api.uploads, 1)
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            with self.assertRaisesRegex(NotebookLMError, "來源 ID"):
                run_api_import(self.root, verify_only=True)

    def test_legacy_id_requires_identical_original_and_is_persisted(self):
        from unittest.mock import MagicMock
        self.run_import()
        journal = self.root / STATE_NAME
        state = json.loads(journal.read_text())
        digest = self.plan.sources[0].digest
        entry = state["notebooks"][URL]["sources"][digest]
        original_id = entry.pop("source_id")
        atomic_write_text(journal, json.dumps(state))
        self.api.rows[0].download_url = "https://contribution.usercontent.google.com/download?id=test"
        self.api.auth = SimpleNamespace(cookie_jar=None)
        httpx = MagicMock()
        response = httpx.Client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
        response.status_code = 200
        data = self.plan.sources[0].path.read_bytes()
        response.iter_bytes.side_effect = [[b"x" * len(data)], [data]]
        with patch.dict("sys.modules", {"httpx": httpx}):
            self.assertEqual(self.run_import().unverified, 1)
            self.assertEqual(self.run_import().unchanged, 1)
            self.assertEqual(self.run_import().unchanged, 1)
        self.assertEqual(response.iter_bytes.call_count, 2)
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(json.loads(journal.read_text())["notebooks"][URL]["sources"][digest]["source_id"], original_id)

    @unittest.skipUnless(importlib.util.find_spec("markdown_it"), "requires the NotebookLM Markdown parser")
    def test_legacy_announcement_verifies_reformatted_markdown_and_links(self):
        from unittest.mock import MagicMock

        path = self.announcement_file()
        path.write_text("## Update\n\n- Read [this](<https://example.com/a>)\n  before class.\n", encoding="utf-8")
        plan = build_import_plan(self.root)
        source = next(s for s in plan.sources if s.path == path)
        journal = self.root / STATE_NAME
        state = {"version": 1, "notebooks": {URL: {"sources": {
            source.digest: {"title": "announcements", "status": "complete"},
        }}}}
        atomic_write_text(journal, json.dumps(state))
        self.api.rows.append(SimpleNamespace(
            id="legacy", title="announcements", status=2,
            download_url="https://contribution.usercontent.google.com/download?id=test",
        ))
        self.api.auth = SimpleNamespace(cookie_jar=None)
        httpx = MagicMock()
        response = httpx.Client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_bytes.side_effect = [
            [b"#### Update\n\n* Read [this](https://example.com/b) before class.\n"],
            [b"#### Update\n\n* Read [this](https://example.com/a) before class.\n"],
        ]
        with patch.dict("sys.modules", {"httpx": httpx}), \
             patch("ntu_cool_materials.notebooklm_api.api_client_context", return_value=self.api):
            self.assertEqual(run_api_import(self.root, notebook_url=URL, plan=plan).unverified, 1)
            self.assertEqual(run_api_import(self.root, notebook_url=URL, plan=plan).unverified, 0)
        self.assertEqual(json.loads(journal.read_text())["notebooks"][URL]["sources"][source.digest]["source_id"], "legacy")

    def test_workbook_export_checks_all_cells_and_sheet_names(self):
        from ntu_cool_materials.notebooklm_api import _workbook_export
        path = self.root / "week2" / "prices.xlsx.md"
        path.parent.mkdir()
        path.write_text("# prices.xlsx\n\n## Bonds\n| Name | Rate |\n| --- | --- |\n| A\\_B | 0.05 |\n", encoding="utf8")
        source = next(s for s in build_import_plan(self.root).sources if s.path.samefile(path))
        expected = b"### prices.xlsx\n#### Bonds\n| Name | Rate |\n| ------ | ------ |\n| A_B | 0.05 |\n\n"
        self.assertEqual(_workbook_export(source), expected)
        path.write_text(path.read_text().replace("0.05", "0.06"))
        self.assertIsNone(_workbook_export(source))
        changed = next(s for s in build_import_plan(self.root).sources if s.path.samefile(path))
        self.assertNotEqual(_workbook_export(changed), expected)

    def test_readonly_adapter_refuses_creation_and_upload(self):
        with self.adapter() as adapter:
            adapter.read_only = True
            with self.assertRaises(NotebookLMError):
                adapter.open_notebook(None, 'test')
            with self.assertRaises(NotebookLMError):
                adapter.upload(self.root / 'lecture.md', 'test', on_created=self.fail)
        self.assertEqual(self.api.created, 0)
        self.assertEqual(self.api.uploads, 0)

    def test_interactive_auto_login_retries_on_auth_failure(self):
        calls = []
        def failing_then_succeeding_context(storage_path):
            calls.append("called")
            if len(calls) == 1:
                raise RuntimeError("login expired")
            return self.api

        with patch("ntu_cool_materials.console.stdin_is_interactive", return_value=True), \
             patch("ntu_cool_materials.notebooklm_api.api_client_context", side_effect=failing_then_succeeding_context), \
             patch("ntu_cool_materials.notebooklm_api.trigger_interactive_login", return_value=True) as login_mock:
            adapter = NotebookLMAPIAdapter({s.title for s in self.plan.sources})
            with adapter:
                self.assertIsNotNone(adapter.client)
            login_mock.assert_called_once()
            self.assertEqual(len(calls), 2)

    def test_interactive_auto_login_failure_raises_notebooklm_error(self):
        with patch("ntu_cool_materials.console.stdin_is_interactive", return_value=True), \
             patch("ntu_cool_materials.notebooklm_api.api_client_context", side_effect=RuntimeError("no credentials")), \
             patch("ntu_cool_materials.notebooklm_api.trigger_interactive_login", return_value=False) as login_mock:
            adapter = NotebookLMAPIAdapter({s.title for s in self.plan.sources})
            with self.assertRaisesRegex(NotebookLMError, "API 登入未就緒或已過期"):
                with adapter:
                    pass
            login_mock.assert_called_once()

    def test_browser_exit_success_is_not_reported_as_verified_sign_in(self):
        with patch("ntu_cool_materials.console.stdin_is_interactive", return_value=True), \
             patch("ntu_cool_materials.notebooklm_api.api_client_context", side_effect=RuntimeError("no credentials")) as connect, \
             patch("ntu_cool_materials.notebooklm_api.trigger_interactive_login", return_value=True), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            adapter = NotebookLMAPIAdapter(set())
            with self.assertRaisesRegex(NotebookLMError, "仍無法驗證登入"):
                with adapter:
                    pass
            self.assertEqual(connect.call_count, 2)
            self.assertNotIn("API 登入已驗證", output.getvalue())

    def test_interactive_select_existing_notebook(self):
        with self.adapter(interactive=True) as adapter, patch("builtins.input", return_value="1"):
            result = import_plan(self.plan, adapter)
            self.assertEqual(result.notebook_url, "https://notebook.google.com/notebook/owned")
            self.assertEqual(self.api.created, 0)
            self.assertEqual(self.api.uploads, 1)


    def test_interactive_select_create_new_notebook(self):
        with self.adapter(interactive=True) as adapter, patch("builtins.input", return_value="0"):
            result = import_plan(self.plan, adapter)
            self.assertEqual(result.notebook_url, "https://notebook.google.com/notebook/new")
            self.assertEqual(self.api.created, 1)
            self.assertEqual(self.api.uploads, 1)

    def test_legacy_hashed_sources_are_automatically_renamed(self):
        legacy_title = "lecture [11b37d6cafc0].md"
        self.api.rows.append(SimpleNamespace(id="src-old", title=legacy_title, status=2))
        with self.adapter() as adapter:
            ready = adapter.ready_sources()
            self.assertEqual(ready.get("src-old"), "lecture.md")
            self.assertEqual(self.api.renames, 1)
            self.assertEqual(self.api.rows[0].title, "lecture.md")

    def test_imported_sources_do_not_contain_hash_code(self):
        with self.adapter() as adapter:
            import_plan(self.plan, adapter)
            self.assertEqual(self.api.uploads, 1)
            self.assertEqual(self.api.rows[0].title, "lecture.md")
            self.assertNotIn("[", self.api.rows[0].title)

    def test_legacy_announcements_title_is_automatically_renamed(self):
        adapter = NotebookLMAPIAdapter({"announcements"}, context_factory=lambda path: self.api)
        self.api.rows.append(SimpleNamespace(id="src-ann", title="announcements - announcements.md", status=2))
        with adapter:
            ready = adapter.ready_sources()
            self.assertEqual(ready.get("src-ann"), "announcements")
            self.assertEqual(self.api.rows[-1].title, "announcements")





    def announcement_file(self):
        path = self.root / "announcements" / "announcements.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("first announcement", encoding="utf-8")
        return path

    def test_updated_statistics_syllabus_replaces_managed_copy_and_restores_title(self):
        path = self.root / "module1" / "Syllabus_Statistics_Fall26 (NTU).md"
        path.parent.mkdir()
        path.write_text("Original syllabus", encoding="utf-8")
        title = "module1 - " + path.name
        with patch("ntu_cool_materials.notebooklm_api.api_client_context", return_value=self.api):
            run_api_import(self.root, notebook_url=URL)
            old = next(row for row in self.api.rows if row.title == title)
            path.write_text("Updated syllabus", encoding="utf-8")
            self.assertEqual(run_api_import(self.root, notebook_url=URL).uploaded, 1)
            rows = [row for row in self.api.rows if "Syllabus" in row.title]
            self.assertEqual([row.title for row in rows], [title])
            self.assertNotEqual(rows[0].id, old.id)
            self.assertEqual(run_api_import(self.root, notebook_url=URL).uploaded, 0)

    def test_updated_announcements_replace_only_managed_source_after_upload(self):
        path = self.announcement_file()
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            run_api_import(self.root, notebook_url=URL)
            self.api.rows.extend([
                SimpleNamespace(id="old2", title="announcements (2)", status=2),
                SimpleNamespace(id="old3", title="announcements (3)", status=2),
                SimpleNamespace(id="attachment", title="announcements - attachments - sample.pdf", status=2),
            ])
            path.write_text("updated announcement", encoding="utf-8")
            result = run_api_import(self.root, max_sources=6)
            self.assertEqual(result.uploaded, 1)
            self.assertEqual({r.id for r in self.api.rows},
                             {"source-1", "source-3", "old2", "old3", "attachment"})
            result = run_api_import(self.root, max_sources=6)
            self.assertEqual(result.uploaded, 0)
            # Reverting to earlier content still replaces the current summary.
            path.write_text("first announcement", encoding="utf-8")
            self.assertEqual(run_api_import(self.root, max_sources=6).uploaded, 1)

    def test_announcement_ready_with_md_suffix_on_upload_and_after_interruption(self):
        path = self.announcement_file()
        (self.root / "lecture.md").unlink()
        wait = self.api.sources.wait_until_ready

        async def wait_with_extension(notebook_id, source_id, *, timeout):
            row = await wait(notebook_id, source_id, timeout=timeout)
            row.title += ".md"
            return row

        self.api.sources.wait_until_ready = wait_with_extension
        with patch("ntu_cool_materials.notebooklm_api.api_client_context", return_value=self.api):
            self.assertEqual(run_api_import(self.root, notebook_url=URL).uploaded, 1)
            path.write_text("Updated announcement", encoding="utf-8")
            self.api.fail_wait = True
            with self.assertRaisesRegex(NotebookLMError, "pending"):
                run_api_import(self.root, notebook_url=URL)
            self.api.fail_wait = False
            self.api.rows[-1].title += ".md"
            self.api.rows[-1].status = 2
            self.assertEqual(run_api_import(self.root, notebook_url=URL).unchanged, 1)
        self.assertEqual(self.api.uploads, 2)
        self.assertEqual([row.id for row in self.api.rows], ["source-2"])

    def test_announcement_delete_failure_is_retried_without_reupload(self):
        (self.root / "lecture.md").unlink()
        path = self.announcement_file()
        async def fail_delete(*args):
            raise RuntimeError("delete failed")
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            run_api_import(self.root, notebook_url=URL)
            path.write_text("updated", encoding="utf-8")
            self.api.sources.delete = fail_delete
            with self.assertRaises(NotebookLMError):
                run_api_import(self.root, notebook_url=URL)
            self.assertEqual({r.id for r in self.api.rows}, {"source-1", "source-2"})
            self.api.sources.delete = self.api.delete_source
            self.assertEqual(run_api_import(self.root).unchanged, 1)
        self.assertEqual(self.api.uploads, 2)
        self.assertEqual([r.id for r in self.api.rows], ["source-2"])

    def test_announcement_delete_must_be_confirmed(self):
        (self.root / "lecture.md").unlink()
        path = self.announcement_file()
        async def no_delete(*args):
            pass
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            run_api_import(self.root, notebook_url=URL)
            path.write_text("updated", encoding="utf-8")
            self.api.sources.delete = no_delete
            with self.assertRaisesRegex(NotebookLMError, "舊公告"):
                run_api_import(self.root, notebook_url=URL)
        self.assertEqual(self.api.uploads, 2)

    def test_readonly_refuses_announcement_deletion(self):
        self.api.rows.append(SimpleNamespace(id="old", title="announcements", status=2))
        with self.adapter() as adapter:
            adapter.read_only = True
            with self.assertRaises(NotebookLMError):
                adapter.delete_sources({"old"}, keep_source_id="new")
        self.assertEqual(len(self.api.rows), 1)

    def test_unmanaged_announcement_duplicate_is_retained(self):
        self.announcement_file()
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            run_api_import(self.root, notebook_url=URL)
            self.api.rows.append(SimpleNamespace(id="duplicate", title="announcements (2)", status=2))
            self.assertEqual(run_api_import(self.root).uploaded, 0)
        self.assertIn("duplicate", {r.id for r in self.api.rows})

    def test_failed_announcement_is_retried_after_confirming_absence(self):
        (self.root / "lecture.md").unlink()
        path = self.announcement_file()
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            run_api_import(self.root, notebook_url=URL)
            self.api.rows.append(SimpleNamespace(id="manual", title="announcements (2)", status=2))
            path.write_text("updated", encoding="utf-8")
            self.api.fail_upload = True
            with self.assertRaises(NotebookLMError):
                run_api_import(self.root, notebook_url=URL)
            self.assertEqual({r.id for r in self.api.rows}, {"source-1", "manual"})
            self.api.fail_upload = False
            self.assertEqual(run_api_import(self.root).uploaded, 1)
        self.assertEqual(self.api.uploads, 3)
        self.assertEqual({r.id for r in self.api.rows}, {"source-3", "manual"})

    def test_full_notebook_keeps_old_announcement(self):
        (self.root / "lecture.md").unlink()
        path = self.announcement_file()
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            run_api_import(self.root, notebook_url=URL)
            path.write_text("updated", encoding="utf-8")
            with self.assertRaisesRegex(NotebookLMError, "來源數"):
                run_api_import(self.root, max_sources=1)
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual([r.id for r in self.api.rows], ["source-1"])

    def test_announcement_releases_slot_for_other_uploads_after_ready(self):
        path = self.announcement_file()
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            run_api_import(self.root, notebook_url=URL)
            path.write_text("updated", encoding="utf-8")
            (self.root / "notes.md").write_text("new notes", encoding="utf-8")
            result = run_api_import(self.root, max_sources=3)
        self.assertEqual(result.uploaded, 2)
        self.assertEqual({r.title for r in self.api.rows}, {"lecture.md", "announcements", "notes.md"})

    def test_wait_failure_preserves_old_announcement_and_reconciles_by_id(self):
        (self.root / "lecture.md").unlink()
        path = self.announcement_file()
        with patch('ntu_cool_materials.notebooklm_api.api_client_context', return_value=self.api):
            run_api_import(self.root, notebook_url=URL)
            self.api.fail_wait = True
            path.write_text("updated", encoding="utf-8")
            with self.assertRaises(NotebookLMError):
                run_api_import(self.root, max_sources=2)
            self.assertEqual({r.id for r in self.api.rows}, {"source-1", "source-2"})
            self.api.fail_wait = False
            self.assertEqual(run_api_import(self.root, max_sources=2).unchanged, 1)
        self.assertEqual(self.api.uploads, 2)
        self.assertEqual([r.id for r in self.api.rows], ["source-2"])
