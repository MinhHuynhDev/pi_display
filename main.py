#!/usr/bin/env python3
"""YNAB money display: this month's spending by category group, income, and top purchases.

Shows:
  - month and year at the top
  - total spent this month in the NECESSITIES and DISCRETIONARY category groups
  - total income this month
  - the five largest purchases this month

Run on a Raspberry Pi, open http://localhost:8080 in a fullscreen browser.
Standard library only. Your YNAB token never leaves this machine.

Environment:
  YNAB_TOKEN       (required) personal access token from YNAB > Account Settings > Developer
  YNAB_BUDGET_ID   optional; your plan/budget ID (defaults to "last-used")
  NECESSITIES_GROUP_ID, DISCRETIONARY_GROUP_ID
                   optional; YNAB category group IDs (otherwise matched by group name)
  TZ_NAME          optional; defaults to America/Los_Angeles
  REFRESH_SECONDS  optional; how often to pull from YNAB (default 300)
  SYNC_BANKS_SECONDS optional; how often to ask YNAB to import new bank transactions,
                   like pressing Import in the app (default 900; 0 = off)
  PORT             optional; default 8080
  YNAB_DEMO=1      serve fake data (for testing the layout)
"""
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
INCOME_CATEGORY = "Inflow: Ready to Assign"
TOP_N = 5

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


def fetch_category_groups():
    """Return {category_id: (group_id, group_name)} for every category in the budget."""
    out = {}
    for g in api_get("categories")["category_groups"]:
        for c in g.get("categories", []):
            out[c["id"]] = (g["id"], g["name"])
    return out


def group_label(cat_group, category_id):
    """Which of our GROUPS (if any) does this category belong to?"""
    gid, gname = cat_group.get(category_id, (None, ""))
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
    }
    payees = {"c1": ["Trader Joe's", "Costco", "Vons"], "c2": ["SCE", "SoCalGas"],
              "c3": ["Shell", "Chevron"], "c4": ["Ramen Shop", "Taqueria"],
              "c5": ["Amazon", "Book Store"], "c6": ["Savings"]}
    txns, d, today = [], since, datetime.now(TZ).date()
    while d <= today:
        for _ in range(rnd.randint(1, 3)):
            cid = rnd.choice(list(cats))
            txns.append({"date": d.isoformat(), "amount": -rnd.randint(8, 260) * 1000,
                         "category_id": cid, "category_name": cats[cid][0],
                         "payee_name": rnd.choice(payees[cid]), "transfer_account_id": None,
                         "deleted": False, "subtransactions": []})
        if d.day == 2:
            txns.append({"date": d.isoformat(), "amount": -2100_000, "category_id": "c2",
                         "category_name": "Utilities", "payee_name": "Landlord",
                         "transfer_account_id": None, "deleted": False, "subtransactions": []})
        if d.day in (1, 15):
            txns.append({"date": d.isoformat(), "amount": 3200_000, "category_id": "inc",
                         "category_name": INCOME_CATEGORY, "payee_name": "Paycheck",
                         "transfer_account_id": None, "deleted": False, "subtransactions": []})
        d += timedelta(days=1)
    return txns, {k: ("gid-" + v[1].lower(), v[1]) for k, v in cats.items()}


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


def summarize(txns, cat_group):
    today = datetime.now(TZ).date()
    month_start = today.replace(day=1)
    totals = {g: 0 for g in GROUPS}
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
            label = group_label(cat_group, cid)
            if label:
                totals[label] += -amt  # outflows add; refunds subtract
        # Top purchases: one row per outflow transaction (splits stay as one purchase).
        if not is_income_txn and t["amount"] < 0:
            subs = [s for s in t.get("subtransactions") or [] if not s.get("deleted")]
            purchases.append({
                "payee": t.get("payee_name") or "(no payee)",
                "category": "Split" if len(subs) > 1 else (t.get("category_name") or "Uncategorized"),
                "date": date.fromisoformat(t["date"]).strftime("%b %-d"),
                "amount": round(-t["amount"] / 1000, 2),
            })

    purchases.sort(key=lambda p: p["amount"], reverse=True)
    return {
        "month": today.strftime("%B %Y"),
        "groups": [{"name": g, "spent": round(totals[g] / 1000, 2)} for g in GROUPS],
        "income": round(income / 1000, 2),
        "top": purchases[:TOP_N],
    }


def unknown_category_ids(txns, cat_group, ignore):
    """Category IDs used by transactions that we have no group for yet."""
    seen = set()
    for t in txns:
        if t.get("deleted"):
            continue
        for _, cid, cname in lines(t):
            if cid and cname != INCOME_CATEGORY:
                seen.add(cid)
    return seen - cat_group.keys() - ignore


def refresh_loop():
    cat_group = {}      # category_id -> (group_id, group_name); fetched once, kept in memory
    ignore = set()      # category IDs that stayed unknown even after a refetch
    last_sync = 0.0
    while True:
        try:
            month_start = datetime.now(TZ).date().replace(day=1)
            if DEMO:
                txns, cat_group = demo_data(month_start)
            else:
                if SYNC_SECONDS and time.time() - last_sync >= SYNC_SECONDS:
                    last_sync = time.time()  # set first so a failure doesn't retry every cycle
                    try:
                        api_post("transactions/import")
                    except Exception as e:  # not fatal; we still show what YNAB has
                        print(f"bank import request failed: {type(e).__name__}: {e}",
                              file=sys.stderr, flush=True)
                txns = fetch_transactions(month_start)
                if not cat_group:
                    cat_group = fetch_category_groups()
                # Refetch only if a transaction uses a category we haven't seen
                # (e.g. you added one in YNAB this month).
                missing = unknown_category_ids(txns, cat_group, ignore)
                if missing:
                    cat_group = fetch_category_groups()
                    ignore |= missing - cat_group.keys()
            data = summarize(txns, cat_group)
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
 --nec:#60a5fa;--dis:#fbbf24;--inc:#4ade80;--err:#fb7185}
*{box-sizing:border-box}
body{margin:0;height:100vh;background:var(--bg);color:var(--fg);overflow:hidden;
 font-family:system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;flex-direction:column;padding:2.2vmin 2.5vmin;gap:2vmin}
h1{margin:0;text-align:center;font-size:6.5vmin;font-weight:700;letter-spacing:.04em}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:2vmin}
.tile{background:var(--card);border-radius:2vmin;padding:2.2vmin 2.6vmin;border-top:.8vmin solid var(--c)}
.tile .lab{font-size:2.8vmin;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
.tile .val{font-size:8.5vmin;font-weight:700;font-variant-numeric:tabular-nums;color:var(--c);margin-top:.6vmin}
.top{flex:1;min-height:0;background:var(--card);border-radius:2vmin;padding:2vmin 2.6vmin;display:flex;flex-direction:column}
.top h2{margin:0 0 1vmin;font-size:2.8vmin;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);font-weight:600}
.rows{flex:1;display:flex;flex-direction:column;justify-content:space-evenly}
.r{display:grid;grid-template-columns:4vmin minmax(0,1fr) auto auto;align-items:baseline;gap:1.5vmin;
 font-size:3.6vmin;border-bottom:1px solid var(--line);padding:.4vmin 0}
.r:last-child{border-bottom:0}
.n{color:var(--dim)}.p{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.c{color:var(--dim);font-size:2.6vmin;white-space:nowrap}
.a{text-align:right;font-weight:600;font-variant-numeric:tabular-nums;min-width:16vmin}
.foot{font-size:2vmin;color:var(--dim);text-align:right}
.err{color:var(--err)}
@media (max-width:700px){.tiles{grid-template-columns:1fr}body{height:auto;overflow:auto}}
</style></head><body>
<h1 id="month">&nbsp;</h1>
<div class="tiles" id="tiles"></div>
<div class="top"><h2>Top purchases</h2><div class="rows" id="rows"></div></div>
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
  const tiles=d.groups.map((g,i)=>[g.name,g.spent,colors[i%2]]).concat([["Income",d.income,"var(--inc)"]]);
  document.getElementById("tiles").innerHTML=tiles.map(([l,v,c])=>
   `<div class="tile" style="--c:${c}"><div class="lab">${esc(l)}</div><div class="val">${whole(v)}</div></div>`).join("");
  document.getElementById("rows").innerHTML=d.top.length?d.top.map((p,i)=>
   `<div class="r"><span class="n">${i+1}</span><span class="p">${esc(p.payee)}</span><span class="c">${esc(p.category)} &middot; ${esc(p.date)}</span><span class="a">${cents(p.amount)}</span></div>`).join(""):
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
