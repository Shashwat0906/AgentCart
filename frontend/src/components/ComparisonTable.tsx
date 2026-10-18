import { Link } from "react-router";
import type { Comparison } from "../lib/types";

export function ComparisonTable({ data, caption = "Side-by-side comparison" }: { data: Comparison; caption?: string }) {
  if (!data.columns.length) return null;
  return (
    <div className="overflow-x-auto rounded-xl border border-line bg-white">
      <table className="w-full min-w-[520px] border-collapse text-sm">
        <caption className="sr-only">{caption}. Best values are marked.</caption>
        <thead>
          <tr className="bg-paper-2/70">
            <th scope="col" className="w-40 px-3 py-2.5 text-left text-xs font-semibold tracking-wide text-muted uppercase">Spec</th>
            {data.columns.map((c) => (
              <th key={c.product_id} scope="col" className="px-3 py-2.5 text-left align-bottom font-semibold">
                <Link to={`/products/${c.product_id}`} className="hover:text-accent">{c.name}</Link>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row) => (
            <tr key={row.key} className="border-t border-line">
              <th scope="row" className="px-3 py-2 text-left font-medium text-ink-2">{row.label}</th>
              {row.values.map((v, i) => {
                const best = row.best.includes(i);
                return (
                  <td key={i} className={`px-3 py-2 tabular-nums ${best ? "bg-leaf-soft font-semibold text-leaf" : ""}`}>
                    {v}
                    {best && <span className="sr-only"> (best)</span>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
