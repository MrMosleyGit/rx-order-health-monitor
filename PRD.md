# PRD: Rx Order Health Monitor

| | |
| --- | --- |
| Status | Prototype built on synthetic data |
| Product area | Prescription home delivery |
| Primary users | Support and operations team |

## 1. Problem

A prescription home delivery order depends on three handoffs: the vet clinic approves it, a pharmacy partner fills it, and a carrier delivers it. When an order stalls at one of those handoffs, nobody is told. The team learns about it when the pet parent calls, and by then the pet may already be out of medication.

The same failure patterns show up repeatedly in public reviews of veterinary home delivery services:

1. Approved orders that do not ship for days
2. Stockouts the customer finds out about late
3. A "shipped" status that only means a label was printed
4. Autoship orders that run on stale data, such as a closed clinic or an old address
5. No proactive communication

Each one costs a support contact, sometimes a refund, and trust with both the pet parent and the clinic that recommended the service.

## 2. Goal

Find at-risk orders before the customer does, and make the first contact ours.

**Non-goals for this version**

- Fixing fulfillment speed itself. This makes delays visible and routes them to an owner.
- Sending messages automatically. Drafts are reviewed by a person first.
- Predicting delays with a model. Simple stage clocks come first, because they are easy to explain and tune.

## 3. Users

| User | What they need |
| --- | --- |
| Support agent | A ranked list of orders to fix today, with the message already drafted |
| Operations lead | Exceptions grouped by root cause and partner, to fix the process and not single orders |
| Clinic staff | To know which of their pending approvals are holding up a client's order |
| Pet parent | To hear about a delay the day it happens, not when the medication runs out |

## 4. User stories

- As a support agent, I want a daily list of at-risk orders ranked by medical urgency, so I can fix them before the customer calls.
- As a pet parent, I want to hear about a delay or stock issue the day it happens, so my pet does not run out of medication.
- As a clinic manager, I want to see which approvals are holding up my clients' orders, so my team can clear them quickly.
- As an operations lead, I want exceptions grouped by root cause and partner, so I can fix the process and not single orders.
- As a pet parent on autoship, I want to be asked before a shipment goes out when something about my order has changed, so I am not charged for a delivery I cannot use.

## 5. How it works

Every open order is in exactly one stage. Each stage has a clock and a threshold.

| Stage | Clock starts | At risk when | Owner | Automated action |
| --- | --- | --- | --- | --- |
| Awaiting vet approval | Order placed | No approval after 24 business hours | Clinic | Remind the clinic. Tell the pet parent the order is waiting on their vet. |
| Item unavailable | Stock check fails | Immediately | Support | Tell the pet parent the same day. Ask the clinic about a substitute. |
| Approved, awaiting fulfillment | Vet approves | No label after 24 business hours (72 for compounded) | Pharmacy partner | Escalate to the partner. Update the pet parent. |
| Label created | Label printed | No carrier scan after 24 hours | Pharmacy partner | Show Preparing shipment, not Shipped. Confirm handoff to the carrier. |
| In transit | First carrier scan | Past the promised date, or no scan for 48 hours | Carrier or support | Proactive delay notice. Offer a replacement for critical medications. |
| Autoship | 3 days before ship date | Clinic inactive, no refills, or unverified address change | Support | Pause and confirm before charging. |

**Ranking.** Critical medications first, then longest time over threshold. A medication is critical when missing doses puts the pet at risk, such as insulin, thyroid, heart and asthma medications.

**Root cause.** An order that is late in transit is not always the carrier's fault. If it left the pharmacy on or after the promised date, the root cause is tagged "Shipped too late to meet promise" and routed to support, so the upstream delay gets reviewed and the carrier is not blamed for it.

## 6. Acceptance criteria

Every criterion except the last is covered by a test in `tests/test_monitor.py`. The last one is checked by opening the dashboard.

- [x] Every open order has one current stage and a time in stage
- [x] An order past its stage threshold appears in the at-risk queue
- [x] Weekend hours do not count against the approval and fulfillment clocks
- [x] Compounded items get a longer fulfillment threshold
- [x] A stockout is flagged immediately, with no waiting period
- [x] An order shows as Shipped only after a real carrier scan
- [x] Critical medications sort ahead of standard items
- [x] Every flagged order has a root cause, an owner, a recommended action and a drafted message
- [x] Delivered and cancelled orders never appear in the queue
- [x] A subscription with a closed clinic, no refills or an unverified address change is held before it ships
- [x] The dashboard shows exceptions by stage, root cause and partner

Not built yet: the queue refreshing within 15 minutes of a threshold being crossed. The prototype runs on a snapshot file. A production version would run on a schedule or on order events.

## 7. Success metrics

| Metric | Why it matters | Sample data baseline |
| --- | --- | --- |
| Approval to ship time, median and 90th percentile | The slow tail is where trust is lost | 0.8 days and 4.5 days |
| On-time delivery rate against the promised date | The promise the customer was given | 77.3% |
| "Where is my order" contacts per 1,000 orders | Direct support cost of silence | 334 |
| Share of exceptions where we contacted the customer first | The goal of this product | Not measured yet |
| Autoship errors per 1,000 shipments | Charges the customer did not want | Not measured yet |

The baselines come from synthetic data. They show how the metrics would be reported and are not real performance numbers.

## 8. Rollout plan

1. **Shadow mode.** Run the monitor next to the current process for two weeks. Compare what it flags against the tickets that actually came in. Tune thresholds.
2. **Beta with a few support agents.** Agents work the queue and send the drafted messages after review. Collect their feedback on false alarms and message wording.
3. **Wider release.** Add the clinic reminder and the weekly root cause review with pharmacy partners.

## 9. Risks and open questions

- **Alert fatigue.** Thresholds that are too tight bury the real problems. Shadow mode is there to catch this before agents see it.
- **Data freshness.** If clinic and patient data sync on a delay, new patients may show as stalled approvals when the clinic cannot act yet. Does the approval clock need to start at sync time?
- **Carrier data quality.** Scan gaps are normal on some routes. Should the stall threshold vary by carrier and service level?
- **Message tone.** A proactive delay notice can read as an apology or as an alarm. This needs testing with real pet parents.
- **Who owns the fix?** An alert with no clear owner does not get worked. The owner field is a first pass and needs agreement from each team.
