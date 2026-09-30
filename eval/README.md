# el-jev evaluation harness

This harness answers one narrow question: **when is it safe for `el-jev` to act instead of
abstaining?** It does not turn a Cohere score into a confidence value by assumption.

## Why raw scores cannot gate

The live 50-candidate call produced top scores of `0.92933786`, `0.92888767`, and a top-two
margin of only `0.00045019`. The lowest score was `0.59793660`, so a `0.60` floor would accept
almost everything. Exact score ties also occurred. `relevance_score` is an absolute,
per-document relevance score. It is not a probability, distribution, or ratio scale.

The harness therefore fits the contract's pointwise temperature transform independently to the
top two scores:

```text
p(s) = sigmoid(logit(clip(s)) / T)
```

It then sweeps both `threshold` and `margin_threshold`, choosing the highest-coverage pair whose
one-sided risk bound is within budget. The pair is evaluated once on a locked held-out split.
Exact top-score ties always abstain. `top_k` is retained only as score-count metadata; it is not
part of the transform.

## Statistical vocabulary

- **Selective risk** is the error rate among decisions the service acts on. The target is at
  most 2%.
- **Coverage** is the fraction of all held-out decisions that the service acts on.
- The reported safety gate is a one-sided 95% **upper confidence bound on risk**. Equivalently,
  the report shows the one-sided lower confidence bound on selective accuracy. A point estimate
  below 2% is not enough.
- Empty `acceptable_choices` means `none`; a label can contain several acceptable candidate ids.

With a 2% risk budget and a one-sided 95% bound, zero observed held-out errors require roughly
149 acted-on held-out decisions. One error requires materially more. Run the power calculation
before collecting labels.

## Privacy boundary

Raw corpus and label JSONL files belong outside this repository, by default:

```text
%USERPROFILE%\.eljev\corpus\
```

`capture.py` applies redaction once at entry and stores only the redacted replay payload. It
refuses to write real data below `eval/corpus/`. Do not copy captures into the repository.
The root `.gitignore` must exclude `eval/corpus/*` except `.gitkeep`, generated report files,
and `eval/calibration.json`; see `eval/corpus/README.md`. The root ignore file is owned by
another agent and is intentionally not modified here.

## End-to-end command sequence

Run from `<path-to-el-jev>` with Python 3.13. The shipped tooling uses only the
standard library.

### 1. Capture replayable records

Prepare a transient raw JSONL stream containing `criterion`, `candidates`, the current `choice`,
the deterministic trigger, `task_wall_ms`, optional `turn_count`/`token_counts`, tier data, and
the complete ranked `model_results`. Then capture it outside the repository:

```powershell
python eval\scripts\capture.py `
  --input C:\path\to\raw-decisions.jsonl `
  --output $env:USERPROFILE\.eljev\corpus\decisions.jsonl
```

Each stored record contains the criterion, redacted candidate payload, candidate ids, N,
post-redaction lengths, baseline choice, trigger, task timing, optional token/turn counts, and
the scores required for replay and calibration. The stored redacted payload is the only payload
later tiers and labellers see.

### 2. Calculate label power before labelling

```powershell
python eval\scripts\power.py `
  --risk-budget 0.02 `
  --confidence 0.95 `
  --development-fraction 0.70 `
  --expected-coverage 0.50
```

The output tells you the minimum acted-on held-out decisions and the larger raw corpus needed
when expected coverage is below 100%. Pass `--corpus`, `--labels`, `--available-labels`, or
`--available-heldout` later to receive explicit insufficiency warnings.

### 3. Blind-label the corpus

Interactive labelling randomizes candidates and never prints model scores, the baseline choice,
or baseline ranking:

```powershell
python eval\scripts\label.py `
  --corpus $env:USERPROFILE\.eljev\corpus\decisions.jsonl `
  --output $env:USERPROFILE\.eljev\corpus\labels.jsonl `
  --labeler-id reviewer-1
```

Enter `none`, one id, or comma-separated ids. To retest a subset without losing the primary
labels:

```powershell
python eval\scripts\label.py `
  --corpus $env:USERPROFILE\.eljev\corpus\decisions.jsonl `
  --relabel-from $env:USERPROFILE\.eljev\corpus\labels.jsonl `
  --relabel-count 30 `
  --output $env:USERPROFILE\.eljev\corpus\labels.jsonl `
  --labeler-id reviewer-1-retest
```

Retests are marked separately. The report calculates exact set agreement and mean set Jaccard.
Labels are deliberately separate from the corpus so the same captured decisions can be
re-labelled later.

### 4. Freeze the split

```powershell
python eval\scripts\split.py `
  --corpus $env:USERPROFILE\.eljev\corpus\decisions.jsonl `
  --labels $env:USERPROFILE\.eljev\corpus\labels.jsonl `
  --heldout-fraction 0.30 `
  --seed jev-s4 `
  --output $env:USERPROFILE\.eljev\corpus\split.json
```

The manifest hashes the corpus and labels and stores disjoint development and held-out ids.
Changing either input invalidates the manifest.

### 5. Fit calibration on development only

```powershell
python eval\scripts\calibrate.py `
  --corpus $env:USERPROFILE\.eljev\corpus\decisions.jsonl `
  --labels $env:USERPROFILE\.eljev\corpus\labels.jsonl `
  --split-manifest $env:USERPROFILE\.eljev\corpus\split.json `
  --risk-budget 0.02 `
  --confidence 0.95 `
  --top-k 50 `
  --output eval\calibration.json
```

The fitter accepts development records only, checks all input hashes, rejects an empty
development split, and marks the held-out split as locked in the output. It emits both
`threshold` and `margin_threshold`. Never select either threshold after inspecting held-out
outcomes.

### 6. Evaluate the locked held-out curve

```powershell
python eval\scripts\coverage_risk.py `
  --corpus $env:USERPROFILE\.eljev\corpus\decisions.jsonl `
  --labels $env:USERPROFILE\.eljev\corpus\labels.jsonl `
  --split-manifest $env:USERPROFILE\.eljev\corpus\split.json `
  --calibration eval\calibration.json `
  --minimum-coverage 0.10 `
  --output $env:USERPROFILE\.eljev\corpus\coverage-risk.json
```

The curve evaluates the two-dimensional surface of distinct calibrated top1 probabilities and
top1-minus-top2 margins. The chosen operating point maximizes coverage subject to the one-sided
risk upper bound. The tool prints a loud warning when coverage is below the configured usability
floor. That is a legitimate finding; do not loosen the 2% risk budget to hide it.

### 7. Generate the report

```powershell
python eval\scripts\report.py `
  --corpus $env:USERPROFILE\.eljev\corpus\decisions.jsonl `
  --labels $env:USERPROFILE\.eljev\corpus\labels.jsonl `
  --split-manifest $env:USERPROFILE\.eljev\corpus\split.json `
  --calibration eval\calibration.json `
  --coverage-risk $env:USERPROFILE\.eljev\corpus\coverage-risk.json `
  --output eval\reports\jev-evaluation.md
```

The report includes provenance, split sizes, test–retest agreement, the complete curve, the
chosen threshold and confidence bound, per-tier results, measured latency context, and explicit
PASS/FAIL lines. Task-level latency remains a separate S1/S7 paired measurement.

## Interpreting a bad result

- **Risk FAIL:** do not enable `ELJEV_COVERAGE_POLICY=calibrated`. Increase labels, inspect
  disagreements, improve the decision input, or accept abstention.
- **Coverage unusably low:** the scores do not separate safe from unsafe choices at the requested
  risk across either dimension. That is evidence against the current gate or model, not permission
  to borrow a threshold.
- **No usable development rows:** the corpus lacks non-tied model rankings or labels. Capture
  complete scores and label more decisions.
- **Low test–retest agreement:** the task criterion or candidate payload is underspecified. Fix
  the labelling rubric before fitting a gate.
- **Latency criterion not assessed:** run the paired S1/S7 task experiment. A safe gate alone
  does not prove that the overall project improves the real task.

## Synthetic validation

The harness was validated with an external synthetic corpus containing 50-candidate, bunched
scores and an independent answer key. The same command sequence was run with simulated labels.
The validation also supplied a tampered manifest with no development ids; `calibrate.py` rejected
it instead of fitting on the held-out rows. On the deliberately difficult bunched fixture,
including exact ties, no operating pair reached the configured 10% coverage floor at the 2%
risk budget. The report marks that result as FAIL rather than loosening the budget.
