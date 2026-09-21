import { AppShell } from "@/components/shell/AppShell";
import { LoginPage } from "@/pages/LoginPage";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { ThemeProvider } from "@/contexts/ThemeContext";

function Gate() {
  const { ready, reachable, authenticated, auto_login } = useAuth();
  if (!ready) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-ink-3">
        Starting…
      </div>
    );
  }
  // The backend is not answering at all. That says nothing about whether this
  // operator is signed in, so showing the login form here is a lie — and it
  // is the one an app restart used to tell, on an install with auto-login on.
  // The page reloads itself when the server comes back (AuthContext).
  if (!reachable) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 text-sm text-ink-3">
        <p>Reconnecting to the trader…</p>
        <p className="text-xs text-ink-3">
          It will come back on its own when the app has restarted.
        </p>
      </div>
    );
  }
  // `auto_login` is the operator's own setting, read from the backend. The
  // client never decides to skip the password on its own.
  return authenticated || auto_login ? <AppShell /> : <LoginPage />;
}

export default function App() {
  return (
    // Theme outside auth: the login page is a screen too, and a sign-in form
    // that ignores the chosen theme is the first thing anybody sees.
    <ThemeProvider>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </ThemeProvider>
  );
}
