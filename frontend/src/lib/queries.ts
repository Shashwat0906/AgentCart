import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { useToast } from "../components/toastContext";
import { api, ApiError, type ProductQuery } from "./api";
import { useAuth } from "./useAuth";

export const qk = {
  products: (q: ProductQuery) => ["products", q] as const,
  product: (id: number) => ["product", id] as const,
  categories: ["categories"] as const,
  brands: (c?: string) => ["brands", c ?? "all"] as const,
  cart: ["cart"] as const,
  conversations: ["conversations"] as const,
  conversation: (id: string) => ["conversation", id] as const,
  preferences: ["preferences"] as const,
  orders: ["orders"] as const,
  config: ["config"] as const,
  tasks: ["tasks"] as const,
  task: (id: string) => ["task", id] as const,
};

export function useProducts(q: ProductQuery) {
  return useQuery({ queryKey: qk.products(q), queryFn: ({ signal }) => api.products(q, signal), placeholderData: keepPreviousData });
}

export function useCategories() {
  return useQuery({ queryKey: qk.categories, queryFn: api.categories, staleTime: 5 * 60_000 });
}

export function useCart() {
  const { isAuthenticated } = useAuth();
  return useQuery({ queryKey: qk.cart, queryFn: api.cart, enabled: isAuthenticated });
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "Something went wrong";
}

/** Direct (user-initiated) add to cart. Redirects to sign-in when needed. */
export function useAddToCart() {
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const { isAuthenticated } = useAuth();
  const mutation = useMutation({
    mutationFn: ({ productId, quantity }: { productId: number; quantity: number }) => api.addToCart(productId, quantity),
    onSuccess: (cart) => {
      qc.setQueryData(qk.cart, cart);
      toast("Added to your cart");
    },
    onError: (err) => toast(errorMessage(err), "error"),
  });
  return {
    ...mutation,
    add: (productId: number, quantity = 1) => {
      if (!isAuthenticated) {
        navigate(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
        return;
      }
      mutation.mutate({ productId, quantity });
    },
  };
}
