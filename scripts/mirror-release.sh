#!/usr/bin/env bash
set -euo pipefail

DEFAULT_GPG_PUBLIC_KEY_URL="https://raw.githubusercontent.com/openshift/cluster-update-keys/master/keys/verifier-public-key-openshift-ci-4"
DEFAULT_ACCEPTED_STREAMS_URL="https://amd64.origin.releases.ci.openshift.org/api/v1/releasestreams/accepted"

usage() {
    cat <<'EOF'
Mirror an OKD release using the same workflow as the mirror-release Tekton task.

Usage:
  mirror-release.sh \
    --release-name NAME \
    --release-pullspec PULLSPEC \
    --target-release-stream STREAM \
    --content-mirror-pushspec PUSHSPEC \
    --release-mirror-pushspec PUSHSPEC \
    [--registry-config PATH] \
    [--gpg-public-key-url URL] \
    [--accepted-streams-url URL] \
    [--gpg-home PATH] \
    [--result-file PATH]

Required tools: oc, curl, gpg, and jq.

If --registry-config is omitted, the script checks REGISTRY_AUTH_FILE, the
Podman auth file, and ~/.docker/config.json, in that order. A temporary GPG
home is used unless --gpg-home or GNUPGHOME is set.
EOF
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

release_name=""
release_pullspec=""
target_release_stream=""
content_mirror_pushspec=""
release_mirror_pushspec=""
registry_config="${REGISTRY_AUTH_FILE:-}"
gpg_public_key_url="$DEFAULT_GPG_PUBLIC_KEY_URL"
accepted_streams_url="$DEFAULT_ACCEPTED_STREAMS_URL"
gpg_home="${GNUPGHOME:-}"
result_file=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --release-name)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            release_name="$2"
            shift 2
            ;;
        --release-pullspec)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            release_pullspec="$2"
            shift 2
            ;;
        --target-release-stream)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            target_release_stream="$2"
            shift 2
            ;;
        --content-mirror-pushspec)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            content_mirror_pushspec="$2"
            shift 2
            ;;
        --release-mirror-pushspec)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            release_mirror_pushspec="$2"
            shift 2
            ;;
        --registry-config)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            registry_config="$2"
            shift 2
            ;;
        --gpg-public-key-url)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            gpg_public_key_url="$2"
            shift 2
            ;;
        --accepted-streams-url)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            accepted_streams_url="$2"
            shift 2
            ;;
        --gpg-home)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            gpg_home="$2"
            shift 2
            ;;
        --result-file)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            result_file="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

[[ -n "$release_name" ]] || die "--release-name is required"
[[ -n "$release_pullspec" ]] || die "--release-pullspec is required"
[[ -n "$target_release_stream" ]] || die "--target-release-stream is required"
[[ -n "$content_mirror_pushspec" ]] || die "--content-mirror-pushspec is required"
[[ -n "$release_mirror_pushspec" ]] || die "--release-mirror-pushspec is required"

for command_name in oc curl gpg jq; do
    command -v "$command_name" >/dev/null 2>&1 || die "required command not found: $command_name"
done

if [[ -z "$registry_config" ]]; then
    registry_candidates=()
    if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
        registry_candidates+=("$XDG_RUNTIME_DIR/containers/auth.json")
    fi
    registry_candidates+=("$HOME/.config/containers/auth.json" "$HOME/.docker/config.json")

    for candidate in "${registry_candidates[@]}"; do
        if [[ -f "$candidate" ]]; then
            registry_config="$candidate"
            break
        fi
    done
fi

[[ -n "$registry_config" ]] || die "no registry auth file found; pass --registry-config"
[[ -f "$registry_config" ]] || die "registry auth file does not exist: $registry_config"

temporary_gpg_home=""
cleanup() {
    if [[ -n "$temporary_gpg_home" ]]; then
        rm -rf -- "$temporary_gpg_home"
    fi
}
trap cleanup EXIT

if [[ -z "$gpg_home" ]]; then
    temporary_gpg_home="$(mktemp -d)"
    gpg_home="$temporary_gpg_home"
fi
mkdir -p "$gpg_home"
chmod 700 "$gpg_home"
export GNUPGHOME="$gpg_home"

echo "Importing release verification key..."
curl --fail --location --silent --show-error "$gpg_public_key_url" | gpg --import

echo "Looking up accepted releases for ${target_release_stream}..."
accepted_streams="$(curl --fail --location --silent --show-error "$accepted_streams_url")"
all_releases="$(jq -r --arg stream "$target_release_stream" '.[$stream] // [] | .[]' <<<"$accepted_streams")"

if [[ "$release_name" =~ ^([0-9]+)\.([0-9]+)\. ]]; then
    current_major="${BASH_REMATCH[1]}"
    current_minor="${BASH_REMATCH[2]}"
    current_major_minor="${current_major}.${current_minor}"
else
    die "release name must start with a major.minor.patch version: $release_name"
fi

previous_args=()
# Compute previous edges when the target stream already has releases, OR when this
# is the first release of a new major (minor == 0): its previous edge comes from
# the previous major's stream, so the (empty) target stream must not short-circuit
# the lookup.
if [[ -n "$all_releases" || "$current_minor" -eq 0 ]]; then
    previous_releases=""
    if (( current_minor == 0 )); then
        (( current_major > 0 )) || die "cannot determine the previous stream for major version 0"
        previous_major="$((current_major - 1))"
        previous_stream="${target_release_stream/#$current_major/$previous_major}"
        previous_stream_releases="$(jq -r --arg stream "$previous_stream" '.[$stream] // [] | .[]' <<<"$accepted_streams")"
        previous_major_minor="$(cut -d. -f1-2 <<<"$previous_stream_releases" | sort -t. -k1,1n -k2,2n | tail -n 1)"
        if [[ -n "$previous_major_minor" ]]; then
            previous_releases="$(grep -E "^${previous_major_minor//./\\.}\." <<<"$previous_stream_releases" | head -n 1 || true)"
        fi
    else
        previous_minor="$((current_minor - 1))"
        previous_major_minor="${current_major}.${previous_minor}"
        previous_releases="$(grep -E "^${previous_major_minor//./\\.}\." <<<"$all_releases" | head -n 1 || true)"
    fi

    current_releases="$(grep -E "^${current_major_minor//./\\.}\." <<<"$all_releases" | grep -Fxv "$release_name" || true)"
    filtered_releases="$(printf '%s\n%s\n' "$previous_releases" "$current_releases" | sed '/^$/d')"

    if [[ -n "$filtered_releases" ]]; then
        previous_csv="$(tr '\n' ',' <<<"$filtered_releases" | sed 's/,$//')"
        previous_args+=("--previous=$previous_csv")
    else
        die "no previous releases found for ${release_name}"
    fi
else
    die "no accepted releases found for stream ${target_release_stream}"
fi

release_mirror="${release_mirror_pushspec}:${release_name}"

echo "Mirroring ${release_pullspec} to ${release_mirror}..."
oc adm release new \
    --request-timeout=60s \
    --registry-config="$registry_config" \
    --from-release "$release_pullspec" \
    --mirror "$content_mirror_pushspec" \
    --to-image "$release_mirror" \
    --name="$release_name" \
    --keep-manifest-list=true \
    "${previous_args[@]}"

if [[ -n "$result_file" ]]; then
    printf '%s' "$release_mirror" >"$result_file"
fi

echo "Mirrored release: $release_mirror"
