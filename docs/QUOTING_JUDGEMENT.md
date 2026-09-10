# The estimator's judgement

Source: a conversation between EDM Zone and **Paul**, the shop's experienced
estimator, supplied 10 September 2026 as `Document1.docx`.

Its stated purpose is blunt, and worth repeating at the top of this file:
capture the judgement before it retires. Forty-odd years of instinct currently
lives in one head, and the intention is that Paul's son can eventually operate
the same reasoning without having lived it.

The organising idea is *The Checklist Manifesto*: **the checklist does not
replace expertise, it makes sure the expertise gets applied** — on the day
somebody is busy, or new, or looking at a drawing that flatters to deceive.
The comparison Paul drew is an online insurance quotation: you cannot reach
the price until the questions have been answered.

Encoded in `backend/app/judgement.py`, tested in
`backend/tests/test_judgement.py`. Everything below that is a number or a rule
is in the code; everything that is reasoning is here, because the reasoning is
what makes the number defensible later.

---

## 1. Settled: the standard rate is £65/hour

This document uses **£60/hour** throughout — *"the computer should not simply
say machine hours × £60"*. The business supplied **£65/hour** on 7 September
with the instruction to keep every process at that rate.

Put to EDM Zone on 10 September, the answer was: **£65 stands. Paul's £60 is
out of date.**

So nothing in the app changes. The seeded rate is £65 on every process, and
the three trial quote sheets priced at £65 are unaffected.

What this does change is how the long-runner ladder is built. It is derived
from whatever the standard rate happens to be, in Paul's own £2.50 steps,
rather than from a hard-coded £60:

| | Rung 1 | Rung 2 | Rung 3 | Rung 4 | Standard |
|---|---|---|---|---|---|
| Paul's table, at £60 | £50.00 | £52.50 | £55.00 | £57.50 | £60.00 |
| **In use, at £65** | **£55.00** | **£57.50** | **£60.00** | **£62.50** | **£65.00** |

The reasoning is untouched — it is the *shape* of the concession that is
Paul's, not the particular figures. Worth noting when the ladder is next
discussed with him: the floor he described as competitive, £55, is now the
first rung down rather than the middle one, so a job he would have taken at
£55 sits one step further from the standard rate than it used to.

---

## 2. The long-runner adjustment

Paul's rule, close to verbatim:

> Sometimes I alter my price if it's a long runner. If it's a simple setup, or
> relatively simple, or there's a large quantity, and the cut time is over 10
> hours, I'll often work out the price at 50, 52.50, 55, and 57.50 an hour,
> not just straightforward 60 an hour all the time. It becomes more
> competitive if you can charge, say, 55 an hour. Free cutting time, really
> speaking, we can reduce the rate.

### The reasoning, which matters more than the threshold

**Expensive operator and setup time and relatively "free" machine-running time
do not deserve the same hourly rate.** A machine cutting unattended overnight
is not consuming the thing the hourly rate is really paying for.

This is the part to teach. *"Over 10 hours = £55/hour"* is exactly the rule
Paul did **not** want written down, because a rule like that gets applied to
a ten-hour job that needs watching every twenty minutes.

### How it is built

- Trigger: **10 or more cutting hours**, inclusive.
- The ladder is the standard rate and four rungs below it in **£2.50 steps**.
  At £60 that is £50.00 / £52.50 / £55.00 / £57.50 / £60.00 — Paul's table
  exactly. At £65 it becomes £55.00 / £57.50 / £60.00 / £62.50 / £65.00.
- **All five are shown. The system does not pick one.** Paul asked for exactly
  that: see the price at every rate, then choose, and be able to say why. A
  function that chose would throw away the judgement being captured.

### Gates before a reduction is defensible

All of these, and none is automatic:

- Is the setup straightforward?
- Can the machine run unattended?
- Is there little operator intervention?
- Is it a large batch?
- Is there low scrap and low risk?
- Can it run overnight or at weekends?

---

## 3. The eight-stage checklist

Full text in `judgement.py`. The ordering is not cosmetic: **workholding and
distortion are asked before the time calculation**, because both change the
time, and a checklist that asks them afterwards is only recording regret.

| # | Stage | The point of it |
|---|-------|-----------------|
| 1 | Basic job information | Reference, drawing **and revision**, quantity, process, who supplies material, repeat job |
| 2 | Material | Identified, and whether it cuts like steel |
| 3 | Geometry and machine capability | Height, length, taper, wire, starter hole, travels, weight, machine choice |
| 4 | Tolerance and finish | Dimensional, geometric, finish, **skim count**, inspection method |
| 5 | Workholding | *How are we actually going to hold this?* |
| 6 | Movement and distortion | Stress release, thin walls, springing, sequence, tags |
| 7 | Time calculation | Programming + setup + cutting + skims + handling + inspection + electrodes + contingency |
| 8 | Commercial sense-check | One-off or production, handling per part, free-issue risk, specialist value, repeat potential, history, minimum order, *does this feel right?* |

Seventeen questions are mandatory out of forty-six. Deliberately a minority:
make everything compulsory and people tick blindly. An explicit **N/A counts
as answered** — the mechanism is making somebody say it out loud, not letting
them skip past.

### The two Paul singled out

**"How are we actually going to hold this?"** — his words: *"exactly the sort
of thing an inexperienced estimator can overlook while concentrating on the
drawing."* Mandatory.

**"Does this price actually feel commercially right?"** — the last gate, and
the one the arithmetic cannot answer. Mandatory.

---

## 4. Materials that do not cut like steel

Paul named phosphor bronze, titanium and tungsten alloy. The system should say
so in plain capitals at the point of quoting:

> PHOSPHOR BRONZE — allow for slower cutting than standard steel.

Multipliers are in `MATERIAL_DIFFICULTY`. **They are first estimates and are
marked as such** — the words are Paul's, the numbers are not yet anybody's.
They want replacing with real observed figures the same way the wire speed
table came from the real spreadsheet. Until then they are a prompt to think,
not a calculation to trust.

---

## 5. The learning loop

The field Paul asked for, and the most valuable thing in the document:

> **"Paul says price should be £_____."**
> **Reason: __________________**

Every time the calculated answer and the experienced answer differ, record the
difference **and why**. That is how instinct becomes rules.

The example he raised unprompted: **50 COMET keyways at £16 each** — a price
the machine-time calculation would not have arrived at. It is recorded in
`BENCHMARKS`, and it is recorded together with the admission that *the
reasoning behind it has not been captured yet*. A remembered number with no
reason attached is precisely what this system is built to distrust, so it is
stored wearing that label rather than quietly used as a precedent.

**This is the highest-value thing to build next.** Twenty to thirty real
enquiries with the delta and the reason recorded is worth more than any
further feature.

---

## 6. Output shape Paul asked for

Not a single number:

- **Recommended price**
- **Suggested selling range**, low to high
- **Confidence: high / medium / low**
- **Three to five job-specific warnings** — "things to bear in mind"
- **Questions to ask the customer before quoting** — anything missing

The system already produces the warnings and the missing-information list. The
range, the confidence band and the recommendation with a reason are not built.

---

## 7. How Paul said to build it

> Real jobs first, software second.

Use the next genuine enquiry, run it through the checklist, and let him say
where the thinking is right or wrong. After twenty to thirty of those there is
something worth turning into software.

That is the same method already agreed for scoring extraction accuracy against
previously quoted drawings — one exercise, not two.
