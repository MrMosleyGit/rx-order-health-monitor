#!/usr/bin/env python3
"""Generate synthetic prescription home delivery data.

Everything here is fictional: clinics, pharmacy partners, pets, people and
orders are invented. The generator simulates a full ground-truth timeline for
each order (approval, stock check, label, carrier scans, delivery), then hides
every event that happens after the snapshot time. What is left is what an
operations team would actually see at that moment.

Usage:
    python src/generate_orders.py
"""
import csv
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

SEED = 11
NOW = datetime(2026, 11, 12, 10, 0)  # snapshot time (a Thursday morning)
WINDOW_DAYS = 30
N_ORDERS = 500
N_SUBSCRIPTIONS = 160

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# name, slow_approver
CLINICS = [
    ("Marrowgate Veterinary", False),
    ("Tallowbrook Animal Clinic", False),
    ("Fennick & Vale Vet", True),
    ("Quillhaven Pet Clinic", False),
    ("Osterly Paws Veterinary", False),
    ("Brindlemoor Animal Hospital", True),
    ("Larkspur Hollow Vet", False),
    ("Pennywhistle Pet Clinic", False),
    ("Hartwick Fold Veterinary", False),
    ("Dunmere Animal Care", False),
]
CLOSED_CLINIC = "Dunmere Animal Care"
CLOSED_ON = datetime(2026, 10, 31)

# name, item_type, urgency
ITEMS = [
    ("Insulin glargine 10 mL vial", "rx_med", "critical"),
    ("Methimazole 5 mg tablets", "rx_med", "critical"),
    ("Levothyroxine 0.3 mg tablets", "rx_med", "critical"),
    ("Enalapril 5 mg tablets", "rx_med", "critical"),
    ("Fluticasone inhaler 110 mcg", "rx_med", "critical"),
    ("Fluoxetine 20 mg capsules", "rx_med", "critical"),
    ("Trilostane 30 mg capsules (compounded)", "compounded", "critical"),
    ("Gabapentin oral suspension (compounded)", "compounded", "standard"),
    ("Carprofen 75 mg chewables", "rx_med", "standard"),
    ("Flea and tick chewable, 6 doses", "rx_med", "standard"),
    ("Heartworm preventive, 6 doses", "rx_med", "standard"),
    ("Hydrolyzed protein diet, 25 lb", "rx_diet", "standard"),
    ("Renal support diet, 12 cans", "rx_diet", "standard"),
    ("Low fat gastrointestinal diet, 17 lb", "rx_diet", "standard"),
]

# Fulfillment partners and how they behave.
# median_h / sigma: lognormal hours from approval to label
# backlog_p / backlog_h: chance and size of a backlog stall
# oos_p: chance an item is out of stock; missed_pickup_p: label with no scan
PARTNERS = {
    "Norvale Pharmacy": dict(median_h=8, sigma=0.6, backlog_p=0.03,
                             backlog_h=(48, 110), oos_p=0.04, missed_pickup_p=0.04),
    "Harborlight Rx": dict(median_h=12, sigma=0.7, backlog_p=0.14,
                           backlog_h=(60, 200), oos_p=0.05, missed_pickup_p=0.09),
    "Cedarwick Compounding": dict(median_h=40, sigma=0.5, backlog_p=0.08,
                                  backlog_h=(72, 160), oos_p=0.03, missed_pickup_p=0.04),
    "Tamsin Pet Nutrition": dict(median_h=10, sigma=0.6, backlog_p=0.05,
                                 backlog_h=(48, 120), oos_p=0.12, missed_pickup_p=0.05),
}

PETS = ["Biscuit", "Luna", "Mochi", "Rocco", "Pepper", "Scout", "Olive", "Tank",
        "Nala", "Ziggy", "Juniper", "Moose", "Clementine", "Diesel", "Poppy",
        "Remy", "Sable", "Waffles", "Koda", "Mabel", "Otis", "Fig", "Bandit",
        "Hazel", "Gus", "Pickle", "Willow", "Ranger", "Cleo", "Bruno"]
PEOPLE = ["Alana", "Marcus", "Priya", "Devon", "Rosa", "Hank", "Imani", "Tess",
          "Caleb", "Yuki", "Noor", "Wes", "Lena", "Omar", "Greta", "Sam",
          "Bianca", "Theo", "Mirabel", "Jonah", "Kiki", "Abel", "Dara", "Flynn"]


def lognormal(rng, median, sigma):
    return rng.lognormvariate(math.log(median), sigma)


def pick_partner(rng, item_type):
    if item_type == "compounded":
        return "Cedarwick Compounding"
    if item_type == "rx_diet":
        return "Tamsin Pet Nutrition"
    return "Norvale Pharmacy" if rng.random() < 0.6 else "Harborlight Rx"


def random_placed_at(rng):
    """Order times cluster in daytime hours across the 30 day window."""
    day = rng.randrange(WINDOW_DAYS)
    base = (NOW - timedelta(days=WINDOW_DAYS)).replace(hour=0, minute=0)
    hour = min(22, max(6, int(rng.gauss(14, 3.5))))
    ts = base + timedelta(days=day + 1, hours=hour, minutes=rng.randrange(60))
    return min(ts, NOW - timedelta(minutes=rng.randrange(5, 240)))


def simulate_order(rng, n):
    """Return the full ground-truth timeline for one order."""
    while True:
        clinic, slow = rng.choice(CLINICS)
        placed = random_placed_at(rng)
        # A closed clinic takes no new one-time orders after it closes.
        if not (clinic == CLOSED_CLINIC and placed >= CLOSED_ON):
            break
    item, item_type, urgency = rng.choice(ITEMS)
    partner = pick_partner(rng, item_type)
    p = PARTNERS[partner]

    # 1. Vet approval
    approval_h = lognormal(rng, 5, 0.8) * (2 if slow else 1)
    if rng.random() < (0.15 if slow else 0.05):
        approval_h += rng.uniform(48, 160)
    approved = placed + timedelta(hours=approval_h)

    # 2. Stock check
    stock_status, stock_issue, notified, cancelled = "in_stock", None, None, None
    restock_h = 0.0
    if rng.random() < p["oos_p"]:
        stock_status = "out_of_stock"
        stock_issue = approved + timedelta(hours=rng.uniform(1, 6))
        restock_h = rng.uniform(96, 300)
        if rng.random() < 0.25:
            notified = stock_issue + timedelta(hours=rng.uniform(2, 20))
        if rng.random() < 0.3:
            cancelled = stock_issue + timedelta(hours=rng.uniform(72, 160))

    # 3. Fulfillment: approval to shipping label
    fulfil_h = lognormal(rng, p["median_h"], p["sigma"]) + restock_h
    if rng.random() < p["backlog_p"]:
        fulfil_h += rng.uniform(*p["backlog_h"])
    label = approved + timedelta(hours=fulfil_h)

    # 4. Carrier pickup: label to first scan
    pickup_h = lognormal(rng, 5, 0.7)
    if rng.random() < p["missed_pickup_p"]:
        pickup_h += rng.uniform(30, 110)
    first_scan = label + timedelta(hours=pickup_h)

    # 5. Transit
    transit_h = rng.uniform(24, 72)
    lost = rng.random() < 0.015
    lost_cancel = None
    stall_start = stall_end = None
    if lost:
        stall_start = first_scan + timedelta(hours=rng.uniform(12, 48))
        stall_end = None
        delivered = None
        # A lost package is eventually declared lost and refunded.
        lost_cancel = stall_start + timedelta(hours=rng.uniform(120, 260))
    else:
        if rng.random() < 0.07:
            stall_start = first_scan + timedelta(hours=rng.uniform(12, 36))
            stall_end = stall_start + timedelta(hours=rng.uniform(72, 168))
            transit_h += (stall_end - stall_start).total_seconds() / 3600
        delivered = first_scan + timedelta(hours=transit_h)

    if cancelled and cancelled < label:
        label = first_scan = delivered = None
        stall_start = stall_end = None
    else:
        cancelled = lost_cancel

    promise_days = 8 if item_type == "compounded" else 5
    promised_by = (placed + timedelta(days=promise_days)).date()

    return dict(
        order_id=f"RX-{48000 + n}", placed_at=placed, clinic=clinic,
        pet_parent=rng.choice(PEOPLE), pet_name=rng.choice(PETS),
        species=rng.choice(["Dog", "Dog", "Dog", "Cat", "Cat"]),
        item=item, item_type=item_type, urgency=urgency, partner=partner,
        is_autoship=rng.random() < 0.3, promised_by=promised_by,
        approved_at=approved, stock_status=stock_status,
        stock_issue_at=stock_issue, customer_notified_at=notified,
        label_created_at=label, first_scan_at=first_scan,
        delivered_at=delivered, cancelled_at=cancelled,
        _stall_start=stall_start, _stall_end=stall_end, _lost=lost,
    )


def last_scan_before(rng, o, now):
    """Latest carrier scan visible at snapshot time."""
    first = o["first_scan_at"]
    if first is None or first > now:
        return None
    end = min(now, o["delivered_at"] or now)
    scans, t = [first], first
    while True:
        t = t + timedelta(hours=rng.uniform(8, 22))
        if t > end:
            break
        s0, s1 = o["_stall_start"], o["_stall_end"]
        if s0 and t >= s0 and (s1 is None or t < s1):
            continue  # package is stalled: no scans
        scans.append(t)
    if o["delivered_at"] and o["delivered_at"] <= now:
        scans.append(o["delivered_at"])
    return max(scans)


def censor(rng, o, now):
    """Hide everything that has not happened yet at snapshot time."""
    row = dict(o)
    row["last_scan_at"] = last_scan_before(rng, o, now)
    chain = ["approved_at", "label_created_at", "first_scan_at", "delivered_at"]
    hidden = False
    for key in chain:
        if hidden or row[key] is None or row[key] > now:
            row[key] = None
            hidden = True
    for key in ["stock_issue_at", "customer_notified_at", "cancelled_at"]:
        if row[key] is not None and row[key] > now:
            row[key] = None
    if row["approved_at"] is None or row["stock_issue_at"] is None:
        # Stock problems only surface after approval.
        if row["stock_status"] == "out_of_stock" and row["stock_issue_at"] is None:
            row["stock_status"] = "in_stock"
    if row["label_created_at"] is not None:
        row["stock_status"] = "in_stock"  # restocked and shipped

    # Support contacts ("where is my order") grow with visible delay.
    contacts = 1 if rng.random() < 0.03 else 0
    end = row["delivered_at"] or row["cancelled_at"] or now
    days_late = (end.date() - row["promised_by"]).days
    if days_late > 0:
        contacts += rng.choice([0, 1, 1, 2])
    elif (end - row["placed_at"]).total_seconds() / 3600 > 72 and rng.random() < 0.2:
        contacts += 1
    row["support_contacts"] = contacts
    return {k: v for k, v in row.items() if not k.startswith("_")}


def make_subscriptions(rng, now):
    rows = []
    for n in range(N_SUBSCRIPTIONS):
        clinic, _ = rng.choice(CLINICS)
        item, item_type, urgency = rng.choice(ITEMS)
        next_ship = (now + timedelta(days=rng.randrange(0, 28))).date()
        refills = rng.choice([0, 1, 1, 2, 2, 3, 3, 4, 5, 6])
        changed, verified = None, True
        if rng.random() < 0.2:
            changed = now - timedelta(days=rng.uniform(1, 20))
            verified = rng.random() < 0.3
        rows.append(dict(
            subscription_id=f"SUB-{7100 + n}", clinic=clinic,
            clinic_status="closed" if clinic == CLOSED_CLINIC else "active",
            pet_parent=rng.choice(PEOPLE), pet_name=rng.choice(PETS),
            item=item, urgency=urgency, next_ship_date=next_ship,
            refills_remaining=refills, address_changed_at=changed,
            address_verified=verified,
        ))
    return rows


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    return str(v)


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: fmt(v) for k, v in r.items()})


def main():
    rng = random.Random(SEED)
    orders = [censor(rng, simulate_order(rng, n), NOW) for n in range(N_ORDERS)]
    orders.sort(key=lambda r: r["placed_at"])
    subs = make_subscriptions(rng, NOW)
    write_csv(DATA_DIR / "orders.csv", orders)
    write_csv(DATA_DIR / "subscriptions.csv", subs)
    (DATA_DIR / "snapshot_time.txt").write_text(NOW.strftime("%Y-%m-%d %H:%M") + "\n", encoding="utf-8")
    print(f"Wrote {len(orders)} orders and {len(subs)} subscriptions to {DATA_DIR}")


if __name__ == "__main__":
    main()
