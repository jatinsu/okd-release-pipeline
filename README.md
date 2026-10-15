# OKD Release Pipeline

## Prerequisites
1. create or update the following secrets:
* `okd-githubapp-auth`
  * contains key `githubapp-private-key`, the private key of GitHub App (okd-tekton-token) with permissions to create releases in `okd` project
  * graph PR automation also requires the App to be installed on `okd-project/cincinnati-graph-data`, with Contents and Pull requests read/write permissions
* `okd-quay-pull-secret`, which is a dockerconfig with permissions to push to `quay.io/okd/scos-release` and `quay.io/okd/scos-content`
* `okd-release-gpg-signing-key`
  * contains key `private.key`, which is the GPG key used to sign the OKD release
* `okd-prow-sa-auth`
  * contains key `token`, which is a Service Account token for the Prow CI cluster, with permissions to tag release images. This token comes from the secret (sa-image-tagger-token-chsw6) stored in the app.ci cluster on the origin project.
* `okd-matrix-bot-auth`, which contains an access token for Matrix (under key `token`)

2. Apply:
```bash
oc apply -k environments/moc
```

## Run the pipeline
Launch the pipeline run with the Tekton client.

Example for stable release:
```bash
oc create -f environments/moc/pipelineruns/okd-release-stable-pipelinerun.yaml
```

Example for 4.next release: 
```bash
oc create -f environments/moc/pipelineruns/okd-release-next-pipelinerun.yaml

```

## Graph-data PR automation

The stable and next promotion templates enable an independent graph-data PR
PipelineRun after `update-release-channel` tags the promoted image. It waits up
to 48 hours for that exact release to become `Accepted` in its target OKD release
controller stream, then proposes additions to matching stable or candidate
channels in `okd-project/cincinnati-graph-data`. The follow-up has no release
workspace and reports its own status, including Matrix notifications when enabled.

Before enabling it on a cluster, publish the image from
[`graph-pr-bot/Containerfile`](graph-pr-bot/Containerfile), grant the GitHub App
access, and apply the new Tasks, Pipeline, and service accounts. See
[`graph-pr-bot/README.md`](graph-pr-bot/README.md) for deployment, configuration,
read-only previews, monitoring, and rerunning only the graph proposal.

The reusable release Pipeline defaults `enable-graph-pr` to `false`; both supplied
promotion templates explicitly set it to `true` and use `okd-release-promoter`.

## Annex - running a task individually
```bash
tkn task start create-github-release \
  --param github-org-repo="okd-project/okd" \
  --param github-token-secret-key="gh-okd-token" \
  --param github-token-secret-name="gh-token" \
  --param gpg-key-id="maintainers@okd.io" \
  --param gpg-signing-key="okd-release-gpg-signing-key" \
  --param mirrored-release-pullspec="quay.io/okd/scos-release:4.12.0-0.okd-scos-2022-12-02-083740" \
  --param release-name="4.12.0-0.okd-scos-2022-12-02-083740"

```
