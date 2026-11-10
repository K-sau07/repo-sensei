# repo-sensei

repo-sensei reads a codebase the way a careful engineer would, then answers for what it says.

- **Ask** a question in words and get an answer whose every claim cites the lines that show it.
  Each claim is checked against those lines before you see it, and an answer it can't stand
  behind says so instead of guessing.
- **Trace** a feature from the screen to the data: the component, the API call, the route, the
  services, the table, hop by hop, across languages. Each hop says how it was found: verified
  from the code, inferred by a rule, or unresolved.
- **Fix** a bug from a report: it reproduces the bug with a failing test, finds the cause, and
  proposes a plan for a person to approve. Then it writes the patch in a sandbox and checks it
  with your tests, and opens a pull request only when the fix holds up.
- **Use it from your editor or agent** through its MCP server, its CLI, or its API.

## How it works

```
GitHub ──webhooks──▶ API (FastAPI) ──▶ Postgres (pgvector, BM25, row-level security)
                      │    ▲                 ▲
   web (Next.js) ─────┘    │                 │
                     Redis queues ──▶ workers (Celery): clone, parse, embed, summarize,
                                       graph, link, index; Ask and Fix agents
                                            │
                                            └──▶ sandbox host: Docker + gVisor, for running
                                                 a repository's own tests
```

- **Indexing.** Every commit is parsed with tree-sitter into symbols and chunks, and embedded
  for semantic search. It is also scored for keyword search (BM25, in Postgres). Calls, imports
  and HTTP requests become a graph that crosses language boundaries: a `fetch` in TypeScript is
  linked to the Python or Java handler that serves it, and on to the ORM model it touches.
  Re-indexing a push only redoes what changed.
- **Ask** is an agent with read-only tools: code search, grep, symbols, the graph, a tracer,
  file reads. It first commits to the evidence it will cite, then writes from that evidence
  alone. A second pass checks each cited claim against its lines and rewrites or removes what
  they don't show. The confidence you see comes from what was checked.
- **Fix** runs in stages, each a short-lived agent with a narrow set of tools: investigate,
  reproduce, root cause, plan, patch, verify. Patches are held to anti-gaming rules. Editing
  tests to pass, special-casing the test's values, swallowing the error, exiting early, or
  touching CI or credentials blocks the fix, whatever the tests say.

## Safety

- **Tenants are isolated in the database**, not just in code. Every org-scoped table has
  forced row-level security. A test suite seeds every table in two orgs and proves neither can
  read or write the other's rows, and another proves it over HTTP. An authorization matrix
  holds every API operation to a declared rule for each role.
- **A repository's text is data, never instructions.** The agents are told so. Their tools
  can't leave the checkout (no symlinks out, paths only from the indexed tree), they have no
  web access, and answers never load remote images. Tests drive a model scripted to obey
  planted instructions and show it reaches nothing outside.
- **Untrusted code runs in gVisor** on a separate host, with a read-only root, no
  capabilities, and time and memory limits. Tests run with no route out. Dependency installs
  reach only a proxy to the package registries. A person approves the plan, and the change ships as
  a pull request for review.
- **Spend is capped**: per person, per org, and globally. Past the global cap the service goes
  read-only until the next day.

## Benchmarks

[CrossLayerBench](https://github.com/K-sau07/crosslayerbench) measures whether a tracer, started
at a UI entry point, reaches the code that serves it all the way down to the data. It has 101
hand-labelled flows over five open-source full-stack repositories, plus renamed copies and a
private held-out split. repo-sensei's tracer makes no model calls:

| repository                  | hop recall | exact@3 |
| --------------------------- | ---------- | ------- |
| fastapi-template (tuned on) | 0.95       | 1.00    |
| langflow (tuned on)         | 0.70       | 0.41    |
| redash                      | 0.23       | 0.00    |
| umami                       | 0.33       | 0.00    |
| jhipster-react              | 0.28       | 0.00    |

It finds the paths of the repositories it was developed on nearly whole. On repositories it
has never seen, it loses the path after a hop or two. That gap is the work ahead, and these
flows are how it will be measured. Full results, renamed copies and how to reproduce them:
[the dataset's page](https://github.com/K-sau07/crosslayerbench).

## Trade-offs

- **Postgres for everything:** vectors, keyword search, the graph, the queues' state. One
  database to secure, back up and restore, at the cost of a search engine's tuning knobs.
- **Checked answers are slower answers.** Verification adds a model pass after the answer. It
  is what lets an answer say "I couldn't find it" instead of inventing it.
- **The tracer is rules plus a graph**, not a model. It is cheap, deterministic and explains
  every hop, and it misses what its rules don't cover (see the benchmark).
- **Fixes are conservative.** Anything the rules can't read (binary changes, symlinks, CI) is
  refused, so some real fixes are left to people.

## Local development

You need Docker, [uv](https://docs.astral.sh/uv/), Node 22 with pnpm, and `make`.

```sh
make setup        # Python and JS dependencies, browsers for tests, git hooks
cp .env.example .env
make up           # Postgres, Redis and S3-compatible storage in Docker
make migrate
make dev          # API on :8000, workers, scheduler and the web app on :3000
```

The web app also runs against a mock API with no backend: `NEXT_PUBLIC_MOCKS=1 make web`.
Ask, Fix, summaries and semantic search need model and embedding keys in `.env` (Anthropic,
Voyage).

```sh
make check        # lint, types and unit tests (Python, web, stories with accessibility checks)
make test-int     # integration tests against the local stack
```

Deploying: `infra/single-server/` puts everything on one machine (an Oracle Cloud Always Free VM
works) behind a Cloudflare Tunnel, for $0 beyond the models; `infra/fly/README.md` splits it
across Fly.io and a sandbox host (`infra/sandbox-host/`), with point-in-time restore either way.

## License

All rights reserved.
