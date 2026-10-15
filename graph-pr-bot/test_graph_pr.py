import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("graph_pr", Path(__file__).with_name("graph_pr.py"))
bot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bot)
VERSION = "5.0.0-okd-scos.2"
PREVIOUS = "5.0.0-okd-scos.1"
REPO = "okd-project/cincinnati-graph-data"


def channel(name="stable-5.0", versions=None, **feeder):
    return yaml.safe_dump({
        "name": name,
        "feeder": {"name": name.split("-")[0], "filter": r"5[.]0[.]0-okd-scos[.].*", "delay": "PT0H", **feeder},
        "versions": [PREVIOUS] if versions is None else versions,
    }, sort_keys=False)


def risk(target=PREVIOUS, **extra):
    return yaml.safe_dump(dict(to=target, name="ExampleRisk", **{"from": ".*"}, **extra))


def args(**overrides):
    values = dict(release_name=VERSION, release_stream="5-scos-stable", release_controller=bot.CONTROLLER,
                  repository=REPO, base_branch="main", source_pipelinerun="promotion-123",
                  app_id="123", installation_id="456", private_key_file=Path("/unused.key"),
                  dry_run=False, graph_data_dir=None)
    values.update(overrides)
    return argparse.Namespace(**values)


class QuietTest(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)


class AcceptanceTests(QuietTest):
    def wait(self, responses, timeout=180):
        clock = [0]

        def sleep(seconds):
            clock[0] += seconds

        def response():
            value = responses.pop(0) if len(responses) > 1 else responses[0]
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(bot, "release_phase", side_effect=lambda *a: response()), \
                patch.object(bot.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(bot.time, "sleep", side_effect=sleep):
            bot.wait_for_acceptance(bot.CONTROLLER, "5-scos-stable", VERSION, timeout, 60)
        return clock[0]

    def test_waits_for_exact_release(self):
        data = {"name": "5-scos-stable", "tags": [
            {"name": "5.0.0-okd-scos.3", "phase": "Accepted"},
            {"name": VERSION, "phase": "Ready"},
        ]}
        with patch.object(bot, "request_json", return_value=data) as request:
            self.assertEqual(bot.release_phase(bot.CONTROLLER, "5-scos-stable", VERSION), "Ready")
        self.assertIn("/5-scos-stable/tags", request.call_args.args[0])

    def test_absent_tag_is_not_accepted(self):
        with patch.object(bot, "request_json", return_value={"name": "5-scos-stable", "tags": []}):
            self.assertEqual(bot.release_phase(bot.CONTROLLER, "5-scos-stable", VERSION), "NotFound")

    def test_waits_through_pending_and_missing(self):
        self.assertEqual(self.wait(["NotFound", "Pending", "Ready", "Accepted"]), 180)

    def test_transient_errors_keep_waiting(self):
        for error in (bot.APIError("GET", "controller", 503), bot.APIError("GET", "controller", 429),
                      bot.APIError("GET", "controller", 404), urllib.error.URLError("temporary"),
                      ValueError("bad JSON")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.wait([error, "Accepted"]), 60)

    def test_permission_error_fails(self):
        with self.assertRaises(bot.APIError):
            self.wait([bot.APIError("GET", "controller", 403)])

    def test_rejected_and_failed_stop(self):
        for phase in ("Rejected", "Failed"):
            with self.subTest(phase=phase), self.assertRaisesRegex(bot.BotError, "rerun"):
                self.wait([phase])

    def test_timeout(self):
        with self.assertRaisesRegex(bot.BotError, "Timed out"):
            self.wait(["Ready"], timeout=61)

    def test_stream_response_must_match(self):
        with patch.object(bot, "request_json", return_value={"name": "4-scos-stable", "tags": []}), \
                self.assertRaisesRegex(bot.BotError, "Malformed"):
            bot.release_phase(bot.CONTROLLER, "5-scos-stable", VERSION)

    def test_malformed_tag_schema_fails_closed(self):
        for data in (None, [], {"name": "5-scos-stable", "tags": [None]}):
            with self.subTest(data=data), patch.object(bot, "request_json", return_value=data), \
                    self.assertRaisesRegex(bot.BotError, "Malformed"):
                bot.release_phase(bot.CONTROLLER, "5-scos-stable", VERSION)

    def test_stable_and_next_stream_validation(self):
        bot.validate_target(VERSION, "5-scos-stable")
        bot.validate_target("4.22.0-okd-scos.ec.17", "4-scos-next")
        for version, stream in ((VERSION, "4-scos-stable"), (VERSION, "5-scos-next"),
                                ("5.0.0-0.okd-scos-nightly-2026-10-14", "5-scos-stable")):
            with self.subTest(version=version, stream=stream), self.assertRaises(bot.BotError):
                bot.validate_target(version, stream)


class ChannelTests(QuietTest):
    def test_stable_only_updates_stable_channels(self):
        files = {"channels/stable-5.0.yaml": channel(), "channels/candidate-5.0.yaml": channel("candidate-5.0")}
        changes = bot.plan_changes(files, VERSION, bot.CONTROLLER)
        self.assertEqual(set(changes), {"channels/stable-5.0.yaml"})
        self.assertEqual(yaml.safe_load(changes["channels/stable-5.0.yaml"])["versions"], [PREVIOUS, VERSION])

    def test_next_only_updates_candidate_channels(self):
        release = "5.0.0-okd-scos.ec.11"
        files = {"channels/stable-5.0.yaml": channel(), "channels/candidate-5.0.yaml": channel("candidate-5.0")}
        changes = bot.plan_changes(files, release, bot.CONTROLLER)
        self.assertEqual(set(changes), {"channels/candidate-5.0.yaml"})

    def test_filter_must_match_entire_version(self):
        with self.assertRaisesRegex(bot.BotError, "No existing"):
            bot.plan_changes({"channels/stable-5.0.yaml": channel(filter="5.0.0")}, VERSION, bot.CONTROLLER)

    def test_missing_channel_fails(self):
        with self.assertRaisesRegex(bot.BotError, "configure the channel"):
            bot.plan_changes({}, VERSION, bot.CONTROLLER)

    def test_present_and_tombstoned_versions_are_noops(self):
        for text in (channel(versions=[VERSION]), channel() + "tombstones:\n- " + VERSION + "\n"):
            self.assertEqual(bot.plan_changes({"channels/stable-5.0.yaml": text}, VERSION, bot.CONTROLLER), {})

    def test_nonzero_delay_fails(self):
        with self.assertRaisesRegex(bot.BotError, "acceptance timestamps"):
            bot.plan_changes({"channels/stable-5.0.yaml": channel(delay="PT24H")}, VERSION, bot.CONTROLLER)

    def test_block_list_formatting_and_comments_preserved(self):
        for indent in (0, 2, 4):
            text = "# Header\nname: stable-5.0\nversions:\n" + " " * indent + "- " + PREVIOUS + " # keep\n# Footer\n"
            result = bot.append_version(text, VERSION)
            self.assertEqual(result, text.replace("# Footer", " " * indent + "- " + VERSION + "\n# Footer"))
            self.assertEqual(yaml.safe_load(result)["versions"], [PREVIOUS, VERSION])

    def test_empty_flow_and_unterminated_lists(self):
        for text in ("versions: []\n", "versions: [" + PREVIOUS + "]\n", "versions:\n- " + PREVIOUS):
            self.assertIn(VERSION, yaml.safe_load(bot.append_version(text, VERSION))["versions"])

    def test_aliased_version_list_does_not_change_other_fields(self):
        text = "shared: &versions\n- " + PREVIOUS + "\nversions: *versions\n"
        updated = yaml.safe_load(bot.append_version(text, VERSION))
        self.assertEqual(updated["shared"], [PREVIOUS])
        self.assertEqual(updated["versions"], [PREVIOUS, VERSION])

    def test_unresolved_risk_blocks(self):
        files = {"channels/stable-5.0.yaml": channel(), "blocked-edges/previous.yaml": risk()}
        with self.assertRaisesRegex(bot.BotError, "fixedIn"):
            bot.plan_changes(files, VERSION, bot.CONTROLLER)

    def test_fixed_or_extended_risk_allows_promotion(self):
        for risks in ({"old.yaml": risk(fixedIn=VERSION)},
                      {"old.yaml": risk(), "new.yaml": risk(target=VERSION)},
                      {"old.yaml": risk(target=PREVIOUS + "+arm64")}):
            files = {"channels/stable-5.0.yaml": channel()}
            files.update({"blocked-edges/" + path: text for path, text in risks.items()})
            changes = bot.plan_changes(files, VERSION, bot.CONTROLLER)
            self.assertEqual(set(changes), {"channels/stable-5.0.yaml"})

    def test_latest_risk_declaration_supplies_fix(self):
        files = {"channels/stable-5.0.yaml": channel(),
                 "blocked-edges/old.yaml": risk(target="5.0.0-okd-scos.0"),
                 "blocked-edges/recent.yaml": risk(fixedIn=VERSION)}
        self.assertTrue(bot.plan_changes(files, VERSION, bot.CONTROLLER))

    def test_cross_minor_requires_tested_path(self):
        release = "4.22.0-okd-scos.11"
        text = channel(filter=r"(4[.]22|5[.]0)[.]0-okd-scos[.].*")
        graph = {"nodes": [{"version": release}, {"version": PREVIOUS}], "edges": [[0, 1]]}
        files = {"channels/stable-5.0.yaml": text}
        with patch.object(bot, "request_json", return_value=graph):
            self.assertTrue(bot.plan_changes(files, release, bot.CONTROLLER))
        with patch.object(bot, "request_json", return_value={**graph, "edges": []}), \
                self.assertRaisesRegex(bot.BotError, "no tested path"):
            bot.plan_changes(files, release, bot.CONTROLLER)

    def test_cross_minor_honors_unconditional_and_conditional_blocks(self):
        release = "4.22.0-okd-scos.11"
        graph = {"nodes": [{"version": release}, {"version": PREVIOUS}], "edges": [[0, 1]]}
        files = {"blocked-edges/blocked.yaml": risk()}
        self.assertFalse(bot.reaches_channel_minor(graph, files, release, [PREVIOUS], (5, 0)))
        files["blocked-edges/blocked.yaml"] = risk(matchingRules=[{"type": "Always"}])
        self.assertTrue(bot.reaches_channel_minor(graph, files, release, [PREVIOUS], (5, 0)))

    def test_cross_minor_rejects_downgrade_detour(self):
        release, earlier = "4.22.0-okd-scos.11", "4.22.0-okd-scos.10"
        graph = {"nodes": [{"version": version} for version in (release, earlier, PREVIOUS)],
                 "edges": [[0, 1], [1, 2]]}
        self.assertFalse(bot.reaches_channel_minor(graph, {}, release, [earlier, PREVIOUS], (5, 0)))

    def test_cross_minor_accepts_controller_conditional_path(self):
        release = "4.22.0-okd-scos.11"
        graph = {"nodes": [], "conditionalEdges": [{"edges": [{"from": release, "to": PREVIOUS}]}]}
        self.assertTrue(bot.reaches_channel_minor(graph, {}, release, [PREVIOUS], (5, 0)))


class FakeGitHub:
    """An in-memory HTTP API exercising the real branch/tree/commit/PR code."""

    def __init__(self):
        self.blobs, self.calls = {}, []
        files = {"channels/stable-5.0.yaml": channel(), "README.md": "Keep this file\n"}
        entries = []
        for path, text in files.items():
            sha = bot.blob_sha(text)
            self.blobs[sha] = text
            entries.append(dict(path=path, type="blob", mode="100644", sha=sha))
        self.trees = {"base-tree": entries}
        self.commits = {"base-commit": {"tree": {"sha": "base-tree"}}}
        self.refs = {"main": "base-commit"}
        self.pulls = []
        self.phases = ["Accepted"]
        self.fail_pr_once = False
        self.race_pr = False
        self.race_ref = False

    def request(self, url, method="GET", data=None, token=None):
        self.calls.append((url, method, data))
        if url.startswith(bot.CONTROLLER):
            phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
            return {"name": "5-scos-stable", "tags": [{"name": VERSION, "phase": phase}]}
        path = url.split("/repos/" + REPO, 1)[1]
        if path.startswith("/pulls?"):
            return list(self.pulls)
        if path.startswith("/git/ref/heads/"):
            branch = urllib.parse.unquote(path[len("/git/ref/heads/"):])
            if branch not in self.refs:
                raise bot.APIError(method, url, 404)
            return {"object": {"sha": self.refs[branch]}}
        if path.startswith("/git/commits/"):
            return self.commits[path.split("/")[-1]]
        if path.startswith("/git/trees/"):
            return {"tree": self.trees[path.split("/")[-1].split("?")[0]], "truncated": False}
        if path.startswith("/git/blobs/"):
            text = self.blobs[path.split("/")[-1]]
            return {"content": bot.base64.b64encode(text.encode()).decode(), "encoding": "base64"}
        if path == "/git/trees" and method == "POST":
            tree = {entry["path"]: dict(entry) for entry in self.trees[data["base_tree"]]}
            for entry in data["tree"]:
                sha = bot.blob_sha(entry["content"])
                self.blobs[sha] = entry["content"]
                tree[entry["path"]] = {k: v for k, v in entry.items() if k != "content"}
                tree[entry["path"]]["sha"] = sha
            tree_id = "tree-" + str(len(self.trees))
            self.trees[tree_id] = list(tree.values())
            return {"sha": tree_id}
        if path == "/git/commits" and method == "POST":
            commit = "commit-" + str(len(self.commits))
            self.commits[commit] = dict(data, tree={"sha": data["tree"]})
            return {"sha": commit}
        if path == "/git/refs" and method == "POST":
            branch = data["ref"][len("refs/heads/"):]
            if branch in self.refs:
                raise bot.APIError(method, url, 422)
            self.refs[branch] = data["sha"]
            if self.race_ref:
                raise bot.APIError(method, url, 422)
            return {}
        if path == "/pulls" and method == "POST":
            if self.fail_pr_once:
                self.fail_pr_once = False
                raise bot.APIError(method, url, 500)
            pull = {"html_url": "https://github.com/" + REPO + "/pull/999999",
                    "state": "open", "merged_at": None, "head": {"repo": {"full_name": REPO}}}
            self.pulls.append(pull)
            if self.race_pr:
                raise bot.APIError(method, url, 422)
            return pull
        raise AssertionError("Unexpected API call: {} {}".format(method, url))


class ProposalTests(QuietTest):
    def setUp(self):
        super().setUp()
        self.api = FakeGitHub()
        self.request = patch.object(bot, "request_json", side_effect=self.api.request)
        self.request.start()
        self.addCleanup(self.request.stop)
        self.auth = patch.object(bot, "installation_token", return_value="test-token").start()
        self.addCleanup(patch.stopall)

    def writes(self):
        return [call for call in self.api.calls if call[1] != "GET"]

    def test_full_proposal_preserves_other_files_and_links_acceptance(self):
        url = bot.propose(args())
        self.assertTrue(url.endswith("/999999"))
        branch_commit = self.api.refs[next(key for key in self.api.refs if key != "main")]
        commit = self.api.commits[branch_commit]
        self.assertEqual(commit["parents"], ["base-commit"])
        entries = {entry["path"]: entry for entry in self.api.trees[commit["tree"]["sha"]]}
        self.assertIn("README.md", entries)
        self.assertEqual(self.api.blobs[entries["README.md"]["sha"]], "Keep this file\n")
        self.assertEqual(yaml.safe_load(self.api.blobs[entries["channels/stable-5.0.yaml"]["sha"]])["versions"],
                         [PREVIOUS, VERSION])
        body = next(data["body"] for url, method, data in self.writes() if url.endswith("/pulls"))
        self.assertIn("promotion-123", body)
        self.assertIn("/5-scos-stable/release/" + VERSION, body)

    def test_unaccepted_release_never_gets_credentials_or_writes(self):
        self.api.phases = ["Ready"]
        with self.assertRaisesRegex(bot.BotError, "requires Accepted"):
            bot.propose(args())
        self.auth.assert_not_called()
        self.assertEqual(self.writes(), [])

    def test_acceptance_rechecked_before_branch_and_pr(self):
        for phases, expected_writes in ((["Accepted", "Rejected"], 0), (["Accepted", "Accepted", "Rejected"], 3)):
            with self.subTest(phases=phases):
                self.api = FakeGitHub()
                self.api.phases = phases
                with patch.object(bot, "request_json", side_effect=self.api.request), \
                        self.assertRaisesRegex(bot.BotError, "requires Accepted"):
                    bot.propose(args())
                self.assertEqual(len(self.writes()), expected_writes)
                self.assertFalse(self.api.pulls)

    def test_repeated_run_reuses_open_pr(self):
        first = bot.propose(args())
        count = len(self.writes())
        self.assertEqual(bot.propose(args()), first)
        self.assertEqual(len(self.writes()), count)

    def test_recovers_if_branch_created_but_pr_request_failed(self):
        self.api.fail_pr_once = True
        with self.assertRaises(bot.APIError):
            bot.propose(args())
        tree_count, commit_count = len(self.api.trees), len(self.api.commits)
        self.assertTrue(bot.propose(args()))
        self.assertEqual(len(self.api.trees), tree_count)
        self.assertEqual(len(self.api.commits), commit_count)
        self.assertEqual(len(self.api.pulls), 1)

    def test_existing_branch_with_manual_changes_is_not_overwritten(self):
        self.api.fail_pr_once = True
        with self.assertRaises(bot.APIError):
            bot.propose(args())
        self.api.trees["tree-1"][0]["sha"] = "human-change"
        before = len(self.writes())
        with self.assertRaisesRegex(bot.BotError, "review it manually"):
            bot.propose(args())
        self.assertEqual(len(self.writes()), before)

    def test_pr_creation_race_reuses_existing_pr(self):
        self.api.race_pr = True
        self.assertTrue(bot.propose(args()))
        self.assertEqual(len(self.api.pulls), 1)

    def test_branch_creation_race_reuses_identical_branch(self):
        self.api.race_ref = True
        self.assertTrue(bot.propose(args()))
        self.assertEqual(len(self.api.pulls), 1)

    def test_closed_unmerged_pr_is_not_duplicated(self):
        bot.propose(args())
        self.api.pulls[0]["state"] = "closed"
        count = len(self.writes())
        with self.assertRaisesRegex(bot.BotError, "reopen it manually"):
            bot.propose(args())
        self.assertEqual(len(self.writes()), count)

    def test_merged_addition_is_noop(self):
        bot.propose(args())
        self.api.pulls[0].update(state="closed", merged_at="2026-10-14T00:00:00Z")
        self.api.refs["main"] = next(value for key, value in self.api.refs.items() if key != "main")
        count = len(self.writes())
        self.assertEqual(bot.propose(args()), "")
        self.assertEqual(len(self.writes()), count)

    def test_dry_run_has_no_credentials_or_writes(self):
        bot.propose(args(dry_run=True))
        self.auth.assert_not_called()
        self.assertEqual(self.writes(), [])
        self.assertIn("+" + VERSION, self.output.getvalue().replace("- ", ""))


class DeploymentTests(unittest.TestCase):
    def test_launcher_creates_independent_run_and_reuses_it(self):
        task = yaml.safe_load((ROOT / "base/tekton.dev/tasks/launch-graph-pr.yaml").read_text())
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            fake_oc = work / "oc"
            fake_oc.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
                               "record=Path(os.environ['FAKE_OC_MANIFEST'])\n"
                               "if sys.argv[1]=='get':\n"
                               "    if record.exists(): print(json.loads(record.read_text())['metadata']['name'])\n"
                               "elif sys.argv[1]=='create':\n"
                               "    record.write_text(Path(sys.argv[sys.argv.index('-f')+1]).read_text())\n"
                               "else: sys.exit(3)\n")
            fake_oc.chmod(0o755)
            env = dict(os.environ, PATH=str(work) + os.pathsep + os.environ["PATH"], NAMESPACE="okd-coreos",
                       FAKE_OC_MANIFEST=str(work / "manifest.json"))
            for param in task["spec"]["params"]:
                env[param["name"].upper().replace("-", "_")] = param.get("default", "")
            env.update(RELEASE_NAME=VERSION, TARGET_RELEASE_STREAM="5-scos-stable", SOURCE_PIPELINERUN="promotion-123")
            result = work / "result"
            script = task["spec"]["steps"][0]["script"].replace("$(results.graph-pipelinerun-name.path)", str(result))
            subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
            manifest = json.loads((work / "manifest.json").read_text())
            self.assertEqual(manifest["spec"]["timeouts"]["pipeline"], "176400s")
            self.assertEqual(manifest["spec"]["timeouts"]["tasks"], "176100s")
            self.assertEqual(manifest["spec"]["timeouts"]["finally"], "300s")
            self.assertEqual(manifest["spec"]["taskRunSpecs"], [
                {"pipelineTaskName": "propose-graph-update", "timeout": "176100s"}])
            self.assertEqual(manifest["spec"]["serviceAccountName"], "okd-graph-pr-bot")
            self.assertNotIn("workspaces", manifest["spec"])
            self.assertNotIn("ownerReferences", manifest["metadata"])
            self.assertEqual(manifest["metadata"]["annotations"]["okd.io/source-pipelinerun"], "promotion-123")
            self.assertEqual(result.read_text(), manifest["metadata"]["name"])
            subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
            self.assertEqual(json.loads((work / "manifest.json").read_text()), manifest)
            self.assertEqual(result.read_text(), manifest["metadata"]["name"])

    def test_kustomize_and_new_pipeline_references(self):
        rendered = subprocess.run(["oc", "kustomize", "environments/moc"], cwd=ROOT, check=True,
                                  capture_output=True, text=True).stdout
        resources = {(obj["kind"], obj["metadata"]["name"]): obj for obj in yaml.safe_load_all(rendered)}
        self.assertIn(("Role", "okd-graph-pr-launcher"), resources)
        for pipeline_name in ("okd-release-pipeline", "okd-graph-pr-pipeline"):
            pipeline = resources["Pipeline", pipeline_name]["spec"]
            for task in pipeline["tasks"]:
                if task["name"] not in ("launch-graph-pr", "propose-graph-update"):
                    continue
                task_spec = resources["Task", task["taskRef"]["name"]]["spec"]
                declared = {param["name"] for param in task_spec["params"]}
                self.assertTrue({param["name"] for param in task["params"]} <= declared)
        for name in ("stable", "next"):
            run = yaml.safe_load((ROOT / ("environments/moc/pipelineruns/okd-release-" + name + "-pipelinerun.yaml")).read_text())
            self.assertEqual(run["spec"]["serviceAccountName"], "okd-release-promoter")
            self.assertIn({"name": "enable-graph-pr", "value": "true"}, run["spec"]["params"])


class AuthenticationTests(unittest.TestCase):
    def test_app_jwt_signature_and_repository_scoping(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            key, public, signature = (work / name for name in ("key.pem", "public.pem", "signature.bin"))
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                            "-out", str(key)], check=True, capture_output=True)
            with patch.object(bot.time, "time", return_value=1000), \
                    patch.object(bot, "request_json", return_value={"token": "fresh-token"}) as request:
                token = bot.installation_token("123", "456", key, REPO)
            self.assertEqual(token, "fresh-token")
            url, method, body, jwt = request.call_args.args
            self.assertTrue(url.endswith("/app/installations/456/access_tokens"))
            self.assertEqual(method, "POST")
            self.assertEqual(body, {"repositories": ["cincinnati-graph-data"],
                                    "permissions": {"contents": "write", "pull_requests": "write"}})
            header, payload, encoded_signature = jwt.split(".")
            claims = json.loads(bot.base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            self.assertEqual(claims, {"iat": 940, "exp": 1300, "iss": "123"})
            signature.write_bytes(bot.base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4)))
            subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)],
                           check=True, capture_output=True)
            subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(signature)],
                           input=(header + "." + payload).encode(), check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
