# OKD Release Promotions

Promotes OKD releases (stable and next) to production.

## Usage

### Manual Promotion

```bash
# Promote both releases
oc run okd-manual-promotion-$(date +%Y%m%d-%H%M%S) --image=curlimages/curl --restart=Never \
  -- curl -X POST http://el-cluster-command-listener.okd-coreos.svc.cluster.local:8080 \
  -H 'Content-Type: application/json' \
  -d '{"message":"manual", "next":"true", "stable":"true"}'

# Promote only next
oc run okd-manual-promotion-$(date +%Y%m%d-%H%M%S) --image=curlimages/curl --restart=Never \
  -- curl -X POST http://el-cluster-command-listener.okd-coreos.svc.cluster.local:8080 \
  -H 'Content-Type: application/json' \
  -d '{"message":"manual", "next":"true"}'

# Promote only stable  
oc run okd-manual-promotion-$(date +%Y%m%d-%H%M%S) --image=curlimages/curl --restart=Never \
  -- curl -X POST http://el-cluster-command-listener.okd-coreos.svc.cluster.local:8080 \
  -H 'Content-Type: application/json' \
  -d '{"message":"manual", "stable":"true"}'

# Promote from local yaml
oc run okd-manual-promotion-$(date +%Y%m%d-%H%M%S) --image=curlimages/curl --restart=Never \
  -- curl -X POST http://el-cluster-command-listener.okd-coreos.svc.cluster.local:8080 \
  -H 'Content-Type: application/json' \
  -d '{"message":"manual", "next":"true", "next-yaml": "../environments/moc/pipelineruns/okd-release-next-pipelinerun.yaml"}'

```


### Automatic
CronJob runs every Monday at 5:00 AM UTC, automatically promoting both releases with default YAML URLs.

### Graph-data pull requests

With the updated stable and next PipelineRun templates, manual and scheduled
promotions also launch `okd-graph-pr-pipeline` after the promoted image is tagged
in the release controller. It waits up to 48 hours for the exact promoted release
to become `Accepted`, then opens a PR adding it to matching graph-data channels.
Rejected or failed releases produce a failed follow-up without a PR.

The graph follow-up is independent of promotion completion and uses no shared
release workspace. Its success/failure notification is separate from the
promotion notification. Monitor the follow-up to confirm PR creation.

Deploy the bot image and App permissions described in
[`graph-pr-bot/README.md`](../graph-pr-bot/README.md) first. The default trigger URLs
download promotion templates from `main`; those templates must include the new
`enable-graph-pr: "true"` parameter and `okd-release-promoter` service account.
Applying the Tasks and Pipelines alone does not update remote template URLs.

To test the updated local templates directly after deployment, use
`oc create -f environments/moc/pipelineruns/okd-release-stable-pipelinerun.yaml -n okd-coreos`
from the repository root (or the corresponding next template).

## Monitoring

```bash
# List recent promotion pods
oc get pods -n okd-coreos | grep okd-manual-promotion

# View logs (example: okd-manual-promotion-20251226-143052)
oc logs okd-manual-promotion-20251226-143052 -n okd-coreos -f
```

The manual curl pod only submits the event. Follow graph proposals separately:

```bash
oc get pipelineruns -n okd-coreos -l app.kubernetes.io/name=okd-graph-pr
tkn pipelinerun logs <graph-pipelinerun-name> -n okd-coreos -f
```

For graph-only retries after acceptance, credential fixes, or risk updates, see
the [graph bot instructions](../graph-pr-bot/README.md). A retry does not promote
another release or allocate another release number.
