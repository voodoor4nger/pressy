# Pressy scoring system

This document is the canonical specification for how Pressy turns
extracted events into per-category and composite scores. Code in
`src/score.py` implements this spec. If the two ever disagree, the
spec is authoritative — adjust the code (or amend the spec and bump
the version in the changelog).

## Goals

- Turn a stream of structured events into stable per-category scores
  that move slowly enough to feel calibrated but fast enough to
  reflect the news.
- Be transparent: every score should be auditable back to the events
  that produced it.
- Prefer simple, defensible math over clever optimization.

## Categories

Ten categories, each scored 0-100:

economy, jobs, housing, health, education, science, international,
constitutional, moral, institutional.

Per-category baselines, band sizes, and weights live in
`config/baselines.yaml`. Baselines are the analyst's best read of
where each category sits at the start of the term; band sizes set how
far an active news cycle can move the score before further events
stop counting.

## Per-event weighted impact

Each event contributes a signed numeric weight to each of its
categories:

```
event_weight = magnitude_impact * direction_sign * confidence_weight * time_decay
```

Returns 0 (no contribution) if any of:
- `is_relevant` is false
- `impact_direction` is "neutral"
- the event is older than the hard cutoff (730 days)

### Magnitude impact

Each magnitude maps to a Fibonacci-like impact:

| Magnitude | Impact points |
|-----------|---------------|
| 1 | ±1 |
| 2 | ±2 |
| 3 | ±3 |
| 4 | ±5 |
| 5 | ±8 |

This follows a Fibonacci-like scale (1, 2, 3, 5, 8). Rationale: minor
events accumulate naturally without drowning each other out, while
major events still carry significantly more weight (a magnitude 5
event equals roughly 8 magnitude 1 events). This is gentler than
exponential scaling which can let single events pin a category at the
band ceiling, but firmer than linear scaling which lets routine news
bury major events. The Fibonacci sequence is widely used in agile
estimation precisely because humans find it intuitive for "this is
bigger than that, but not infinitely bigger."

### Direction sign

| Direction | Sign |
|-----------|------|
| positive  | +1 |
| negative  | -1 |
| neutral   |  0 (event contributes nothing) |

### Confidence weight

| Confidence | Weight |
|------------|--------|
| high   | 1.0 |
| medium | 0.7 |
| low    | 0.4 |

Low-confidence events (opinion / editorial / op-ed per the prompt
rules) still count, but at reduced weight.

### Time decay

Exponential decay with a 90-day half-life. An event 90 days old
contributes half as much as one today; 180 days old contributes a
quarter; etc. Hard zero past 730 days.

```
time_decay = 0.5 ** (age_days / 90)   if age_days <= 730
           = 0
```

## Multi-category attribution

When an event has multiple categories, the FULL weighted impact is
applied to each. We do not divide. An event that affects both
`economy` and `international` moves both scores by its full weight.

Rationale: a major tariff genuinely affects both the economy and US
international standing; halving the weight understates each.

## Per-category score

```
sum_of_weights         = sum of event_weight for all events tagged with this category
post_k_deviation       = sum_of_weights * k_factor
clamped_deviation      = clamp(post_k_deviation, -band_size, +band_size)
score                  = clamp(baseline + clamped_deviation, 0, 100)
```

The `band_size` is per-category and reflects how much room the
analyst thinks the category can move from baseline within a single
news cycle. Smaller bands mean the category is harder to move; larger
bands mean the category is volatile.

The `raw_deviation` (pre-clamp) is preserved for diagnostics — if it
exceeds `band_size`, the category is "pinned" at its band, which is a
signal that either the band is too narrow or the baseline is wrong.

## K-factor (term-stage decay)

```
months_in_term  = (as_of_date - term_start_date) / 30.4 days
k_factor        = max(K_FACTOR_FLOOR,
                      1.0 - (months_in_term / 24) * (1.0 - K_FACTOR_FLOOR))
```

Linear from 1.0 at month 0 to `K_FACTOR_FLOOR` (0.5) at month 24,
flat at the floor afterward.

Rationale: early in a term, fewer events have accumulated and each
new event carries proportionally more signal. Late in a term, the
record is thicker and individual events should move scores less.
This prevents an out-of-the-gate flurry of events from over-anchoring
a long-term assessment.

## Outlook

Per-category direction-of-travel signal. Compares recent (last 30
days) weighted impact to historical (days 31-90) weighted impact. The
threshold is in raw weighted-impact units before k-factor — k-factor
cancels in the difference, but the function accepts it for API
symmetry.

```
recent_sum     = sum of event_weight for events 0-30 days old
historical_sum = sum of event_weight for events 31-90 days old
delta          = recent_sum - historical_sum

if abs(delta) < OUTLOOK_THRESHOLD: outlook = "stable"
elif delta > 0:                   outlook = "positive"
else:                             outlook = "negative"
```

Note: outlook is independent of the cumulative score. A category at a
healthy score with a deteriorating recent stretch will show a
negative outlook.

## Composite

The composite is a weighted average of per-category scores:

```
composite = sum(score[c] * weight[c] for c in categories) / sum(weights)
```

Weights default to 1.0 in the config; the analyst can adjust. A
weight of 0 effectively excludes a category.

The composite outlook is the simple majority of non-stable
per-category outlooks: positive if more positives than negatives,
negative if more negatives than positives, stable otherwise.

## Audit trail

Every score must be reproducible from the events that produced it.
The `compute_scores()` result includes a per-category list of
`(event_id, weighted_impact)` tuples so a downstream view (or a
human) can trace any deviation back to specific articles.

## Parameter summary

| Parameter              | Value     | Where it lives        |
|------------------------|-----------|-----------------------|
| Magnitude impacts      | 1, 2, 3, 5, 8 (Fibonacci-like) | `MAGNITUDE_IMPACTS` |
| Confidence weights     | high=1.0, medium=0.7, low=0.4  | `CONFIDENCE_WEIGHTS` |
| Direction signs        | pos=+1, neg=-1, neutral=0      | `DIRECTION_SIGNS` |
| Time-decay half-life   | 90 days                        | `HALF_LIFE_DAYS` |
| Hard cutoff            | 730 days                       | `HARD_CUTOFF_DAYS` |
| K-factor floor         | 0.5                            | `K_FACTOR_FLOOR` |
| K-factor decay window  | 24 months                      | `K_FACTOR_DECAY_MONTHS` |
| Outlook threshold      | 2                              | `OUTLOOK_THRESHOLD` |
| Per-category baselines | see `config/baselines.yaml`    | config |
| Per-category bands     | see `config/baselines.yaml`    | config |
| Per-category weights   | see `config/baselines.yaml`    | config |
| Term start date        | see `config/baselines.yaml`    | config |

## Changelog

- **v1**: initial scoring spec. Per-event weighted impact with
  magnitude impacts on an exponential scale (1, 2, 4, 8, 16),
  90-day decay, 730-day cutoff, per-category band clamping, k-factor
  term decay, recent-vs-historical outlook.
- **v1.1**: changed magnitude impact scaling from exponential
  (1,2,4,8,16) to Fibonacci-like (1,2,3,5,8) per analyst preference
  for less aggressive scaling.
