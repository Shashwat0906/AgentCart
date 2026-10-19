import { QueryClientProvider } from "@tanstack/react-query";
import { Compass } from "lucide-react";
import { BrowserRouter, Link, Route, Routes } from "react-router";
import { Container, Layout, RequireAuth } from "./components/Layout";
import { ToastProvider } from "./components/toast";
import { EmptyState } from "./components/ui";
import { ChatPage } from "./features/chat/ChatPage";
import { AuthProvider } from "./lib/auth";
import { makeQueryClient } from "./lib/queryClient";
import { LoginPage, RegisterPage } from "./pages/AuthPages";
import { CartPage } from "./pages/CartPage";
import { CatalogPage } from "./pages/CatalogPage";
import { CheckoutPage } from "./pages/CheckoutPage";
import { ComparePage } from "./pages/ComparePage";
import { HomePage } from "./pages/HomePage";
import { OrdersPage } from "./pages/OrdersPage";
import { PreferencesPage } from "./pages/PreferencesPage";
import { TasksPage } from "./pages/TasksPage";
import { ProductPage } from "./pages/ProductPage";

function NotFound() {
  return (
    <Container>
      <EmptyState title="Page not found" icon={Compass} action={<Link to="/" className="btn btn-primary btn-sm">Go home</Link>}>
        The page you're looking for doesn't exist.
      </EmptyState>
    </Container>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<HomePage />} />
        <Route path="products" element={<CatalogPage />} />
        <Route path="products/:id" element={<ProductPage />} />
        <Route path="compare" element={<ComparePage />} />
        <Route path="login" element={<LoginPage />} />
        <Route path="register" element={<RegisterPage />} />
        <Route path="assistant" element={<RequireAuth><ChatPage /></RequireAuth>} />
        <Route path="assistant/:conversationId" element={<RequireAuth><ChatPage /></RequireAuth>} />
        <Route path="cart" element={<RequireAuth><CartPage /></RequireAuth>} />
        <Route path="checkout" element={<RequireAuth><CheckoutPage /></RequireAuth>} />
        <Route path="orders" element={<RequireAuth><OrdersPage /></RequireAuth>} />
        <Route path="preferences" element={<RequireAuth><PreferencesPage /></RequireAuth>} />
        <Route path="tasks" element={<RequireAuth><TasksPage /></RequireAuth>} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

const queryClient = makeQueryClient();

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ToastProvider>
          <AuthProvider>
            <AppRoutes />
          </AuthProvider>
        </ToastProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
