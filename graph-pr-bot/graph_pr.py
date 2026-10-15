#!/usr/bin/env python3
"""Wait for a promoted OKD release, then propose its Cincinnati channel additions."""

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml


CONTROLLER = "https://amd64.origin.releases.ci.openshift.org"
GITHUB_API = "https://api.github.com"
RELEASE = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-okd-scos\.(ec\.)?(0|[1-9]\d*)"
)


class BotError(Exception):
    """An actionable failure, safe to print in pod logs."""


class APIError(BotError):
    def __init__(self, method, url, status):
        self.status = status
        super().__init__("{} {} returned HTTP {}".format(method, url, status))


def request_json(url, method="GET", data=None, token=None):
    headers = {"Accept": "application/json", "User-Agent": "okd-graph-pr-bot"}
    if token:
        headers.update({
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    request = urllib.request.Request(url, body, headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Response bodies and request headers may contain credentials. Never log them.
        raise APIError(method, url, error.code) from None


def release_key(version):
    match = RELEASE.fullmatch(version)
    if not match:
        raise BotError("Expected a promoted OKD/SCOS release, got {!r}".format(version))
    major, minor, patch, ec, build = match.groups()
    return int(major), int(minor), int(patch), 0 if ec else 1, int(build)


def validate_target(release, stream):
    key = release_key(release)
    suffix = "next" if key[3] == 0 else "stable"
    expected = "{}-scos-{}".format(key[0], suffix)
    if stream != expected:
        raise BotError("{} belongs in {}, not {}".format(release, expected, stream))


def release_phase(controller, stream, release):
    url = "{}/api/v1/releasestream/{}/tags".format(
        controller.rstrip("/"), urllib.parse.quote(stream, safe="")
    )
    data = request_json(url)
    if (not isinstance(data, dict) or data.get("name") != stream
            or not isinstance(data.get("tags"), list)
            or not all(isinstance(tag, dict) for tag in data["tags"])):
        raise BotError("Malformed tags response for {}".format(stream))
    matches = [tag for tag in data["tags"] if tag.get("name") == release]
    if len(matches) > 1:
        raise BotError("Controller returned duplicate tags for {}".format(release))
    return matches[0].get("phase", "Unknown") if matches else "NotFound"


def require_accepted(controller, stream, release):
    phase = release_phase(controller, stream, release)
    if phase != "Accepted":
        raise BotError("{} in {} is {}; PR creation requires Accepted".format(release, stream, phase))


def wait_for_acceptance(controller, stream, release, timeout, interval):
    validate_target(release, stream)
    if timeout <= 0 or interval <= 0:
        raise BotError("Acceptance timeout and poll interval must be positive")
    deadline = time.monotonic() + timeout
    while True:
        try:
            phase = release_phase(controller, stream, release)
        except APIError as error:
            if error.status not in (404, 408, 429) and error.status < 500:
                raise
            phase = "Controller unavailable (HTTP {})".format(error.status)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            phase = "Controller unavailable ({})".format(type(error).__name__)
        print("{} / {}: {}".format(stream, release, phase), flush=True)
        if phase == "Accepted":
            return
        if phase in ("Rejected", "Failed"):
            raise BotError("{} is {}; rerun the graph pipeline after acceptance".format(release, phase))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BotError("Timed out waiting for {} to be Accepted".format(release))
        time.sleep(min(interval, remaining))


def yaml_mapping(text, path):
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise BotError("{} must contain a YAML mapping".format(path))
    return data


def append_version(text, release):
    """Keep comments, indentation, and existing version order for block sequences."""
    expected = yaml.safe_load(text)
    expected["versions"] = expected["versions"] + [release]
    document = yaml.compose(text)
    sequence = next(value for key, value in document.value if key.value == "versions")
    if sequence.value and not sequence.flow_style:
        last = sequence.value[-1]
        lines = text.splitlines(keepends=True)
        position = last.end_mark.line + 1
        if not lines[position - 1].endswith("\n"):
            lines[position - 1] += "\n"
        lines.insert(position, " " * sequence.start_mark.column + "- " + release + "\n")
        result = "".join(lines)
        # Alias nodes refer to the anchor's marks, which can be outside versions.
        if yaml.safe_load(result) == expected:
            return result
    # Also support empty lists, aliases, and flow sequences through the YAML emitter.
    return yaml.safe_dump(expected, sort_keys=False)


def risk_concerns(files, release):
    current = set()
    previous = {}
    key = release_key(release)
    for path, text in files.items():
        if not path.startswith("blocked-edges/"):
            continue
        risk = yaml_mapping(text, path)
        target, _, arch = risk["to"].partition("+")
        if arch and arch != "amd64":
            continue
        if not RELEASE.fullmatch(target):
            continue
        name = risk.get("name") or path
        if target == release:
            current.add(name)
        target_key = release_key(target)
        if target_key[:2] != key[:2] or target_key >= key:
            continue
        if name not in previous or release_key(previous[name]["to"].partition("+")[0]) < target_key:
            previous[name] = risk
    concerns = []
    for name, risk in sorted(previous.items()):
        fixed = risk.get("fixedIn")
        if name in current or (fixed and key >= release_key(fixed.partition("+")[0])):
            continue
        concerns.append("{} affects {}; extend its blocked-edge declaration to {} or record "
                        "the verified fixedIn version".format(name, risk["to"], release))
    return concerns


def reaches_channel_minor(graph, files, release, versions, minor):
    nodes = graph["nodes"]
    edges = set()
    for source, target in graph.get("edges", []):
        if (type(source) is not int or type(target) is not int
                or not 0 <= source < len(nodes) or not 0 <= target < len(nodes)):
            raise BotError("Controller returned an invalid graph edge")
        edges.add((nodes[source]["version"], nodes[target]["version"]))
    for conditional in graph.get("conditionalEdges", []):
        edges.update((edge["from"], edge["to"]) for edge in conditional["edges"])
    for path, text in files.items():
        if not path.startswith("blocked-edges/"):
            continue
        risk = yaml_mapping(text, path)
        target, _, arch = risk["to"].partition("+")
        if (arch and arch != "amd64") or risk.get("matchingRules"):
            continue
        edges = {(source, dest) for source, dest in edges
                 if dest != target or not re.search(risk["from"], source + "+amd64")}
    pending, visited = [release], set()
    allowed = set(versions) | {release}
    while pending:
        source = pending.pop()
        if source in visited:
            continue
        visited.add(source)
        for start, target in edges:
            if start != source or target not in allowed:
                continue
            if not RELEASE.fullmatch(target) or release_key(target) <= release_key(source):
                continue  # downgrade/reinstall tests cannot establish a forward update path
            if release_key(target)[:2] == minor:
                return True
            pending.append(target)
    return False


def plan_changes(files, release, controller):
    kind = "candidate" if release_key(release)[3] == 0 else "stable"
    changes, matched, graph = {}, False, None
    for path, text in sorted(files.items()):
        if not path.startswith("channels/"):
            continue
        channel = yaml_mapping(text, path)
        name = channel.get("name", "")
        if not re.fullmatch(kind + r"-\d+\.\d+", name):
            continue
        if path != "channels/{}.yaml".format(name):
            raise BotError("Channel name does not match filename: {}".format(path))
        feeder = channel.get("feeder", {})
        if feeder.get("name") != kind or not re.fullmatch(feeder.get("filter", ".*"), release):
            continue
        matched = True
        versions = channel.get("versions")
        if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions):
            raise BotError("{}: versions must be a list of strings".format(path))
        if release in versions or release in channel.get("tombstones", []):
            continue
        delay = str(feeder.get("delay", "PT0H"))
        if not re.fullmatch(r"P(?:0W|0D|(?:0D)?T(?:0H)?(?:0M)?(?:0S)?)", delay):
            raise BotError("{}: nonzero feeder delays require acceptance timestamps".format(path))
        if feeder.get("errata"):
            raise BotError("{}: OKD has no errata-based promotion".format(path))
        minor = tuple(int(part) for part in name.split("-", 1)[1].split("."))
        if release_key(release)[:2] != minor:
            if graph is None:
                graph = request_json(controller.rstrip("/") + "/graph?format=cincinnati")
            if not reaches_channel_minor(graph, files, release, versions, minor):
                raise BotError("{}: no tested path from {} into {}.{} through this channel".format(
                    path, release, *minor))
        changes[path] = append_version(text, release)
    if not matched:
        raise BotError("No existing {} channel feeder matches {}; configure the channel first".format(
            kind, release))
    if changes:
        concerns = risk_concerns(files, release)
        if concerns:
            raise BotError("Unresolved upgrade risks: " + "; ".join(concerns))
    return changes


def installation_token(app_id, installation_id, key_file, repository):
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    now = int(time.time())
    header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"iat": now - 60, "exp": now + 300, "iss": app_id}).encode())
    signing_input = header + b"." + payload
    signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_file)],
                            input=signing_input, capture_output=True, check=True)
    jwt = (signing_input + b"." + b64(signed.stdout)).decode()
    response = request_json(GITHUB_API + "/app/installations/{}/access_tokens".format(installation_id),
                            "POST", {"repositories": [repository.split("/")[1]],
                                     "permissions": {"contents": "write", "pull_requests": "write"}}, jwt)
    if not response.get("token"):
        raise BotError("GitHub App returned no installation token; check installation and permissions")
    return response["token"]


def blob_sha(content):
    data = content.encode()
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class GitHub:
    def __init__(self, repository, token=None):
        self.repository, self.token = repository, token
        self.url = GITHUB_API + "/repos/" + repository

    def call(self, path, method="GET", data=None):
        return request_json(self.url + path, method, data, self.token)

    def ref(self, branch):
        try:
            return self.call("/git/ref/heads/" + urllib.parse.quote(branch, safe="/"))["object"]["sha"]
        except APIError as error:
            if error.status != 404:
                raise
            return None

    def tree(self, commit):
        tree_sha = self.call("/git/commits/" + commit)["tree"]["sha"]
        response = self.call("/git/trees/{}?recursive=1".format(tree_sha))
        if response.get("truncated"):
            raise BotError("GitHub truncated the repository tree; refusing a partial update")
        entries = {entry["path"]: entry for entry in response["tree"] if entry["type"] != "tree"}
        return tree_sha, entries

    def files(self, entries):
        files = {}
        for path, entry in entries.items():
            if not re.fullmatch(r"(?:channels|blocked-edges)/[^/]+\.ya?ml", path):
                continue
            if entry["type"] != "blob" or entry["mode"] not in ("100644", "100755"):
                raise BotError("Expected an ordinary YAML file at " + path)
            blob = self.call("/git/blobs/" + entry["sha"])
            if blob.get("encoding") != "base64":
                raise BotError("Unexpected GitHub blob encoding for " + path)
            files[path] = base64.b64decode(blob["content"]).decode()
        return files

    def pulls(self, branch, base):
        query = urllib.parse.urlencode({"state": "all", "head": self.repository.split("/")[0] + ":" + branch,
                                        "base": base, "per_page": 100})
        return [pull for pull in self.call("/pulls?" + query)
                if pull["head"].get("repo", {}).get("full_name", "").lower() == self.repository.lower()]

    def ensure_branch(self, branch, base_sha, tree_sha, entries, changes, message):
        def verify_existing(sha):
            _, actual = self.tree(sha)
            expected = {path: (entry["mode"], entry["type"], entry["sha"])
                        for path, entry in entries.items()}
            for path, content in changes.items():
                expected[path] = entries[path]["mode"], "blob", blob_sha(content)
            observed = {path: (entry["mode"], entry["type"], entry["sha"])
                        for path, entry in actual.items()}
            if observed != expected:
                raise BotError("Existing branch {} differs from the proposed update; review it manually".format(branch))

        existing = self.ref(branch)
        if existing:
            verify_existing(existing)
            return
        tree = self.call("/git/trees", "POST", {"base_tree": tree_sha, "tree": [
            {"path": path, "mode": entries[path]["mode"], "type": "blob", "content": content}
            for path, content in sorted(changes.items())
        ]})
        commit = self.call("/git/commits", "POST", {"message": message, "tree": tree["sha"], "parents": [base_sha]})
        try:
            self.call("/git/refs", "POST", {"ref": "refs/heads/" + branch, "sha": commit["sha"]})
        except APIError as error:
            if error.status != 422:
                raise
            # Another run may have created the same branch concurrently.
            existing = self.ref(branch)
            if not existing:
                raise
            verify_existing(existing)


def propose(args):
    validate_target(args.release_name, args.release_stream)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repository):
        raise BotError("Expected a GitHub OWNER/REPO repository")
    require_accepted(args.release_controller, args.release_stream, args.release_name)
    if args.graph_data_dir and not args.dry_run:
        raise BotError("--graph-data-dir is available only with --dry-run")
    branch = "okd-graph/{}-{}".format(
        args.release_name, hashlib.sha256(args.base_branch.encode()).hexdigest()[:8])
    token = None
    if not args.dry_run:
        token = installation_token(args.app_id, args.installation_id, args.private_key_file, args.repository)
    github = GitHub(args.repository, token)
    if not args.dry_run:
        pulls = github.pulls(branch, args.base_branch)
        for pull in pulls:
            if pull["state"] == "open":
                print("Existing graph PR: " + pull["html_url"])
                return pull["html_url"]
        if any(not pull.get("merged_at") for pull in pulls):
            raise BotError("A graph PR for this release was closed without merging; reopen it manually")
    if args.graph_data_dir:
        files = {str(path.relative_to(args.graph_data_dir)): path.read_text()
                 for directory in ("channels", "blocked-edges")
                 for path in (args.graph_data_dir / directory).glob("*.yaml")}
    else:
        base_sha = github.ref(args.base_branch)
        if not base_sha:
            raise BotError("Base branch does not exist: " + args.base_branch)
        tree_sha, entries = github.tree(base_sha)
        files = github.files(entries)
    changes = plan_changes(files, args.release_name, args.release_controller)
    if not changes:
        print("No channel additions: release is already present or tombstoned")
        return ""
    print("Proposed channel additions for {}: {}".format(args.release_name, ", ".join(sorted(changes))))
    if args.dry_run:
        import difflib
        for path, content in sorted(changes.items()):
            print("".join(difflib.unified_diff(files[path].splitlines(True), content.splitlines(True),
                                             fromfile="a/" + path, tofile="b/" + path)), end="")
        return ""
    title = "channels: Add {}".format(args.release_name)
    acceptance_url = "{}/releasestream/{}/release/{}".format(
        args.release_controller.rstrip("/"), args.release_stream, args.release_name)
    body = "\n".join([
        "Add `{}` after the OKD release controller reported **Accepted**.".format(args.release_name),
        "", "Release controller: {}".format(acceptance_url),
        "Promotion PipelineRun: `{}`".format(args.source_pipelinerun), "",
        "Channels:", *["- `{}`".format(path) for path in sorted(changes)], "",
        "Checked feeder filters, tombstones, unresolved risk declarations, and tested paths "
        "when adding a release to a newer minor's channel. Existing blocked edges are retained.",
        "", "Generated by the OKD release pipeline. Review and merge through the normal graph-data checks.",
    ])
    require_accepted(args.release_controller, args.release_stream, args.release_name)
    github.ensure_branch(branch, base_sha, tree_sha, entries, changes, title)
    require_accepted(args.release_controller, args.release_stream, args.release_name)
    try:
        pull = github.call("/pulls", "POST", {"title": title, "body": body, "head": branch,
                                               "base": args.base_branch, "maintainer_can_modify": True})
    except APIError as error:
        if error.status != 422:
            raise
        existing = [pull for pull in github.pulls(branch, args.base_branch) if pull["state"] == "open"]
        if not existing:
            raise
        pull = existing[0]
    print("Graph PR: " + pull["html_url"])
    return pull["html_url"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("wait", "propose"):
        command = commands.add_parser(name)
        command.add_argument("--release-name", required=True)
        command.add_argument("--release-stream", required=True)
        command.add_argument("--release-controller", default=CONTROLLER)
        if name == "wait":
            command.add_argument("--timeout-seconds", type=int, default=172800)
            command.add_argument("--poll-seconds", type=int, default=60)
        else:
            command.add_argument("--repository", default="okd-project/cincinnati-graph-data")
            command.add_argument("--base-branch", default="main")
            command.add_argument("--source-pipelinerun", default="manual-graph-rerun")
            command.add_argument("--app-id", default="309584")
            command.add_argument("--installation-id", default="35633111")
            command.add_argument("--private-key-file", type=Path,
                                 default=Path("/secret/githubapp/githubapp-private-key"))
            command.add_argument("--result-file", type=Path)
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--graph-data-dir", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "wait":
            wait_for_acceptance(args.release_controller, args.release_stream, args.release_name,
                                args.timeout_seconds, args.poll_seconds)
        else:
            url = propose(args)
            if args.result_file:
                args.result_file.write_text(url)
    except (BotError, OSError, ValueError, KeyError, TypeError, yaml.YAMLError,
            urllib.error.URLError, subprocess.CalledProcessError) as error:
        # CalledProcessError prints only the command, which contains a key path, never the key or JWT.
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
