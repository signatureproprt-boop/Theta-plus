#!/usr/bin/env bash
# Phase F public smoke (public ingress, not localhost).
set -u
B=https://nifty-pcr-signal.preview.emergentagent.com/api
p() { echo "--- $1"; }

p "live chart signals"
curl -s "$B/chart/signals" -o /tmp/live.json -w "status=%{http_code}\n"
python3 - <<'EOF'
import json
d=json.load(open('/tmp/live.json'))
assert d["symbol"]=="NIFTY" and d["tradingview_symbol"]=="NSE:NIFTY" and d["timeframe"]=="5", d
assert d["data_origin_label"]=="LIVE • SIMULATED DATA", d["data_origin_label"]
assert d["include_wait"] is False
assert all(m["decision"]!="WAIT" for m in d["markers"])
print("live ok: markers=",d["count"],"latest=",d["latest_decision"],"vwap=",d["vwap"])
EOF

p "replay-synthetic chart signals"
curl -s "$B/chart/signals?origin=REPLAY-SYNTHETIC" -o /tmp/rep.json -w "status=%{http_code}\n"
python3 - <<'EOF'
import json
d=json.load(open('/tmp/rep.json'))
assert d["data_origin"]=="REPLAY-SYNTHETIC" and d["count"]>=1, d["count"]
m=d["markers"][0]
assert "+05:30" in m["timestamp"], m["timestamp"]
assert m["vwap"] is not None and m["max_score"]==100
kinds={x["decision"] for x in d["markers"]}
print("replay ok:",d["count"],"decisions=",kinds,"first score=",m["score"],"/",m["max_score"])
EOF

p "marker detail"
SID=$(python3 -c "import json;print(json.load(open('/tmp/rep.json'))['markers'][0]['signal_id'])")
curl -s "$B/chart/signals/$SID" -w " status=%{http_code}\n" | tail -c 400

p "negative: bad origin (expect 422)"
curl -s -o /dev/null -w "status=%{http_code}\n" "$B/chart/signals?origin=TRADINGVIEW"

p "negative: unknown signal_id (expect 404)"
curl -s -o /dev/null -w "status=%{http_code}\n" "$B/chart/signals/nope"

p "regression: dashboard still OK"
curl -s "$B/dashboard" -o /tmp/dash.json -w "status=%{http_code}\n"
python3 -c "import json;d=json.load(open('/tmp/dash.json'));print('dashboard signal=',d['signal']['decision'],'health=',d['data_health']['overall'])"
