#!/usr/bin/env bash
# One reproducible live run; saves only actual WB responses through the app Collector.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

label=manual
skip_build=false
phrase=
destination=${WB_TEST_DESTINATION:-1259570991}
while (($#)); do
  case "$1" in
    --label) label=${2:?Expected label}; shift 2 ;;
    --query) phrase=${2:?Expected search phrase}; shift 2 ;;
    --dest) destination=${2:?Expected destination}; shift 2 ;;
    --skip-build) skip_build=true; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 64 ;;
  esac
done
[[ "$label" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'Label must contain letters, digits, _ or -.' >&2; exit 64; }
[[ "$destination" =~ ^-?[0-9]+$ ]] || { echo 'Destination must be an integer.' >&2; exit 64; }

compose=(docker compose -f compose.yaml -f compose.browser.yaml)
"${compose[@]}" config --quiet
if [[ -n "$("${compose[@]}" ps --status running -q collector)" ]]; then
  echo 'Collector is running. Stop it before a diagnostic run: docker compose stop collector' >&2
  exit 75
fi
if [[ "$skip_build" == false ]]; then
  "${compose[@]}" build collector
fi

run_id="$(date -u +%Y%m%dT%H%M%SZ)-${label}-$$"
container="wb-serp-test-${run_id}"
report_dir="data/live-tests/${run_id}"
artifact="/app/data/verification/${run_id}.jsonl"
mkdir -p "$report_dir"
args=(python -m scripts.probe_batch_wb --output "$artifact" --save-live)
[[ -z "$phrase" ]] || args+=(--query "$phrase")

echo "New isolated browser profile; fixed dest=${destination}; top_n=200."
echo "Network label supplied by operator: ${label}. VPN state is not changed by this script."
echo "Reports: ${report_dir}"
set +e
"${compose[@]}" run -T --no-deps --name "$container" \
  -e WB_DESTINATION="$destination" -e WB_TOP_N=200 \
  -e WB_BROWSER_PROFILE_PATH="/app/data/browser-profile-${run_id}" \
  collector "${args[@]}" \
  2>&1 | tee "$report_dir/console.log"
run_status=${PIPESTATUS[0]}
set -e

copy_status=0
docker cp "${container}:${artifact}" "$report_dir/results.jsonl" || copy_status=1
docker cp "${container}:${artifact%.jsonl}.summary.json" "$report_dir/summary.json" || copy_status=1
docker rm "$container" >/dev/null
echo "Report folder: ${report_dir}"
if (( copy_status )); then
  echo 'Run ended before all diagnostic artifacts were written; see console.log.' >&2
  exit 1
fi
if (( run_status )); then
  echo 'Live run did not confirm every requested top-200. See summary.json and results.jsonl.' >&2
fi
exit "$run_status"
