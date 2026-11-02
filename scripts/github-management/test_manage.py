import copy
import json
import subprocess
import unittest
from unittest.mock import patch

import manage


CONFIG = json.loads((manage.ROOT / "desired-state.json").read_text(encoding="utf-8"))


def settled():
    return {
        "repo": "BardForge/bardforge",
        "labels": [{key: row[key] for key in ("name", "color", "description")} for row in CONFIG["labels"]],
        "milestones": [{"number": index + 1, "title": row["title"], "description": row["description"],
                        "state": "open", "due_on": None} for index, row in enumerate(CONFIG["milestones"])],
        "alias_assignments": {},
    }


class PlannerTests(unittest.TestCase):
    def test_catalog_and_idempotency(self):
        manage.validate_config(CONFIG)
        self.assertEqual(len(CONFIG["labels"]), 38)
        self.assertEqual(manage.plan(CONFIG, settled())["operations"], [])

    def test_rename_in_place_instead_of_creating_synonym(self):
        snapshot = settled()
        docs = next(row for row in snapshot["labels"] if row["name"] == "type:documentation")
        docs["name"] = "type:docs"
        snapshot["alias_assignments"]["type:docs"] = [{"number": 17, "labels": ["type:docs", "unrelated"]}]
        operations = manage.plan(CONFIG, snapshot)["operations"]
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["method"], "PATCH")
        self.assertTrue(operations[0]["endpoint"].endswith("labels/type%3Adocs"))
        self.assertEqual(operations[0]["body"]["new_name"], "type:documentation")
        self.assertNotIn("name", operations[0]["body"])

    def test_collision_adds_canonical_preserves_alias_and_other_assignments(self):
        snapshot = settled()
        snapshot["labels"].append({"name": "type:test", "color": "ffffff", "description": "old"})
        snapshot["alias_assignments"]["type:test"] = [
            {"number": 2, "labels": ["type:test", "unrelated"]},
            {"number": 3, "labels": ["type:test", "type:maintenance"]},
        ]
        planned = manage.plan(CONFIG, snapshot)
        self.assertEqual(len(planned["operations"]), 1)
        self.assertEqual(planned["operations"][0]["method"], "POST")
        self.assertTrue(planned["operations"][0]["endpoint"].endswith("issues/2/labels"))
        self.assertEqual(planned["operations"][0]["body"], {"labels": ["type:maintenance"]})
        self.assertEqual(planned["retained_aliases"][0]["alias"], "type:test")
        snapshot["alias_assignments"]["type:test"][0]["labels"].append("type:maintenance")
        self.assertEqual(manage.plan(CONFIG, snapshot)["operations"], [])

    def test_multiple_aliases_rename_one_then_migrate_the_rest(self):
        snapshot = settled()
        snapshot["labels"] = [row for row in snapshot["labels"] if row["name"] != "type:maintenance"]
        for name in ["type:chore", "type:test"]:
            snapshot["labels"].append({"name": name, "color": "ffffff", "description": "old"})
            snapshot["alias_assignments"][name] = [{"number": 5, "labels": [name]}]
        operations = manage.plan(CONFIG, snapshot)["operations"]
        self.assertEqual([op["method"] for op in operations], ["PATCH", "POST"])
        self.assertTrue(operations[0]["endpoint"].endswith("type%3Achore"))
        self.assertTrue(operations[1]["endpoint"].endswith("issues/5/labels"))

    def test_unknown_labels_are_not_touched(self):
        snapshot = settled()
        snapshot["labels"].append({"name": "custom-label", "color": "ffffff", "description": "custom"})
        self.assertEqual(manage.plan(CONFIG, snapshot)["operations"], [])

    def test_milestone_rename_retains_existing_state_date_and_assignments(self):
        snapshot = settled()
        snapshot["milestones"][0].update(title="v0.1.0", state="closed", due_on="2030-01-01T00:00:00Z")
        operation = manage.plan(CONFIG, snapshot)["operations"][0]
        self.assertEqual(operation["method"], "PATCH")
        self.assertTrue(operation["endpoint"].endswith("milestones/1"))
        self.assertEqual(set(operation["body"]), {"title", "description"})

    def test_ambiguous_or_unrecognized_milestones_block_plan(self):
        snapshot = settled()
        snapshot["milestones"].append({**snapshot["milestones"][0], "number": 9, "title": "v0.1.0"})
        with self.assertRaises(manage.SetupError):
            manage.plan(CONFIG, snapshot)
        snapshot = settled()
        snapshot["milestones"][0]["title"] = "v0.1.0 different existing scope"
        with self.assertRaises(manage.SetupError):
            manage.plan(CONFIG, snapshot)

    def test_new_repository_has_no_dates_or_destructive_operations(self):
        snapshot = {"repo": "BardForge/bardforge", "labels": [], "milestones": [], "alias_assignments": {}}
        operations = manage.plan(CONFIG, snapshot)["operations"]
        self.assertEqual(len(operations), 42)
        self.assertTrue(all(operation["method"] == "POST" for operation in operations))
        self.assertTrue(all("due_on" not in operation["body"] for operation in operations))

    def test_organization_infrastructure_creates_only_labels(self):
        snapshot = {"repo": "BardForge/.github", "labels": [], "milestones": [], "alias_assignments": {}}
        operations = manage.plan(CONFIG, snapshot)["operations"]
        self.assertEqual(len(operations), 38)
        self.assertTrue(all(operation["method"] == "POST" for operation in operations))
        self.assertTrue(all(operation["endpoint"] == "repos/BardForge/.github/labels" for operation in operations))

    def test_organization_infrastructure_leaves_existing_milestones_untouched(self):
        snapshot = settled()
        snapshot["repo"] = "BardForge/.github"
        # A product repository would reject these conflicting aliases. Infrastructure
        # ignores milestone synchronization completely, including rename and state.
        snapshot["milestones"][0].update(title="v0.1.0", description="Existing scope", state="closed")
        snapshot["milestones"].append({**snapshot["milestones"][0], "number": 9, "title": "Audio MVP"})
        original = copy.deepcopy(snapshot["milestones"])
        self.assertEqual(manage.plan(CONFIG, snapshot)["operations"], [])
        self.assertEqual(snapshot["milestones"], original)

    def test_stale_or_edited_review_is_rejected(self):
        snapshot = settled()
        reviewed = manage.plan(CONFIG, snapshot)
        self.assertEqual(manage.verified_plan(CONFIG, snapshot, reviewed), reviewed)
        edited = copy.deepcopy(reviewed)
        edited["operations"] = [{"method": "DELETE", "endpoint": "anything"}]
        with self.assertRaises(manage.SetupError):
            manage.verified_plan(CONFIG, snapshot, edited)
        snapshot["labels"][0]["color"] = "123456"
        with self.assertRaises(manage.SetupError):
            manage.verified_plan(CONFIG, snapshot, reviewed)

    def test_archived_canonical_requires_manual_review(self):
        snapshot = settled()
        snapshot["labels"][0]["archived_at"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(manage.SetupError):
            manage.plan(CONFIG, snapshot)

    def test_invalid_configuration_is_rejected(self):
        bad = copy.deepcopy(CONFIG)
        bad["labels"][0]["aliases"].append("type:bug")
        with self.assertRaises(manage.SetupError):
            manage.validate_config(bad)
        bad = copy.deepcopy(CONFIG)
        bad["milestones"][0]["due_on"] = "2030-01-01"
        with self.assertRaises(manage.SetupError):
            manage.validate_config(bad)


class ClientTests(unittest.TestCase):
    def test_readonly_default_and_repository_boundary(self):
        api = manage.GitHub("BardForge/bardforge")
        with patch("manage.subprocess.run") as run:
            for method, endpoint in [("POST", api.prefix + "labels"), ("DELETE", api.prefix + "labels/x"),
                                     ("GET", "repos/Other/repo/labels")]:
                with self.assertRaises(manage.SetupError):
                    api.request(method, endpoint)
            run.assert_not_called()
        with self.assertRaises(manage.SetupError):
            manage.GitHub("Other/repo")

    def test_body_is_structured_json_on_stdin_without_shell(self):
        api = manage.GitHub("BardForge/bardforge", allow_write=True)
        payload = {"name": "label", "description": "$(must not execute) `literal` Русский"}
        result = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with patch("manage.subprocess.run", return_value=result) as run:
            api.request("POST", api.prefix + "labels", payload)
            args, kwargs = run.call_args
            self.assertIsInstance(args[0], list)
            self.assertEqual(json.loads(kwargs["input"]), payload)
            self.assertNotIn("shell", kwargs)
            self.assertEqual(args[0][-2:], ["--input", "-"])

    def test_audit_includes_closed_assignments_and_never_retains_issue_body(self):
        class Fake:
            repo = "BardForge/bardforge"
            queries = []
            def pages(self, resource, **query):
                self.queries.append((resource, query))
                if resource == "labels":
                    return [{"name": "type:docs", "color": "FFFFFF", "description": None}]
                if resource == "milestones":
                    return []
                return [{"number": 3, "labels": [{"name": "type:docs"}], "body": "private body", "title": "private title"}]
        api = Fake()
        snapshot = manage.audit(api, CONFIG)
        self.assertIn(("issues", {"state": "all", "labels": "type:docs"}), api.queries)
        self.assertNotIn("private", json.dumps(snapshot))
        self.assertEqual(snapshot["alias_assignments"]["type:docs"][0]["number"], 3)

    def test_path_escape_and_api_free_validation(self):
        with self.assertRaises(manage.SetupError):
            manage.local_path("../outside.json")
        with patch("manage.subprocess.run") as run:
            self.assertEqual(manage.main(["--validate"]), 0)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
