# Corpus boundary

This directory intentionally contains no corpus data. Real captures, labels, split manifests,
and replay payloads belong outside the repository, by default under `~/.eljev/corpus`.

Use `eval/scripts/capture.py` for entry redaction. It refuses to write real data under this
directory. Redaction is applied once at capture entry, and the stored redacted candidate payload
is the replay payload seen by every later tier and the labelling flow.

The repository `.gitignore` must exclude at least:

```text
eval/corpus/*
!eval/corpus/.gitkeep
eval/reports/*
!eval/reports/.gitkeep
eval/calibration.json
```

The root `.gitignore` is owned by another agent; this harness does not modify it.
