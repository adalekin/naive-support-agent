# Evaluation

Cases of a made-up product, Pixmill, each modelled on a real support case: the request, what the
sources answer, and what a good answer must and must not say. `instructions/` is Pixmill's
instruction set. An installation keeps its own set of real cases next to its private instructions.

Each case is graded two ways:

- **By code**, where code can tell: an answer came, its kind, whether a responsible person was
  called, words that must not appear, and every email of the answer under a spoiler.
- **By a grader model**, against the case's `must` and `must_not` lists. The grader quotes the words
  of the answer each verdict rests on.

A case passes when every check and every rubric item passes. The report in `reports/` shows each
case, each requirement of the specs the cases cover, and what the run cost.

```bash
docker compose up -d postgres
cd server
uv run python ../evals/run.py --check      # reads the cases, calls nothing
uv run python ../evals/run.py              # every case, against the real model
uv run python ../evals/run.py --case expired-credits
```

A run calls the model and costs money. `ANTHROPIC_API_KEY` has to be set.
