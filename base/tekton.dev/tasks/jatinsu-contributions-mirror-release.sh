#!/usr/bin/env bash
# Lines contributed by Jatin Suri (jatinsu) to mirror-release.yaml
# Extracted via git blame
set -euxo pipefail

# --- Parameters ---
RELEASE_NAME="4.21"
# RELEASE_STREAM="4.22.0-0.okd-scos-nightly"
RELEASE_STREAM="4-scos-stable"

# Get the current major.minor version i.e. 4.19.x-okd-scos.x turns to 4.19
CURRENT_MAJOR_MINOR="$(echo "$RELEASE_NAME" | cut -d. -f1-2)"
CURRENT_MAJOR="$(echo "$CURRENT_MAJOR_MINOR" | cut -d. -f1)"
CURRENT_MINOR="$(echo "$CURRENT_MAJOR_MINOR" | cut -d. -f2)"

# Get previous major.minor version for upgrade path
PREV_MINOR="$((CURRENT_MINOR - 1))"
PREV_MAJOR_MINOR="${CURRENT_MAJOR}.${PREV_MINOR}"

# Build the previous minor stream name by replacing the current major.minor with the previous one
PREV_STREAM="$(echo "$RELEASE_STREAM" | sed "s/^${CURRENT_MAJOR_MINOR}\./${PREV_MAJOR_MINOR}./")"

# Grab all of the previous releases for cincinnati
ACCEPTED_STREAMS="$(curl -s https://amd64.origin.releases.ci.openshift.org/api/v1/releasestreams/accepted)"
ALL_RELEASES="$(echo "$ACCEPTED_STREAMS" | jq -r ".[\"${RELEASE_STREAM}\"][]")"

# Get the last 1 previous major.minor releases
PREV_RELEASES="$(echo "$ALL_RELEASES" | grep "^${PREV_MAJOR_MINOR}\." | head -n 1 || true)"
if [[ -z "$PREV_RELEASES" ]]; then
  PREV_RELEASES="$(echo "$ACCEPTED_STREAMS" | jq -r "(.[\"${PREV_STREAM}\"] // [])[]" | head -n 1)"
fi
if [[ -z "$PREV_RELEASES" ]]; then
  PREV_RELEASES="$(echo "$ACCEPTED_STREAMS" | jq -r "(.[\"${PREV_STREAM%-nightly}\"] // [])[]" | head -n 1)"
fi

# Get all current major.minor releases and combine them
CURRENT_RELEASES="$(echo "$ALL_RELEASES" | grep "^${CURRENT_MAJOR_MINOR}\.")"
FILTERED_RELEASES=$(echo -e "$PREV_RELEASES\n$CURRENT_RELEASES" | grep -v '^$')

# Convert the filtered releases to a comma-separated string
PREVIOUS_RELEASES=$(echo "$FILTERED_RELEASES" | tr '\n' ',' | sed 's/,$//')

# check if the filtered releases is empty or it's not a valid comma-separated string
if ! echo "$PREVIOUS_RELEASES" | grep -q ','; then
  echo "PREVIOUS_RELEASES is not a valid comma-separated string"
fi

echo "Previous releases for --previous flag:"
echo "$PREVIOUS_RELEASES"
echo "PREVIOUS_FLAG: ${PREVIOUS_FLAG[*]}"
