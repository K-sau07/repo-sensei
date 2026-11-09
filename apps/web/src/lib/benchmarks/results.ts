/**
 * CrossLayerBench v1's published results (10.4): the public split's flows (101; 20 more are held
 * out), traced by the `trace` tracer at engine ff84fa2e68b4 on 2026-11-09. Worked out from
 * the runs' reports (`sensei-eval run --suite crosslayerbench-v1 --retriever trace`), not typed in.
 */

export interface BenchRow {
  repo: string;
  /** The copy with every project-defined name renamed (what a tracer finds without names). */
  renamed: boolean;
  stack: string;
  /** The tracer was developed against this repository (v0's): the others it never saw. */
  tuned: boolean;
  flows: number;
  hopRecall: number;
  hopPrecision: number;
  exact: number;
  exactAt3: number;
  layersHit: number;
}

export const BENCH_RUN = { date: "2026-11-09", engine: "ff84fa2e68b4" } as const;
export const DATASET_URL = "https://github.com/K-sau07/crosslayerbench";
export const PUBLIC_FLOWS = 101;
export const HELD_OUT_FLOWS = 20;

export const CROSSLAYER_V1: readonly BenchRow[] = [
  {
    repo: "fastapi-template",
    renamed: false,
    stack: "React, FastAPI, SQLModel",
    tuned: true,
    flows: 8,
    hopRecall: 0.95,
    hopPrecision: 0.964,
    exact: 0.75,
    exactAt3: 1.0,
    layersHit: 0.75,
  },
  {
    repo: "fastapi-template",
    renamed: true,
    stack: "React, FastAPI, SQLModel",
    tuned: true,
    flows: 8,
    hopRecall: 0.7,
    hopPrecision: 0.788,
    exact: 0.375,
    exactAt3: 0.5,
    layersHit: 0.375,
  },
  {
    repo: "langflow",
    renamed: false,
    stack: "React, FastAPI, SQLModel",
    tuned: true,
    flows: 17,
    hopRecall: 0.703,
    hopPrecision: 0.646,
    exact: 0.294,
    exactAt3: 0.412,
    layersHit: 0.412,
  },
  {
    repo: "langflow",
    renamed: true,
    stack: "React, FastAPI, SQLModel",
    tuned: true,
    flows: 17,
    hopRecall: 0.226,
    hopPrecision: 0.487,
    exact: 0.0,
    exactAt3: 0.0,
    layersHit: 0.0,
  },
  {
    repo: "redash",
    renamed: false,
    stack: "React, Flask, SQLAlchemy, RQ",
    tuned: false,
    flows: 25,
    hopRecall: 0.233,
    hopPrecision: 0.646,
    exact: 0.0,
    exactAt3: 0.0,
    layersHit: 0.0,
  },
  {
    repo: "redash",
    renamed: true,
    stack: "React, Flask, SQLAlchemy, RQ",
    tuned: false,
    flows: 25,
    hopRecall: 0.211,
    hopPrecision: 0.801,
    exact: 0.0,
    exactAt3: 0.0,
    layersHit: 0.0,
  },
  {
    repo: "umami",
    renamed: false,
    stack: "Next.js, Prisma",
    tuned: false,
    flows: 26,
    hopRecall: 0.325,
    hopPrecision: 0.568,
    exact: 0.0,
    exactAt3: 0.0,
    layersHit: 0.0,
  },
  {
    repo: "umami",
    renamed: true,
    stack: "Next.js, Prisma",
    tuned: false,
    flows: 26,
    hopRecall: 0.183,
    hopPrecision: 0.788,
    exact: 0.0,
    exactAt3: 0.0,
    layersHit: 0.0,
  },
  {
    repo: "jhipster-react",
    renamed: false,
    stack: "React, Redux, Spring Boot, JPA",
    tuned: false,
    flows: 25,
    hopRecall: 0.276,
    hopPrecision: 0.653,
    exact: 0.0,
    exactAt3: 0.0,
    layersHit: 0.0,
  },
];
