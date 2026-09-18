import { AppShell } from "@/components/shell/AppShell";
import { LoginPage } from "@/pages/LoginPage";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";

function Gate() {
  const { ready, authenticated, auto_login } = useAuth();
  if (!ready) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-ink-3">
        Starting…
      </div>
    );
  }
  // `auto_login` is the operator's own setting, read from the backend. The
  // client never decides to skip the password on its own.
  return authenticated || auto_login ? <AppShell /> : <LoginPage />;
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}
