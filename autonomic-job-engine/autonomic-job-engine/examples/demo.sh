#!/usr/bin/env bash
# Walk through the API against a running stack (docker compose up).
#   ./examples/demo.sh [http://localhost:8000]
set -euo pipefail
BASE=${1:-http://localhost:8000}
H=(-H 'content-type: application/json')
[[ -n "${AJE_API_KEY:-}" ]] && H+=(-H "X-API-Key: ${AJE_API_KEY}")
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
WORKER='"command":["python","-m","aje_worker","aje_worker.examples:square"],"env":{"AJE_EXAMPLE_DELAY":"0.5"}'

echo "== 1. farm job: 1 worker by default, the built-in contract scales it on backlog"
FARM=$(curl -s "${H[@]}" -X POST "$BASE/v1/jobs" -d "{
  \"name\":\"demo-farm\",\"pattern\":\"farm\",\"max_workers\":6,
  \"worker\":{$WORKER},
  \"contract\":{\"builtin\":\"farm-throughput\",\"globals\":{\"backlog_per_worker\":3,\"idle_after\":15}},
  \"tasks\":$(python3 -c 'print(list(range(100)))')
}" | j "d['id']")
echo "job $FARM"
for _ in 1 2 3 4 5; do
  sleep 4
  curl -s "${H[@]}" "$BASE/v1/jobs/$FARM" | j "'status', d['status'], 'workers', d['workers'], d['counts'], 'queue', (d['metrics'] or {}).get('queue_depth')"
done

echo "== 2. control interface: passive mode, manual scale, suspend/resume"
curl -s "${H[@]}" -X PUT "$BASE/v1/jobs/$FARM/management" -d '{"management":"passive"}' | j "'management', d['management']"
curl -s "${H[@]}" -X POST "$BASE/v1/jobs/$FARM/scale" -d '{"workers":2}' | j "'workers', d['workers']"
curl -s "${H[@]}" -X POST "$BASE/v1/jobs/$FARM/suspend" | j "d['status']"
curl -s "${H[@]}" -X POST "$BASE/v1/jobs/$FARM/resume" | j "d['status']"
curl -s "${H[@]}" -X POST "$BASE/v1/jobs/$FARM/close" | j "'closed', d['closed']"

echo "== 3. data-parallel word count with a custom contract"
DP=$(curl -s "${H[@]}" -X POST "$BASE/v1/jobs" -d "$(python3 - <<'EOF'
import json, pathlib
print(json.dumps({
  "name": "demo-wordcount", "pattern": "data_parallel", "gather": "merge", "max_workers": 4,
  "worker": {"command": ["python", "-m", "aje_worker", "aje_worker.examples:word_count"],
             "env": {"AJE_EXAMPLE_DELAY": "0.05"}},
  "contract": {"source": pathlib.Path("examples/contracts/dp-deadline.drl").read_text(),
               "globals": {"deadline": 3}},
  "items": ["the quick brown fox", "jumps over the lazy dog"] * 50,
}))
EOF
)" | j "d['id']")
echo "job $DP"
sleep 8
curl -s "${H[@]}" "$BASE/v1/jobs/$DP/batches?include_result=true" | j "[(b['status'], b['partitions'], b['result']) for b in d]"

echo "== 4. a group of jobs sharing 4 workers"
curl -s "${H[@]}" -X POST "$BASE/v1/groups" -d "{
  \"name\":\"demo-group\",\"max_workers\":4,
  \"jobs\":[
    {\"name\":\"g-light\",\"pattern\":\"farm\",\"workers\":3,\"worker\":{$WORKER},\"tasks\":[1,2,3]},
    {\"name\":\"g-heavy\",\"pattern\":\"farm\",\"worker\":{$WORKER},\"contract\":{\"globals\":{\"backlog_per_worker\":2}},
     \"tasks\":$(python3 -c 'print(list(range(60)))'),\"close_when_done\":true}
  ]}" | j "'group', d['id'], [(x['name'], x['workers']) for x in d['jobs']]"

echo "== events of the farm job"
curl -s "${H[@]}" "$BASE/v1/jobs/$FARM/events" | j "'\n'.join(e['kind'] + '  ' + e['message'] for e in d)"
echo
echo "Live stream: curl -N $BASE/v1/jobs/$FARM/stream     Prometheus: $BASE/metrics     Docs: $BASE/docs"
