import type { Meta, StoryObj } from "@storybook/nextjs-vite";

// Headings from 24px up get tighter tracking (-1.5%).
const SIZES = [
  { className: "text-xs", note: "12px · captions, badges" },
  { className: "text-sm", note: "14px · dense UI" },
  { className: "text-base", note: "16px · reading" },
  { className: "text-lg", note: "18px · section titles" },
  { className: "text-2xl tracking-[-0.015em]", note: "24px · page titles" },
  { className: "text-4xl tracking-[-0.015em]", note: "36px · hero (marketing: 56-72px)" },
] as const;

function Typography() {
  return (
    <div className="flex max-w-3xl flex-col gap-10">
      <section aria-labelledby="sans">
        <h2 id="sans" className="mb-4 text-sm font-medium text-fg-muted">
          Geist Sans: UI and headings (400 / 500 / 600)
        </h2>
        <div className="flex flex-col gap-3">
          {SIZES.map(({ className, note }) => (
            <p key={className} className={className}>
              Where does checkout call the database?{" "}
              <span className="text-xs text-fg-muted">{note}</span>
            </p>
          ))}
          <p className="flex gap-6">
            <span className="font-normal">Regular 400</span>
            <span className="font-medium">Medium 500</span>
            <span className="font-semibold">Semibold 600</span>
          </p>
        </div>
      </section>

      <section aria-labelledby="mono">
        <h2 id="mono" className="mb-4 text-sm font-medium text-fg-muted">
          Commit Mono: code (ligatures off)
        </h2>
        <pre className="rounded-lg border border-line bg-surface-1 p-4 font-mono text-[13px] leading-6">
          {`@router.post("/orgs/{org}/invitations")
async def create_invitation(body: InvitationCreate) -> InvitationCreated:
    if role != Role.OWNER and x >= 0:  # => != -> <= ===
        return await orgs.invite(db, access, role=body.role)`}
        </pre>
        <p className="mt-2 font-mono text-sm text-fg-muted italic">Italic 400 for comments</p>
      </section>

      <section aria-labelledby="numbers">
        <h2 id="numbers" className="mb-4 text-sm font-medium text-fg-muted">
          Tabular numbers for metrics
        </h2>
        <table className="text-sm tabular-nums">
          <tbody>
            {[
              ["Indexed files", "11,482"],
              ["Symbols", "204,117"],
              ["Cost this month", "$1,038.20"],
            ].map(([label, value]) => (
              <tr key={label}>
                <th scope="row" className="pr-8 text-left font-normal text-fg-muted">
                  {label}
                </th>
                <td className="text-right">{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

const meta = {
  title: "Foundations/Typography",
  component: Typography,
} satisfies Meta<typeof Typography>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dark: Story = { globals: { theme: "dark" } };
export const Light: Story = { globals: { theme: "light" } };
