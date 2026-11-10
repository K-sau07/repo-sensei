import type { Metadata } from "next";

import { LongPage } from "@/components/marketing/long-page";
import {
  BENCH_RUN,
  CROSSLAYER_V1,
  DATASET_URL,
  HELD_OUT_FLOWS,
  PUBLIC_FLOWS,
  type BenchRow,
} from "@/lib/benchmarks/results";

const LEAD =
  "How well repo-sensei follows a feature from the screen to the data, measured on code it was never tuned on.";

export const metadata: Metadata = {
  title: "Benchmarks",
  description: LEAD,
  alternates: { canonical: "/benchmarks" },
};

const pct = (n: number) => n.toFixed(2);

function Row({ row }: { row: BenchRow }) {
  return (
    <tr>
      <th scope="row" className="font-normal">
        {row.repo}
        {row.renamed ? " (renamed)" : ""}
        {row.tuned && !row.renamed ? (
          <>
            <sup aria-hidden>*</sup>
            <span className="sr-only"> (tuned on)</span>
          </>
        ) : null}
      </th>
      <td>{row.flows}</td>
      <td>{pct(row.hopRecall)}</td>
      <td>{pct(row.hopPrecision)}</td>
      <td>{pct(row.exact)}</td>
      <td>{pct(row.exactAt3)}</td>
      <td>{pct(row.layersHit)}</td>
    </tr>
  );
}

/** 10.4: CrossLayerBench v1 (methodology, the results, how to reproduce them) and FixBench. */
export default function BenchmarksPage() {
  return (
    <LongPage title="Benchmarks" lead={LEAD} updated={BENCH_RUN.date}>
      <h2>CrossLayerBench</h2>
      <p>
        Most code-search benchmarks ask whether a tool finds <em>a</em> relevant file.
        CrossLayerBench asks whether it finds <em>the path</em>. Each flow starts where a user acts:
        a form, a page, a button. It ends at the table the request reads or writes. Every hop in
        between was labelled by hand at a pinned commit, through the state, the API client, the
        route handler, its services and background jobs:
      </p>
      <ul>
        <li>
          <strong>{PUBLIC_FLOWS} published flows</strong> over 5 open-source repositories: FastAPI,
          Flask, Next.js with Prisma, and Spring Boot with JPA, under React front ends.
        </li>
        <li>
          <strong>{HELD_OUT_FLOWS} held-out flows</strong> labelled the same way and kept private. A
          published benchmark can end up in training data; these check that a gain is real.
        </li>
        <li>
          <strong>Renamed copies</strong> of the Python and TypeScript repositories. Every
          project-defined name is replaced, to measure tracing that doesn&apos;t lean on names.
        </li>
      </ul>
      <p>
        The flows, their format and the labelling conventions are at{" "}
        <a href={DATASET_URL}>github.com/K-sau07/crosslayerbench</a>, under CC BY 4.0.
      </p>

      <h3>Results</h3>
      <p>
        These are repo-sensei&apos;s tracer on the published flows (engine{" "}
        <code>{BENCH_RUN.engine}</code>). It builds a call graph across languages from a checkout
        and makes no model calls. <strong>Hop recall</strong> is the share of a flow&apos;s labelled
        hops the best path reaches. <strong>Exact</strong> is a best path that is the labelled path
        in order, and <strong>exact@3</strong> counts any of the three paths it offers.
      </p>
      <table>
        <caption className="sr-only">CrossLayerBench v1 results by repository</caption>
        <thead>
          <tr>
            <th scope="col">Repository</th>
            <th scope="col">Flows</th>
            <th scope="col">Hop recall</th>
            <th scope="col">Hop precision</th>
            <th scope="col">Exact</th>
            <th scope="col">Exact@3</th>
            <th scope="col">Layers hit</th>
          </tr>
        </thead>
        <tbody>
          {CROSSLAYER_V1.map((row) => (
            <Row key={`${row.repo}${row.renamed ? "-renamed" : ""}`} row={row} />
          ))}
        </tbody>
      </table>
      <p>
        <sup>*</sup> The tracer was developed against these two repositories.
      </p>
      <p>The table says plainly where the tracer stands:</p>
      <ul>
        <li>
          <strong>Repositories it was tuned on:</strong> it finds their paths nearly whole.
        </li>
        <li>
          <strong>Repositories it has never seen</strong> (redash, umami, jhipster): it reaches the
          first hop or two, then loses the path. Typical breaks are:
          <ul>
            <li>an API helper that builds URLs at run time;</li>
            <li>Next.js handlers found by their file&apos;s place in the tree;</li>
            <li>Flask-RESTful resources registered as plain statements;</li>
            <li>Redux thunks.</li>
          </ul>
        </li>
        <li>
          <strong>Renamed copies:</strong> the scores drop, which shows how much of what it finds
          rests on names.
        </li>
      </ul>
      <p>
        That gap is the work ahead, and these flows are how it will be measured. Meanwhile, every
        hop in a trace says how it was found: verified from the code itself, inferred by a rule, or
        unresolved, where the graph has no next hop.
      </p>

      <h3>Reproduce</h3>
      <p>
        The scorer and the tracer are open source in{" "}
        <a href="https://github.com/K-sau07/repo-sensei">repo-sensei</a> (
        <code>packages/evals</code>). A run makes no model or embedding calls, so it costs nothing:
      </p>
      <ol>
        <li>Check out each repository at the commit in the dataset&apos;s repository list.</li>
        <li>
          Put the bench file at <code>evals/private/crosslayerbench/v1.yaml</code> in a repo-sensei
          checkout.
        </li>
        <li>
          Run{" "}
          <code>
            sensei-eval run --suite crosslayerbench-v1 --retriever trace --repo umami --checkout
            &lt;path&gt;
          </code>
          . It writes a report with every flow&apos;s hits and misses.
        </li>
      </ol>

      <h2>FixBench</h2>
      <p>
        FixBench is 30 bugs planted in a small full-stack app, with fixes that ship as pull
        requests. The bugs include off-by-ones, timezone and DST windows, N+1 queries, a lost update
        under concurrency, a wrong SQL filter, and an authorization bypass. A fix counts only when:
      </p>
      <ul>
        <li>a hidden test written for the bug passes;</li>
        <li>the app&apos;s own tests still pass;</li>
        <li>nothing outside the bug changed.</li>
      </ul>
      <p>
        The harness itself is checked with known-good patches: all 30 are judged fixed, and none
        unsafe. The model&apos;s results will be published here from its first full run. Until then,
        there is no number to show.
      </p>
    </LongPage>
  );
}
