# OKD graph-data PR bot

After `update-release-channel`, the release Pipeline launches an independent
`okd-graph-pr-pipeline` for the exact promoted release and target stream. Both
stable (`N-scos-stable`) and next (`N-scos-next`) promotions are supported.

The first step polls the controller's `/api/v1/releasestream/<stream>/tags` API
every minute for up to 48 hours. It matches the exact tag and requires its phase
to equal `Accepted`. Acceptance of the source nightly or another tag does not
satisfy this gate. Missing tags, HTTP 404/429/5xx, network failures, and malformed
JSON are retried. `Rejected`, `Failed`, configuration errors, and timeout fail
the follow-up without opening a PR. A manually accepted release is eligible
because the controller reports it as `Accepted`.

After acceptance, the second step rechecks the phase, generates a fresh GitHub
App installation token, reads graph-data from the configured base commit, and
adds only this release to matching existing channels. It checks acceptance again
before creating the branch and immediately before opening the PR. Tokens and
private keys are never written to Tekton results or logs. Only the PR URL is
returned as a Pipeline result.

## Deploy

1. Install the existing GitHub App on `okd-project/cincinnati-graph-data`, granting
   repository **Contents: read/write** and **Pull requests: read/write**. Accept
   any pending permission changes on the installation. Its existing access to
   `okd-project/okd` must also remain available for release publication.
2. Ensure `okd-githubapp-auth` in `okd-coreos` contains the private key under
   **`githubapp-private-key`**, the key used by the existing token Task. The
   defaults reuse App ID `309584` and installation ID `35633111`; all are
   configurable for a separate App or installation.
3. Build and publish the new image. The default image reference is a new image
   that must be published before the follow-up can run:

   ```bash
   podman build -t quay.io/okd/graph-pr-bot:latest -f graph-pr-bot/Containerfile graph-pr-bot
   podman push quay.io/okd/graph-pr-bot:latest
   ```

   An alternative registry/tag can be supplied using `graph-pr-image` in the
   promotion templates. An immutable image digest is also supported.
4. Apply the resources from the repository root:

   ```bash
   oc apply -k environments/moc
   ```

   This creates `okd-release-promoter` with namespace-scoped permission to get
   and create PipelineRuns, and `okd-graph-pr-bot` with no API RoleBinding. The
   supplied promotion templates use the promoter account. Custom promotion
   manifests enabling graph PRs must also use it or an account with equivalent
   namespace-scoped permissions.
5. Ensure the manual/scheduled trigger downloads the updated stable and next
   templates. The TriggerTemplate defaults to URLs under the pipeline repo's
   `main` branch. Applying Kustomize does not publish local templates to those
   URLs. The updated templates explicitly enable graph PRs; the reusable
   release Pipeline itself defaults this feature to disabled for other callers.

The existing `oc run ... manual-promotion` commands then trigger graph proposals
automatically. Direct creation of either supplied promotion PipelineRun also
enables the follow-up.

The bot image installs Python, PyYAML, and OpenSSL at build time, runs without
root, and installs no packages during promotion. The private key is mounted only
in the PR step. It uses GitHub's Git Data API for commits and branches and the
Pull Requests API for PRs; it needs no git checkout or release PVC.

## Channel and risk policy

* Stable releases update `stable-X.Y` channels whose feeder is `stable`.
  Engineering candidates update `candidate-X.Y` channels whose feeder is
  `candidate`. The entire release name must match the channel's feeder filter.
* Tombstoned versions are skipped. Already present versions are successful
  no-ops. The bot never creates new channels or backfills other missing releases.
  A release with no matching configured channel fails with an actionable error.
* Only zero feeder delays are supported because the tags API has no acceptance
  timestamp. Errata-based promotion is unsupported for OKD.
* Unresolved risks in earlier releases of the same major/minor must have their
  declarations extended to the new release, or a verified `fixedIn` version
  recorded. The bot does not infer fixes from prose or remove blocked edges.
  This follows OCP's stabilization guard against dropping existing risks.
* Adding an older minor's release to a newer channel additionally requires a
  tested path into that channel's major/minor in the controller graph, through
  versions available in the proposed channel. Unconditional blocked edges are
  excluded; conditional paths retain their declared risks.
* The payload's existing `--previous` metadata supplies upgrade edges. This bot
  proposes channel membership and retains all blocked-edge files.

For example, if `5.0.0-okd-scos.1` declares an unresolved signing risk, acceptance
of `.2` alone does not prove that risk fixed. Extend the risk to `.2` or record
the verified `fixedIn` before rerunning the graph proposal.

## Configuration

Pass these string parameters in a promotion PipelineRun; they propagate to the
follow-up. Defaults are also available when starting the graph Pipeline directly.

| Parameter | Default |
| --- | --- |
| `enable-graph-pr` (release Pipeline only) | `false`; supplied stable/next templates set `true` |
| `graph-data-repository` | `okd-project/cincinnati-graph-data` |
| `graph-data-base-branch` | `main` |
| `graph-release-controller` | `https://amd64.origin.releases.ci.openshift.org` |
| `graph-acceptance-timeout-seconds` | `172800` (48 hours) |
| `graph-poll-interval-seconds` | `60` |
| `graph-pr-image` | `quay.io/okd/graph-pr-bot:latest` |
| `graph-github-app-id` | `309584` |
| `graph-github-app-installation-id` | `35633111` |
| `graph-githubapp-secret-name` | `okd-githubapp-auth` |

Existing Matrix notification parameters propagate to the follow-up. It reports
its status independently of the promotion Pipeline. The launcher grants the
follow-up one extra hour beyond its acceptance wait for scheduling, API calls,
and notification, reserving five minutes for the notification. An explicit
TaskRun timeout prevents the cluster's default task timeout from shortening the
wait. With defaults, its Pipeline timeout is 49 hours. The original
promotion timeout and workspace are not extended.

## Monitor and retry

```bash
oc get pipelineruns -n okd-coreos -l app.kubernetes.io/name=okd-graph-pr
tkn pipelinerun logs <graph-pipelinerun-name> -n okd-coreos -f
oc get pipelinerun <graph-pipelinerun-name> -n okd-coreos -o jsonpath='{.status.pipelineResults}'
```

The launch Task logs the follow-up name. The follow-up's
`okd.io/source-pipelinerun` annotation records the originating promotion. It has
no owner reference to the promotion, so deleting the completed promotion does
not terminate the acceptance wait. A successful promotion means the follow-up
was launched; check the follow-up to confirm PR creation.

After acceptance or resolving credentials/risk declarations, edit `release-name`
and `target-release-stream` in the graph-only template, then create a fresh run:

```bash
oc create -f environments/moc/pipelineruns/okd-graph-pr-pipelinerun.yaml -n okd-coreos
```

If changing the acceptance timeout in a manually created graph-only run, also
update `spec.timeouts.pipeline`, `spec.timeouts.tasks`, and the
`propose-graph-update` entry in `spec.taskRunSpecs` to allow the new wait. Keep
five minutes reserved in `spec.timeouts.finally`.

Retries reuse an open PR, and merged additions become no-ops. A closed,
unmerged PR requires manual reopening. If a previous run created its branch
but PR creation failed, a retry reuses the branch only if its complete file
tree matches the expected update. Different contents, including base changes
since that incomplete run, require manual review; the bot never overwrites
reviewer edits or force-updates a branch. Concurrent runs recover from duplicate
branch/PR creation responses.

## Read-only preview and tests

Use an existing project virtual environment with PyYAML, or create one:

```bash
python3 -m venv graph-pr-bot/.venv
source graph-pr-bot/.venv/bin/activate
python3 -m pip install -r graph-pr-bot/requirements.txt
```

Preview an accepted release against a local graph-data checkout, without
credentials or writes:

```bash
python3 graph-pr-bot/graph_pr.py propose --dry-run \
  --graph-data-dir ../okd-cincinnati-graph-data \
  --release-name 5.0.0-okd-scos.2 --release-stream 5-scos-stable
```

Omit `--graph-data-dir` to read the configured GitHub repository instead. The
preview still checks controller acceptance and all admission policies.

```bash
python3 -m unittest discover -s graph-pr-bot -p 'test_*.py' -v
oc kustomize environments/moc
```

Tests use fake clocks and an in-memory GitHub HTTP API. They cover exact-tag
acceptance, transient failures, rejection, timeout, stable/next routing,
filters/tombstones, risk carry-forward, cross-minor connectivity, PR recovery,
duplicate prevention, and the actual launcher script with a fake `oc`. The
deployment tests require local `oc`, `jq`, and Bash. They contact no cluster and
create no GitHub objects.
