#!/usr/bin/env bash
# Phase G public smoke (public ingress). Read-only endpoints only.
set -u
B=https://nifty-pcr-signal.preview.emergentagent.com/api
p() { echo "--- $1"; }

p "paper summary"
curl -s "$B/paper/summary" -o /tmp/psum.json -w "status=%{http_code}\n"
python3 - <<'EOF'
import json
d=json.load(open('/tmp/psum.json'))
assert d["mode"] in ("RESEARCH","PAPER"), d["mode"]
assert "PAPER" in d["label"].upper() and "HYPOTHETICAL" in d["label"].upper()
assert d["target_points"]==35 and d["stoploss_points"]==30 and d["max_open_positions"]==1
assert d["data_origin_label"]=="LIVE • SIMULATED DATA"
print("summary ok:",d["mode"],"positions=",d["total_positions"],"open=",d["open_positions"],"net=",d["net_pnl"])
EOF

p "paper positions"
curl -s "$B/paper/positions" -o /tmp/ppos.json -w "status=%{http_code}\n"
python3 - <<'EOF'
import json
d=json.load(open('/tmp/ppos.json'))
print("positions:",len(d))
if d:
    x=d[0]
    for k in ("paper_position_id","signal_id","option_type","strike","entry_price","target_price","stoploss_price","state","data_origin_label"):
        assert k in x, k
    assert x["state"] in ("OPEN","TARGET","STOPLOSS","INVALIDATED","EXPIRED","NO_DATA","AMBIGUOUS")
    if x["entry_price"] is not None:
        assert x["target_price"]==round(x["entry_price"]+35,2)
        assert x["stoploss_price"]==round(x["entry_price"]-30,2)
    print("first:",x["option_type"],x["strike"],"entry",x["entry_price"],"state",x["state"])
    open('/tmp/pid','w').write(x["paper_position_id"])
EOF

p "paper trades + single position"
curl -s -o /dev/null -w "trades status=%{http_code}\n" "$B/paper/trades"
if [ -f /tmp/pid ]; then curl -s -o /dev/null -w "position status=%{http_code}\n" "$B/paper/positions/$(cat /tmp/pid)"; fi

p "alert log (must be credential-free)"
curl -s "$B/alerts" -o /tmp/alerts.json -w "status=%{http_code}\n"
python3 - <<'EOF'
import json
raw=open('/tmp/alerts.json').read()
low=raw.lower()
for banned in ("telegram_bot_token","telegram_chat_id","sendmessage","api.telegram.org"):
    assert banned not in low, banned
d=json.loads(raw)
s=d["status"]
assert s["real_delivery_verified"] is False
print("alerts ok: status=",s["status"],"delivery=",s["delivery"],"records=",len(d["records"]),
      "types=",{r["alert_type"] for r in d["records"]} or "none")
assert all(r["alert_type"]!="WAIT" for r in d["records"])
EOF

p "negative: unknown paper position (expect 404)"
curl -s -o /dev/null -w "status=%{http_code}\n" "$B/paper/positions/does-not-exist"

p "negative: POST to a paper route must not exist (expect 405)"
curl -s -o /dev/null -X POST -w "status=%{http_code}\n" "$B/paper/positions"

p "regression: dashboard + chart still OK"
curl -s -o /dev/null -w "dashboard status=%{http_code}\n" "$B/dashboard"
curl -s -o /dev/null -w "chart status=%{http_code}\n" "$B/chart/signals"
