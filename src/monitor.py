#!/usr/bin/env python3
"""Rx Order Health Monitor.

Reads order and subscription snapshots, applies a clock to every stage of the
order lifecycle, and produces:

  1. an at-risk queue ranked by medical urgency and time over threshold
  2. a root cause tag, an owner and a recommended action for each exception
  3. a drafted message to the pet parent (and the clinic, when they own the fix)
  4. KPIs for the whole operation
  5. a self-contained dashboard (docs/index.html)

Usage:
    python src/monitor.py
"""
import csv
import json
import statistics
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Thresholds. These are starting points to tune against real data.
# ---------------------------------------------------------------------------
THRESHOLDS = {
    "approval_business_hours": 24,            # order placed -> vet approval
    "fulfillment_business_hours": 24,         # vet approval -> shipping label
    "fulfillment_business_hours_compounded": 72,
    "label_scan_hours": 24,                   # label created -> first carrier scan
    "transit_stall_hours": 48,                # no new carrier scan
    "autoship_lookahead_days": 3,             # check subscriptions this far ahead
    "address_change_window_days": 14,         # recent address change to verify
}

STAGES = [
    ("AWAITING_APPROVAL", "Awaiting vet approval"),
    ("ITEM_UNAVAILABLE", "Item unavailable"),
    ("AWAITING_FULFILLMENT", "Approved, awaiting fulfillment"),
    ("LABEL_CREATED", "Label created, no carrier scan"),
    ("IN_TRANSIT", "In transit"),
]
STAGE_LABEL = dict(STAGES)

# What the pet parent should see. "Shipped" requires a real carrier scan.
CUSTOMER_STATUS = {
    "AWAITING_APPROVAL": "Waiting on your vet",
    "ITEM_UNAVAILABLE": "Item unavailable",
    "AWAITING_FULFILLMENT": "Approved, being prepared",
    "LABEL_CREATED": "Preparing shipment",
    "IN_TRANSIT": "Shipped",
    "DELIVERED": "Delivered",
    "CANCELLED": "Cancelled",
}

DT_FIELDS = ["placed_at", "approved_at", "stock_issue_at", "customer_notified_at",
             "label_created_at", "first_scan_at", "last_scan_at", "delivered_at",
             "cancelled_at"]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M") if s else None


def load_orders(path):
    orders = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for k in DT_FIELDS:
                row[k] = parse_dt(row[k])
            row["promised_by"] = date.fromisoformat(row["promised_by"])
            row["is_autoship"] = row["is_autoship"] == "True"
            row["support_contacts"] = int(row["support_contacts"])
            orders.append(row)
    return orders


def load_subscriptions(path):
    subs = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["next_ship_date"] = date.fromisoformat(row["next_ship_date"])
            row["refills_remaining"] = int(row["refills_remaining"])
            row["address_changed_at"] = parse_dt(row["address_changed_at"])
            row["address_verified"] = row["address_verified"] == "True"
            subs.append(row)
    return subs


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------
def hours_between(start, end):
    return max(0.0, (end - start).total_seconds() / 3600)


def business_hours_between(start, end):
    """Elapsed hours between two datetimes, skipping Saturdays and Sundays."""
    if end <= start:
        return 0.0
    total, cur = 0.0, start
    while cur < end:
        next_midnight = datetime.combine(cur.date() + timedelta(days=1), time.min)
        seg_end = min(next_midnight, end)
        if cur.weekday() < 5:
            total += (seg_end - cur).total_seconds() / 3600
        cur = seg_end
    return total


def fmt_duration(hours):
    hours = int(round(hours))
    d, h = divmod(hours, 24)
    return f"{d}d {h}h" if d else f"{h}h"


def fmt_day(d):
    """'Nov 4'. Built by hand because unpadded day codes differ on Windows."""
    return f"{d.strftime('%b')} {d.day}"


def fmt_time(dt):
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


# ---------------------------------------------------------------------------
# Stage logic
# ---------------------------------------------------------------------------
def stage_of(o):
    if o["cancelled_at"]:
        return "CANCELLED"
    if o["delivered_at"]:
        return "DELIVERED"
    if o["first_scan_at"]:
        return "IN_TRANSIT"
    if o["label_created_at"]:
        return "LABEL_CREATED"
    if o["approved_at"]:
        if o["stock_status"] == "out_of_stock":
            return "ITEM_UNAVAILABLE"
        return "AWAITING_FULFILLMENT"
    return "AWAITING_APPROVAL"


def customer_status(o):
    return CUSTOMER_STATUS[stage_of(o)]


def evaluate_order(o, now, t=THRESHOLDS):
    """Return an exception record if the order is at risk, else None."""
    stage = stage_of(o)
    if stage in ("DELIVERED", "CANCELLED"):
        return None

    clock = "business"
    if stage == "AWAITING_APPROVAL":
        in_stage = business_hours_between(o["placed_at"], now)
        threshold = t["approval_business_hours"]
        root_cause, owner = "Clinic approval pending", "Clinic"
    elif stage == "ITEM_UNAVAILABLE":
        clock = "calendar"
        in_stage = hours_between(o["stock_issue_at"], now)
        threshold = 0
        owner = "Support"
        root_cause = ("Stockout, customer already told" if o["customer_notified_at"]
                      else "Stockout, customer not told")
    elif stage == "AWAITING_FULFILLMENT":
        in_stage = business_hours_between(o["approved_at"], now)
        threshold = (t["fulfillment_business_hours_compounded"]
                     if o["item_type"] == "compounded"
                     else t["fulfillment_business_hours"])
        root_cause, owner = "Fulfillment backlog", "Pharmacy partner"
    elif stage == "LABEL_CREATED":
        clock = "calendar"
        in_stage = hours_between(o["label_created_at"], now)
        threshold = t["label_scan_hours"]
        root_cause, owner = "Carrier pickup missed", "Pharmacy partner"
    else:  # IN_TRANSIT
        clock = "calendar"
        in_stage = hours_between(o["first_scan_at"], now)
        since_scan = hours_between(o["last_scan_at"] or o["first_scan_at"], now)
        promise_end = datetime.combine(o["promised_by"], time(23, 59))
        if since_scan > t["transit_stall_hours"]:
            root_cause, owner = "Carrier tracking stalled", "Carrier"
            threshold = t["transit_stall_hours"]
            over = since_scan - threshold
            return _record(o, stage, root_cause, owner, clock, in_stage, threshold, over, now)
        if now > promise_end:
            # Did the carrier lose the time, or did the order ship too late to make it?
            if o["first_scan_at"].date() >= o["promised_by"]:
                root_cause, owner = "Shipped too late to meet promise", "Support"
            else:
                root_cause, owner = "Carrier delay past promised date", "Carrier"
            over = hours_between(promise_end, now)
            return _record(o, stage, root_cause, owner, clock, in_stage, None, over, now)
        return None

    if in_stage <= threshold:
        return None
    return _record(o, stage, root_cause, owner, clock, in_stage, threshold,
                   in_stage - threshold, now)


ACTIONS = {
    "Clinic approval pending":
        "Send the clinic an approval reminder and tell the pet parent the order is waiting on their vet.",
    "Stockout, customer not told":
        "Tell the pet parent today. Ask the clinic to approve a substitute and offer wait, substitute or refund.",
    "Stockout, customer already told":
        "Confirm the restock date with the pharmacy partner and follow up on the pet parent's choice.",
    "Fulfillment backlog":
        "Escalate to the pharmacy partner's ops contact and get a ship date. Update the pet parent.",
    "Carrier pickup missed":
        "Ask the pharmacy partner to confirm the package was handed to the carrier. Keep status at Preparing shipment.",
    "Carrier tracking stalled":
        "Open a carrier trace. For critical medications, offer an expedited replacement now.",
    "Shipped too late to meet promise":
        "Send a proactive delay notice with the new delivery estimate. Log the upstream delay for root cause review.",
    "Carrier delay past promised date":
        "Send a proactive delay notice. For critical medications, offer an expedited replacement.",
}


def draft_messages(o, root_cause, in_stage, now):
    """Templated drafts. A production version could use an LLM with human review."""
    first, pet, item, clinic = o["pet_parent"], o["pet_name"], o["item"], o["clinic"]
    critical = o["urgency"] == "critical"
    clinic_msg = None

    if root_cause == "Clinic approval pending":
        parent = (f"Hi {first}, a quick update on {pet}'s order for {item}. It is waiting on "
                  f"prescription approval from {clinic}. We sent them a reminder today and will "
                  f"ship as soon as they approve.")
        if critical:
            parent += (f" If {pet} is running low, reply to this message and we will help you "
                       f"arrange a short supply through your clinic.")
        clinic_msg = (f"Hi {clinic} team, order {o['order_id']} for {pet} ({item}) has been "
                      f"waiting on prescription approval for {fmt_duration(in_stage)}."
                      + (" This is a medication the patient should not miss." if critical else "")
                      + " You can approve it from your pending approvals queue. Thank you.")
    elif root_cause == "Stockout, customer not told":
        parent = (f"Hi {first}, we want to be upfront about {pet}'s order. {item} is out of "
                  f"stock at our pharmacy partner right now. We have asked {clinic} about a "
                  f"substitute they would approve. You can wait for the restock, switch to the "
                  f"substitute once it is approved, or cancel for a full refund. Reply and tell "
                  f"us which you prefer.")
        clinic_msg = (f"Hi {clinic} team, {item} for {pet} (order {o['order_id']}) is out of "
                      f"stock. Is there a substitute you would approve for this patient?")
    elif root_cause == "Stockout, customer already told":
        parent = (f"Hi {first}, following up on {pet}'s order. {item} is still out of stock. "
                  f"We are checking the restock date today. If you would rather switch to a "
                  f"substitute or cancel for a full refund, reply and we will take care of it.")
    elif root_cause == "Fulfillment backlog":
        parent = (f"Hi {first}, {clinic} approved {pet}'s order for {item} on "
                  f"{fmt_day(o['approved_at'])}, and it has not left our pharmacy partner yet. "
                  f"That is slower than we promised and we are sorry. We escalated it today and "
                  f"will send tracking the moment it ships.")
    elif root_cause == "Carrier pickup missed":
        parent = (f"Hi {first}, {pet}'s order for {item} is packed and labeled, but the carrier "
                  f"has not picked it up yet, so tracking will not show movement. We are "
                  f"following up today and will update you when it is on the way.")
    elif root_cause == "Shipped too late to meet promise":
        parent = (f"Hi {first}, {pet}'s order for {item} is on its way, but it left our pharmacy "
                  f"later than it should have and will arrive after the date we promised "
                  f"({fmt_day(o['promised_by'])}). We are sorry for the delay. Your tracking link "
                  f"is live and we are watching this delivery until it arrives.")
        if critical:
            parent += (f" If {pet} will run out before it arrives, reply to this message and we "
                       f"will help you arrange a short supply through your clinic.")
    else:
        late = "has stopped moving in the carrier's network" if "stalled" in root_cause else \
               f"is running late and missed the delivery date we promised ({fmt_day(o['promised_by'])})"
        parent = (f"Hi {first}, {pet}'s order for {item} {late}. We are sorry. We opened a "
                  f"trace with the carrier today.")
        if critical:
            parent += (f" Because this is a medication {pet} should not miss, we can send a "
                       f"replacement by expedited shipping now. Reply YES and we will ship it.")
        else:
            parent += " We will update you tomorrow either way."
    return parent, clinic_msg


def _record(o, stage, root_cause, owner, clock, in_stage, threshold, over, now):
    parent_msg, clinic_msg = draft_messages(o, root_cause, in_stage, now)
    return {
        "order_id": o["order_id"],
        "placed": fmt_day(o["placed_at"]),
        "pet": o["pet_name"], "species": o["species"], "pet_parent": o["pet_parent"],
        "item": o["item"], "clinic": o["clinic"], "partner": o["partner"],
        "urgency": o["urgency"], "autoship": o["is_autoship"],
        "stage": stage, "stage_label": STAGE_LABEL[stage],
        "root_cause": root_cause, "owner": owner, "clock": clock,
        "hours_in_stage": round(in_stage, 1),
        "threshold_hours": threshold,
        "hours_over": round(over, 1),
        "in_stage_text": fmt_duration(in_stage),
        "over_text": fmt_duration(over),
        "customer_status": customer_status(o),
        "action": ACTIONS[root_cause],
        "message_pet_parent": parent_msg,
        "message_clinic": clinic_msg,
        "timeline": {
            "Placed": _ts(o["placed_at"]),
            "Vet approved": _ts(o["approved_at"]),
            "Label created": _ts(o["label_created_at"]),
            "First carrier scan": _ts(o["first_scan_at"]),
            "Latest carrier scan": _ts(o["last_scan_at"]),
            "Promised by": fmt_day(o["promised_by"]),
        },
    }


def _ts(dt):
    return f"{fmt_day(dt)}, {fmt_time(dt)}" if dt else None


def build_queue(orders, now, t=THRESHOLDS):
    """All at-risk orders, critical medications first, then longest over threshold."""
    queue = [r for r in (evaluate_order(o, now, t) for o in orders) if r]
    queue.sort(key=lambda r: (0 if r["urgency"] == "critical" else 1, -r["hours_over"]))
    for i, r in enumerate(queue, 1):
        r["rank"] = i
    return queue


# ---------------------------------------------------------------------------
# Autoship health
# ---------------------------------------------------------------------------
def evaluate_subscription(s, now, t=THRESHOLDS):
    """Check a subscription shortly before it ships. Return a hold record or None."""
    days_out = (s["next_ship_date"] - now.date()).days
    if not 0 <= days_out <= t["autoship_lookahead_days"]:
        return None
    issues = []
    if s["clinic_status"] != "active":
        issues.append(("Clinic no longer active",
                       "Pause autoship. Help the pet parent move the prescription to a new clinic."))
    if s["refills_remaining"] <= 0:
        issues.append(("No refills remaining",
                       "Pause autoship. Request a renewal from the clinic before charging."))
    if (s["address_changed_at"] and not s["address_verified"] and
            hours_between(s["address_changed_at"], now) <= t["address_change_window_days"] * 24):
        issues.append(("Address changed recently, not verified",
                       "Confirm the shipping address with the pet parent before this shipment."))
    if not issues:
        return None
    return {
        "subscription_id": s["subscription_id"], "pet": s["pet_name"],
        "pet_parent": s["pet_parent"], "item": s["item"], "clinic": s["clinic"],
        "urgency": s["urgency"],
        "next_ship": fmt_day(s["next_ship_date"]), "days_out": days_out,
        "issue": issues[0][0], "action": issues[0][1],
        "all_issues": [i[0] for i in issues],
    }


def build_autoship_holds(subs, now, t=THRESHOLDS):
    holds = [r for r in (evaluate_subscription(s, now, t) for s in subs) if r]
    holds.sort(key=lambda r: (r["days_out"], 0 if r["urgency"] == "critical" else 1))
    return holds


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------
def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def compute_kpis(orders, queue, holds, now):
    open_orders = [o for o in orders if stage_of(o) not in ("DELIVERED", "CANCELLED")]
    ship_hours = [hours_between(o["approved_at"], o["first_scan_at"])
                  for o in orders if o["approved_at"] and o["first_scan_at"]]
    # On-time rate: of orders whose promised date has passed or that were
    # delivered, the share delivered on or before the promised date.
    due = [o for o in orders if not o["cancelled_at"] and
           (o["delivered_at"] or now.date() > o["promised_by"])]
    on_time = [o for o in due if o["delivered_at"] and
               o["delivered_at"].date() <= o["promised_by"]]
    contacts = sum(o["support_contacts"] for o in orders)
    return {
        "total_orders": len(orders),
        "open_orders": len(open_orders),
        "at_risk": len(queue),
        "at_risk_critical": sum(1 for r in queue if r["urgency"] == "critical"),
        "approval_to_ship_median_days": round(statistics.median(ship_hours) / 24, 1),
        "approval_to_ship_p90_days": round(percentile(ship_hours, 0.9) / 24, 1),
        "on_time_rate_pct": round(100 * len(on_time) / len(due), 1),
        "on_time_base": len(due),
        "contacts_per_1000": round(1000 * contacts / len(orders)),
        "autoship_holds": len(holds),
    }


def open_orders_by_partner(orders):
    counts = {}
    for o in orders:
        if stage_of(o) not in ("DELIVERED", "CANCELLED"):
            counts[o["partner"]] = counts.get(o["partner"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_queue_csv(queue, path):
    cols = ["rank", "order_id", "urgency", "stage_label", "root_cause", "owner",
            "in_stage_text", "over_text", "pet", "item", "clinic", "partner",
            "customer_status", "action", "message_pet_parent"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(queue)


def render_dashboard(payload, template_path, out_path):
    html = template_path.read_text(encoding="utf-8")
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html.replace("/*__DATA__*/null", data), encoding="utf-8")


def main():
    now = parse_dt((ROOT / "data" / "snapshot_time.txt").read_text(encoding="utf-8").strip())
    orders = load_orders(ROOT / "data" / "orders.csv")
    subs = load_subscriptions(ROOT / "data" / "subscriptions.csv")

    queue = build_queue(orders, now)
    holds = build_autoship_holds(subs, now)
    kpis = compute_kpis(orders, queue, holds, now)

    payload = {
        "snapshot": f"{now.strftime('%a')}, {fmt_day(now)}, {now.year} at {fmt_time(now)}",
        "kpis": kpis, "queue": queue, "autoship_holds": holds,
        "stages": [{"key": k, "label": v} for k, v in STAGES],
        "open_by_partner": open_orders_by_partner(orders),
        "thresholds": THRESHOLDS,
    }
    out = ROOT / "output"
    write_queue_csv(queue, out / "at_risk_queue.csv")
    (out / "summary.json").write_text(json.dumps(
        {"snapshot": payload["snapshot"], "kpis": kpis}, indent=2) + "\n", encoding="utf-8")
    render_dashboard(payload, ROOT / "src" / "dashboard_template.html",
                     ROOT / "docs" / "index.html")

    print(f"Snapshot: {payload['snapshot']}")
    print(f"{kpis['open_orders']} open orders, {kpis['at_risk']} at risk "
          f"({kpis['at_risk_critical']} critical), {kpis['autoship_holds']} autoship holds")
    print("Wrote output/at_risk_queue.csv, output/summary.json, docs/index.html")


if __name__ == "__main__":
    main()
