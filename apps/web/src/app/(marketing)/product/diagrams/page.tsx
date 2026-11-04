import type { Metadata } from "next";

import { DiagramDemo } from "@/components/marketing/product/diagram-demo";
import { ProductPage } from "@/components/marketing/product/product-page";
import { DEMO_REPO, DEMO_SHA, demoDiagram } from "@/lib/demo/product-demo";

const LEAD =
  "Diagrams of how the code works, drawn from its graph and checked against it, so a box or an arrow never stands for something the code doesn't do.";

export const metadata: Metadata = {
  title: "Diagrams",
  description: LEAD,
  alternates: { canonical: "/product/diagrams" },
};

const POINTS = [
  "Each edge is checked against the code's graph; one it can't confirm is marked inferred, with why.",
  "The share of the diagram the code verifies is on it, and the unverified part can be hidden.",
  "Each box opens the lines it stands for.",
  "A diagram says when the code has changed since it was drawn, and is drawn again in one click.",
  "Export it as PNG, SVG or Mermaid, or share it by link.",
] as const;

export default function DiagramsPage() {
  const diagram = demoDiagram();
  return (
    <ProductPage
      title="Diagrams"
      lead={LEAD}
      points={POINTS}
      example={`Example: signing in to ${DEMO_REPO} at ${DEMO_SHA}, drawn from its trace`}
    >
      <DiagramDemo
        spec={diagram.spec}
        mermaid={diagram.mermaid}
        verifiedRatio={diagram.verifiedRatio}
      />
    </ProductPage>
  );
}
