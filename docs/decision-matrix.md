# Decision matrix and audit trail

Ratings are on a 1–5 scale, where 5 is better for the project. The selected
resolution is marked **chosen**. These are design records, not model-generated
publication decisions.

## Acquisition

| Resolution | Privacy | Reliability | Maintenance | Auditability | Cost | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Manual Takeout request | 5 | 4 | 5 | 5 | 4 | **chosen** |
| Automated downloader | 2 | 2 | 1 | 2 | 3 | rejected: brittle credentials and larger privacy surface |
| Chrome/DevTools automation | 3 | 3 | 2 | 3 | 2 | rejected: browser state is difficult to reproduce |

Manual acquisition keeps account access outside the repository and leaves dated
source boundaries visible. The safe copy command is explicit and defaults to a
dry run.

## Model authentication

| Resolution | OAuth safety | Exact model control | Local ergonomics | Docker support | Auditability | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Direct API key/token cache | 2 | 5 | 3 | 4 | 2 | rejected: credential handling is out of scope |
| Codex SDK/app-server | 5 | 5 | 5 | 4 | 5 | **chosen** |
| GPT Actions OAuth | 3 | 1 | 1 | 2 | 2 | rejected: authenticates an action API, not local model work |

The chosen provider asks Codex to own ChatGPT OAuth and launches read-only,
ephemeral turns. The pipeline never reads `~/.codex/auth` or converts its
contents into a direct API request.

## Runtime topology

| Resolution | Host simplicity | Container isolation | Operational parity | Failure visibility | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Host-only Airflow | 5 | 1 | 2 | 4 | rejected: does not match the existing Compose deployment |
| Docker-only Codex login | 2 | 4 | 3 | 3 | rejected: OAuth lifecycle is awkward in workers |
| Host SDK plus Docker gateway | 4 | 4 | 5 | 5 | **chosen** |

The host gateway exposes only a narrow bearer-protected generation endpoint.
The same provider factory supports direct host execution and gateway clients.

## Privacy policy

| Resolution | Data minimization | Recall | Safety | Operator clarity | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Send raw Takeout to the model | 1 | 5 | 1 | 2 | rejected |
| Block all records with any possible identifier | 5 | 1 | 5 | 3 | rejected: excessive false positives |
| Redact, block high-risk, explicit override | 5 | 4 | 5 | 5 | **chosen** |

Redaction removes common identifiers and secrets. Health, legal, financial,
employment, secret, and government-ID findings block model work by default.
Overrides require a reason and still route the article to review.

## Model failure behavior

| Resolution | Availability | Truthfulness | Complexity | Recovery | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Fail closed on every Codex error | 2 | 5 | 5 | 1 | rejected for routine processing |
| Silent substitute model | 5 | 1 | 4 | 5 | rejected: hides quality changes |
| Automatic local fallback with metadata | 5 | 4 | 4 | 5 | **chosen** |

`gpt-5.6-luna` and reasoning `max` remain the requested defaults. If the
provider is unavailable, the actual provider and fallback reason are retained.

## Research

| Resolution | Source quality | Availability | Citation capture | Latency | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Deterministic HTTP search only | 3 | 5 | 3 | 4 | rejected as primary |
| Codex web search only | 5 | 3 | 5 | 2 | rejected as sole path |
| Codex primary plus HTTP fallback | 5 | 5 | 5 | 3 | **chosen** |

Search results are evidence indexes, not facts. Time-sensitive and sensitive
claims remain review-gated even when sources are captured.

## Publication policy

| Resolution | Throughput | Safety | Reversibility | Editorial quality | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Publish every rewrite automatically | 5 | 1 | 2 | 2 | rejected |
| Require manual approval for every record | 1 | 5 | 5 | 5 | rejected for ordinary high-confidence work |
| Auto-publish strict high-confidence output; review exceptions | 4 | 5 | 5 | 5 | **chosen** |

Privacy, uncertainty, time-sensitive/regulatory claims, ambiguous taxonomy,
and editorial exemptions cannot auto-publish. `N`, `HALF`, and `ALL` selection
modes are recorded in the run ledger so operators can expand scope deliberately.
