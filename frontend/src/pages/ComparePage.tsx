import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";
import { ComparisonTable } from "../components/ComparisonTable";
import { Container } from "../components/Layout";
import { EmptyState, ErrorState, PageHeader, Spinner } from "../components/ui";
import { api } from "../lib/api";

export function ComparePage() {
  const [params] = useSearchParams();
  const ids = (params.get("ids") ?? "").split(",").map(Number).filter((n) => Number.isInteger(n) && n > 0).slice(0, 4);
  const cmp = useQuery({ queryKey: ["compare", ids], queryFn: () => api.compare(ids), enabled: ids.length >= 2 });

  return (
    <Container>
      <PageHeader eyebrow="Compare" title="Side by side" />
      {ids.length < 2 ? (
        <EmptyState title="Pick at least two products" action={<Link to="/products" className="btn btn-primary btn-sm">Browse products</Link>}>
          Use the compare button on product cards, or ask the assistant to “compare the top three”.
        </EmptyState>
      ) : cmp.isLoading ? <Spinner /> : cmp.isError ? <ErrorState error={cmp.error} /> : cmp.data ? (
        <>
          <p className="mb-4 text-sm text-muted">Highlighted cells mark the best value in each row. Every value comes straight from the catalogue.</p>
          <ComparisonTable data={cmp.data} />
        </>
      ) : null}
    </Container>
  );
}
