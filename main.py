#!/usr/bin/env python3
"""YNAB money display: this month's spending by category group, income, pace, and recent purchases.

Shows:
  - month and year at the top
  - total spent this month in the NECESSITIES and DISCRETIONARY category groups
  - total income this month
  - month pace: how much of the month's budget is spent vs how far through the month it is
  - the five most recent purchases

Run on a Raspberry Pi, open http://localhost:8080 in a fullscreen browser.
Standard library only. Your YNAB token never leaves this machine.

Environment:
  YNAB_TOKEN       (required) personal access token from YNAB > Account Settings > Developer
  YNAB_BUDGET_ID   optional; your plan/budget ID (defaults to "last-used")
  NECESSITIES_GROUP_ID, DISCRETIONARY_GROUP_ID
                   optional; YNAB category group IDs (otherwise matched by group name)
  TZ_NAME          optional; defaults to America/Los_Angeles
  REFRESH_SECONDS  optional; how often to pull from YNAB (default 300)
  BUCKET_CATEGORY_IDS / BUCKET_CATEGORY_NAMES  optional; comma-separated bucket categories
                   (property tax, insurance, ...) where you set money aside.
  BUCKET_GROUP_IDS / BUCKET_GROUP_NAMES  same idea, for whole category groups.
                   None of these set = no Set aside tile.
  SYNC_BANKS_SECONDS optional; how often to ask YNAB to import new bank transactions,
                   like pressing Import in the app (default 900; 0 = off)
  PORT             optional; default 8080
  YNAB_DEMO=1      serve fake data (for testing the layout)
"""
import calendar
import json
import os
import random
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("YNAB_TOKEN", "")
BUDGET = os.environ.get("YNAB_BUDGET_ID", "last-used")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Los_Angeles"))
REFRESH = int(os.environ.get("REFRESH_SECONDS", "300"))
PORT = int(os.environ.get("PORT", "8080"))
# How often to ask YNAB to import new transactions from your linked banks
# (same as pressing Import in the app). 0 turns this off.
SYNC_SECONDS = int(os.environ.get("SYNC_BANKS_SECONDS", "900"))
DEMO = os.environ.get("YNAB_DEMO") == "1"

# Category groups to total up. Each is matched by its YNAB group ID if you set the
# matching env var, otherwise by name (case-insensitive).
GROUPS = ["NECESSITIES", "DISCRETIONARY"]
GROUP_IDS = {
    "NECESSITIES": os.environ.get("NECESSITIES_GROUP_ID", "").strip(),
    "DISCRETIONARY": os.environ.get("DISCRETIONARY_GROUP_ID", "").strip(),
}
# "Buckets" = category groups where you set money aside for big expenses (property tax,
# insurance, ...). Give group IDs and/or names, comma separated. If neither is set,
# the Set aside tile is hidden.
BUCKET_GROUP_IDS = [x.strip() for x in os.environ.get("BUCKET_GROUP_IDS", "").split(",") if x.strip()]
BUCKET_GROUP_NAMES = [x.strip().upper() for x in os.environ.get("BUCKET_GROUP_NAMES", "").split(",") if x.strip()]
# Or pick out individual bucket categories (useful when they live inside a group that
# also holds regular spending, e.g. Property Tax inside NECESSITIES).
BUCKET_CATEGORY_IDS = [x.strip() for x in os.environ.get("BUCKET_CATEGORY_IDS", "").split(",") if x.strip()]
BUCKET_CATEGORY_NAMES = [x.strip().upper() for x in os.environ.get("BUCKET_CATEGORY_NAMES", "").split(",") if x.strip()]
BUCKETS_ON = bool(BUCKET_GROUP_IDS or BUCKET_GROUP_NAMES or BUCKET_CATEGORY_IDS or BUCKET_CATEGORY_NAMES)
INCOME_CATEGORY = "Inflow: Ready to Assign"
RECENT_N = 5

state = {"data": None, "error": None, "updated": None}
lock = threading.Lock()


def api_get(path):
    req = urllib.request.Request(f"https://api.ynab.com/v1/budgets/{BUDGET}/{path}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"]


def api_post(path):
    """POST with an empty body (used to trigger YNAB's bank import)."""
    req = urllib.request.Request(f"https://api.ynab.com/v1/budgets/{BUDGET}/{path}",
                                 data=b"", method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["data"]


def fetch_transactions(since: date):
    return api_get(f"transactions?since_date={since.isoformat()}")["transactions"]


def fetch_categories(month_start: date):
    """Return {category_id: (group_id, group_name, budgeted, balance, name)} for this month.

    Amounts are in milliunits. 'budgeted' is what you assigned to the category this
    month; 'balance' is what is in it now (including what has rolled over)."""
    out = {}
    for c in api_get(f"months/{month_start.isoformat()}")["month"]["categories"]:
        if c.get("deleted"):
            continue
        out[c["id"]] = (c.get("category_group_id"), c.get("category_group_name") or "",
                        c.get("budgeted", 0), c.get("balance", 0), c.get("name") or "")
    return out


def is_bucket(cid, info):
    """True if this category is one of your buckets (by group or by category)."""
    return (info[0] in BUCKET_GROUP_IDS or info[1].upper() in BUCKET_GROUP_NAMES
            or cid in BUCKET_CATEGORY_IDS or info[4].upper() in BUCKET_CATEGORY_NAMES)


def group_label(cat_info, category_id):
    """Which of our GROUPS (if any) does this category belong to?"""
    info = cat_info.get(category_id)
    gid, gname = (info[0], info[1]) if info else (None, "")
    for label in GROUPS:
        want_id = GROUP_IDS.get(label)
        if want_id:
            if gid == want_id:
                return label
        elif gname.upper() == label:
            return label
    return None


def demo_data(since: date):
    rnd = random.Random(42)
    cats = {  # category_id -> (category name, group)
        "c1": ("Groceries", "Necessities"), "c2": ("Utilities", "Necessities"),
        "c3": ("Gas", "Necessities"), "c4": ("Dining Out", "Discretionary"),
        "c5": ("Fun Money", "Discretionary"), "c6": ("Emergency Fund", "Savings"),
        "c7": ("Property Tax", "Necessities"), "c8": ("Insurance", "Necessities"),
    }
    spend = ["c1", "c2", "c3", "c4", "c5", "c6"]
    budgeted = {"c1": 900_000, "c2": 2_800_000, "c3": 500_000, "c4": 600_000, "c5": 500_000,
                "c7": 500_000, "c8": 350_000}       # budgeted this month (milliunits)
    balance = {"c7": 4_200_000, "c8": 1_900_000}    # saved so far
    payees = {"c1": ["Trader Joe's", "Costco", "Vons"], "c2": ["SCE", "SoCalGas"],
              "c3": ["Shell", "Chevron"], "c4": ["Ramen Shop", "Taqueria"],
              "c5": ["Amazon", "Book Store"], "c6": ["Savings"]}
    txns, d, today = [], since, datetime.now(TZ).date()
    while d <= today:
        for _ in range(rnd.randint(1, 3)):
            cid = rnd.choice(spend)
            txns.append({"date": d.isoformat(), "amount": -rnd.randint(8, 260) * 1000,
                         "category_id": cid, "category_name": cats[cid][0],
                         "payee_name": rnd.choice(payees[cid]), "transfer_account_id": None,
                         "deleted": False, "subtransactions": []})
        if d.day == 2:
            txns.append({"date": d.isoformat(), "amount": -2100_000, "category_id": "c2",
                         "category_name": "Utilities", "payee_name": "Landlord",
                         "transfer_account_id": None, "deleted": False, "subtransactions": []})
        if d.day == 5:  # property tax paid out of its bucket
            txns.append({"date": d.isoformat(), "amount": -400_000, "category_id": "c7",
                         "category_name": "Property Tax", "payee_name": "LA County Tax",
                         "transfer_account_id": None, "deleted": False, "subtransactions": []})
        if d.day in (1, 15):
            txns.append({"date": d.isoformat(), "amount": 3200_000, "category_id": "inc",
                         "category_name": INCOME_CATEGORY, "payee_name": "Paycheck",
                         "transfer_account_id": None, "deleted": False, "subtransactions": []})
        d += timedelta(days=1)
    return txns, {k: ("gid-" + v[1].lower(), v[1], budgeted.get(k, 0), balance.get(k, 0), v[0])
                  for k, v in cats.items()}


def real_transactions(txns, today):
    """Drop deleted rows, future-dated rows, uncategorized transfers between your own
    accounts (this keeps credit card payments from double counting), and starting balances.

    A transfer that HAS a category is kept: that is how YNAB records a payment to a
    tracking account such as a mortgage (e.g. checking -> Mortgage, categorized
    "Mortgage"), and it is real spending."""
    for t in txns:
        if t.get("deleted"):
            continue
        if t.get("transfer_account_id") and not t.get("category_id"):
            continue
        if (t.get("payee_name") or "") == "Starting Balance":
            continue
        if date.fromisoformat(t["date"]) > today:
            continue
        yield t


def lines(t):
    """Yield (amount, category_id, category_name) for a transaction, expanding splits."""
    subs = [s for s in t.get("subtransactions") or [] if not s.get("deleted")]
    if subs:
        for s in subs:
            if s.get("transfer_account_id") and not s.get("category_id"):
                continue  # uncategorized transfer between your own accounts
            yield s["amount"], s.get("category_id"), s.get("category_name")
    else:
        yield t["amount"], t.get("category_id"), t.get("category_name")


def summarize(txns, cat_info):
    today = datetime.now(TZ).date()
    month_start = today.replace(day=1)
    totals = {g: 0 for g in GROUPS}
    bucket_spend = 0    # spending out of buckets (already funded in earlier months)
    income = 0
    purchases = []

    for t in real_transactions(txns, today):
        if date.fromisoformat(t["date"]) < month_start:
            continue
        is_income_txn = False
        for amt, cid, cname in lines(t):
            if cname == INCOME_CATEGORY:
                income += amt
                is_income_txn = True
                continue
            label = group_label(cat_info, cid)
            if label:
                totals[label] += -amt  # outflows add; refunds subtract
                if BUCKETS_ON and cid in cat_info and is_bucket(cid, cat_info[cid]):
                    bucket_spend += -amt
        # Recent purchases: one row per outflow transaction (splits stay as one purchase).
        if not is_income_txn and t["amount"] < 0:
            subs = [s for s in t.get("subtransactions") or [] if not s.get("deleted")]
            pd = date.fromisoformat(t["date"])
            purchases.append({
                "payee": t.get("payee_name") or "(no payee)",
                "category": "Split" if len(subs) > 1 else (t.get("category_name") or "Uncategorized"),
                "date": ("Today" if pd == today else "Yesterday" if pd == today - timedelta(days=1)
                         else pd.strftime("%b %-d")),
                "amount": round(-t["amount"] / 1000, 2),
                "_key": (pd.toordinal(), len(purchases)),  # newest date first; later entries first within a day
            })

    # Money set aside this month into the bucket groups, and what those buckets hold now.
    buckets = None
    set_aside = 0
    if BUCKETS_ON:
        mine = [i for cid, i in cat_info.items() if is_bucket(cid, i)]
        set_aside = sum(i[2] for i in mine)
        buckets = {"set_aside": round(set_aside / 1000, 2),
                   "saved": round(sum(i[3] for i in mine) / 1000, 2)}

    purchases.sort(key=lambda p: p["_key"], reverse=True)

    # Month pace: how much of this month's budget for the two spending groups has been
    # spent. Bucket categories are left out of both sides (they're funded separately).
    budget = 0
    for cid, info in cat_info.items():
        if group_label(cat_info, cid) and not (BUCKETS_ON and is_bucket(cid, info)):
            budget += info[2]
    spent_ex = sum(totals.values()) - bucket_spend
    days = calendar.monthrange(today.year, today.month)[1]
    pace = None
    if budget > 0:
        pace = {"spent": round(spent_ex / 1000, 2), "budget": round(budget / 1000, 2),
                "pct": round(100 * spent_ex / budget), "month_pct": round(100 * today.day / days),
                "day": today.day, "days": days}

    return {
        "month": today.strftime("%B %Y"),
        "groups": [{"name": g, "spent": round(totals[g] / 1000, 2)} for g in GROUPS],
        "income": round(income / 1000, 2),
        "buckets": buckets,
        # income minus spending (not counting payouts from buckets, which were funded in
        # earlier months) minus what you set aside this month
        "left": round((income - (sum(totals.values()) - bucket_spend) - set_aside) / 1000, 2),
        "pace": pace,
        "recent": [{k: v for k, v in p.items() if k != "_key"} for p in purchases[:RECENT_N]],
    }


def refresh_loop():
    last_sync = 0.0
    while True:
        try:
            month_start = datetime.now(TZ).date().replace(day=1)
            if DEMO:
                txns, cat_info = demo_data(month_start)
            else:
                if SYNC_SECONDS and time.time() - last_sync >= SYNC_SECONDS:
                    last_sync = time.time()  # set first so a failure doesn't retry every cycle
                    try:
                        api_post("transactions/import")
                    except Exception as e:  # not fatal; we still show what YNAB has
                        print(f"bank import request failed: {type(e).__name__}: {e}",
                              file=sys.stderr, flush=True)
                txns = fetch_transactions(month_start)
                # Fetched every cycle: the budgeted amounts change when you move money.
                cat_info = fetch_categories(month_start)
            data = summarize(txns, cat_info)
            with lock:
                state.update(data=data, error=None,
                             updated=datetime.now(TZ).strftime("%-I:%M %p"))
        except Exception as e:  # keep showing last good data
            with lock:
                state["error"] = f"{type(e).__name__}: {e}"
        time.sleep(REFRESH)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Money</title><style>
:root{--bg:#0e1116;--card:#171b22;--fg:#e8ecf1;--dim:#7d8896;--line:#262c36;
 --nec:#60a5fa;--dis:#fbbf24;--inc:#4ade80;--set:#c084fc;--err:#fb7185}
*{box-sizing:border-box}
body{margin:0;height:100vh;background:var(--bg);color:var(--fg);overflow:hidden;
 font-family:system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;flex-direction:column;padding:2.2vmin 2.5vmin;gap:2vmin}
h1{margin:0;text-align:center;font-size:6.5vmin;font-weight:700;letter-spacing:.04em}
.tiles{display:grid;grid-template-columns:repeat(var(--n,3),1fr);gap:2vmin}
.tile{background:var(--card);border-radius:2vmin;padding:2.2vmin 2.6vmin;border-top:.8vmin solid var(--c)}
.tile .lab{font-size:2.8vmin;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
.tile .val{font-size:7.2vmin;font-weight:700;font-variant-numeric:tabular-nums;color:var(--c);margin-top:.6vmin}
.tile .sub{font-size:2.4vmin;color:var(--dim);margin-top:.4vmin;white-space:nowrap}
.left{text-align:center;font-size:4.2vmin;color:var(--dim);margin-top:-1.4vmin;line-height:1.05}
.left b{font-size:9vmin;font-weight:800;font-variant-numeric:tabular-nums;margin-left:1.2vmin;vertical-align:-.6vmin}
.bottom{flex:1;min-height:0;display:grid;grid-template-columns:2fr 3fr;gap:2vmin}
.card{background:var(--card);border-radius:2vmin;padding:2vmin 2.6vmin;display:flex;flex-direction:column;min-height:0}
.card h2{margin:0 0 1vmin;font-size:2.8vmin;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);font-weight:600}
.pace{flex:1;display:flex;flex-direction:column;justify-content:center;gap:1.8vmin}
.pace .big{font-size:9vmin;font-weight:800;font-variant-numeric:tabular-nums;line-height:1}
.pace .big small{font-size:2.8vmin;font-weight:500;color:var(--dim);margin-left:1vmin}
.bar{position:relative;height:3.4vmin;background:#262c36;border-radius:1.7vmin}
.bar .fill{height:100%;border-radius:1.7vmin;background:var(--c)}
.bar .tick{position:absolute;top:-.8vmin;bottom:-.8vmin;width:.5vmin;background:#fff;border-radius:.3vmin}
.pace .cap{font-size:2.4vmin;color:var(--dim);line-height:1.3}
.rows{flex:1;display:flex;flex-direction:column;justify-content:space-evenly}
.r{display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:baseline;gap:1.5vmin;
 font-size:3.3vmin;border-bottom:1px solid var(--line);padding:.4vmin 0}
.r:last-child{border-bottom:0}
.p{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.c{color:var(--dim);font-size:2.4vmin;white-space:nowrap}
.a{text-align:right;font-weight:600;font-variant-numeric:tabular-nums;min-width:14vmin}
.foot{font-size:2vmin;color:var(--dim);text-align:right}
.err{color:var(--err)}
@media (max-width:700px){.tiles,.bottom{grid-template-columns:1fr}body{height:auto;overflow:auto}}
</style></head><body>
<h1 id="month">&nbsp;</h1>
<div class="left" id="left"></div>
<div class="tiles" id="tiles"></div>
<div class="bottom">
 <div class="card"><h2>Month pace</h2><div class="pace" id="pace"></div></div>
 <div class="card"><h2>Latest purchases</h2><div class="rows" id="rows"></div></div>
</div>
<div class="foot" id="foot">loading&hellip;</div>
<script>
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const whole=n=>(n<0?"\\u2212":"")+"$"+Math.abs(n).toLocaleString("en-US",{maximumFractionDigits:0});
const cents=n=>"$"+n.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const colors=["var(--nec)","var(--dis)"];
async function load(){
 const foot=document.getElementById("foot");
 try{
  const j=await (await fetch("/data.json")).json();
  if(!j.data){foot.innerHTML='<span class="err">'+esc(j.error||"waiting for first sync")+'</span>';return}
  const d=j.data;
  document.getElementById("month").textContent=d.month;
  const tiles=d.groups.map((g,i)=>[g.name,g.spent,colors[i%2],""]);
  if(d.buckets)tiles.push(["Set aside",d.buckets.set_aside,"var(--set)","saved "+whole(d.buckets.saved)]);
  tiles.push(["Income",d.income,"var(--inc)",""]);
  const tl=document.getElementById("tiles");
  tl.style.setProperty("--n",tiles.length);
  tl.innerHTML=tiles.map(([l,v,c,s])=>
   `<div class="tile" style="--c:${c}"><div class="lab">${esc(l)}</div><div class="val">${whole(v)}</div><div class="sub">${esc(s)||"&nbsp;"}</div></div>`).join("");
  document.getElementById("left").innerHTML=`Left over <b style="color:${d.left<0?"var(--err)":"var(--inc)"}">${whole(d.left)}</b>`;
  const pc=document.getElementById("pace");
  if(d.pace){
   const p=d.pace,col=p.pct>100?"var(--err)":p.pct>=85?"var(--dis)":"var(--inc)";
   pc.innerHTML=`<div class="big" style="color:${col}">${p.pct}%<small>of budget spent</small></div>
    <div class="bar" style="--c:${col}"><div class="fill" style="width:${Math.min(p.pct,100)}%"></div><div class="tick" style="left:calc(${p.month_pct}% - .25vmin)"></div></div>
    <div class="cap">${whole(p.spent)} of ${whole(p.budget)} budgeted<br>white tick = today (day ${p.day} of ${p.days})</div>`;
  }else pc.innerHTML='<div class="cap">No budget set for this month yet</div>';
  document.getElementById("rows").innerHTML=d.recent.length?d.recent.map(p=>
   `<div class="r"><span class="p">${esc(p.payee)}</span><span class="c">${esc(p.category)} &middot; ${esc(p.date)}</span><span class="a">${cents(p.amount)}</span></div>`).join(""):
   '<div class="c">No purchases yet this month</div>';
  foot.innerHTML="updated "+esc(j.updated)+(j.error?' <span class="err">(sync error, showing last data)</span>':"");
 }catch(e){foot.innerHTML='<span class="err">display offline</span>'}
}
load();setInterval(load,60000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/data.json"):
            with lock:
                body = json.dumps(state).encode()
            ctype = "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not TOKEN and not DEMO:
        raise SystemExit("Set YNAB_TOKEN (YNAB > Account Settings > Developer Settings).")
    threading.Thread(target=refresh_loop, daemon=True).start()
    print(f"Serving on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
