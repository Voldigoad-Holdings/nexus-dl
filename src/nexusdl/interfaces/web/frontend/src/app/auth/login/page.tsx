"use client";

/**
 * Login page — email/password authentication against
 * ``POST /api/v1/auth/login``.
 *
 * Flow
 * ----
 * 1. **Read the redirect target.**  The page accepts a ``?next=<path>``
 *    query parameter so that an authentication middleware can bounce an
 *    unauthenticated visitor to ``/auth/login?next=/library`` and resume
 *    there after login.  The value is **sanitised** by
 *    :func:`sanitizeRedirect` to reject absolute URLs and
 *    protocol-relative paths — a login page is a classic open-redirect
 *    target, and this is not negotiable.
 * 2. **Submit the credentials.**  On submit the form calls
 *    :meth:`~../../../lib/api.NexusApiClient.login`.  The API client
 *    already normalises every failure to :class:`NexusApiError`, so the
 *    error mapping is exhaustive and simple.
 * 3. **Persist and redirect.**  On success, :func:`persistAuthTokens`
 *    writes the tokens to ``localStorage`` (so a reload preserves the
 *    session) and the router does a ``replace`` (not a ``push``) to the
 *    sanitised target — the user must not be able to go "back" to the
 *    login page after logging in.  ``router.refresh()`` follows so that
 *    the target route's server components re-evaluate the new auth
 *    cookie.
 *
 * Hydration
 * ---------
 * ``useSearchParams()`` requires a Suspense boundary in Next.js 14 — the
 * default export wraps the form in one, so the page is a valid App Router
 * route.  The fallback is a skeleton with the same footprint as the card,
 * so there is no layout shift while the boundary resolves.
 *
 * Design
 * ------
 * Full-viewport centered card, neon-glowing brand title, dark theme (the
 * page inherits the app's default dark theme via ``globals.css``).  Every
 * colour and shadow comes from the design tokens — the page has no
 * hardcoded colour values.
 *
 * Accessibility
 * -------------
 * * Every field has a visible ``<label>``, not just a placeholder.
 * * Submit errors are surfaced in a ``role="alert"`` block that is
 *   programmatically focusable (``tabIndex={-1}``) and focused on error,
 *   so a screen-reader user hears the message without having to hunt for
 *   it.
 * * Per-field errors use ``aria-invalid`` plus ``aria-describedby``
 *   pointing at the message, so the reason is announced alongside the
 *   field.
 * * The password reveal toggle is an ``aria-pressed`` button with an
 *   explicit ``aria-label`` that describes its action.
 * * ``autoComplete`` hints let password managers fill the form correctly.
 */
import { AlertCircle, Eye, EyeOff, Loader2, LogIn } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Suspense,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { api, NexusApiError, persistAuthTokens } from "../../../lib/api";
import { cn } from "../../../lib/utils";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/** Fallback redirect target when ``?next=`` is absent or unsafe. */
const DEFAULT_REDIRECT = "/";

// ───────────────────────────────────────────────────────────────────────────
// Helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Return a **safe** redirect target for the given raw ``?next=`` value.
 *
 * Only same-origin absolute paths are accepted:
 *
 * * the value must start with ``/``;
 * * it must **not** start with ``//`` (protocol-relative URL — an attacker
 *   could otherwise redirect to ``//evil.example``);
 * * it must **not** start with ``/\`` (some browsers normalise
 *   ``/\evil.example`` to ``//evil.example``).
 *
 * Anything else collapses to :data:`DEFAULT_REDIRECT`.
 */
function sanitizeRedirect(raw: string | null): string {
  if (raw === null) {
    return DEFAULT_REDIRECT;
  }
  if (!raw.startsWith("/")) {
    return DEFAULT_REDIRECT;
  }
  if (raw.startsWith("//") || raw.startsWith("/\\")) {
    return DEFAULT_REDIRECT;
  }
  return raw;
}

/**
 * Map a :class:`NexusApiError` from the login endpoint to a user-facing
 * message.
 *
 * The codes referenced here are drawn from
 * :data:`~../../../lib/api.API_ERROR_CODES`.  ``status === 0`` on
 * ``INTERNAL_ERROR`` indicates a transport failure (the ``fetch`` call
 * threw before any response arrived).
 */
function describeLoginError(error: NexusApiError): string {
  switch (error.code) {
    case "UNAUTHORIZED":
      return "Invalid email or password.";
    case "RATE_LIMIT_EXCEEDED":
      return "Too many attempts. Please wait a moment and try again.";
    case "VALIDATION_ERROR":
      return error.message || "Please check your input and try again.";
    case "INTERNAL_ERROR":
      if (error.status === 0) {
        return "Cannot reach the server. Please check your connection.";
      }
      return "The server had a problem. Please try again in a moment.";
    default:
      return error.message || "Sign-in failed. Please try again.";
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Page
// ───────────────────────────────────────────────────────────────────────────

/**
 * The ``/auth/login`` route.
 *
 * See the module docstring for the full contract.
 */
export default function LoginPage() {
  return (
    <Suspense fallback={<LoginPageFallback />}>
      <LoginForm />
    </Suspense>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Form
// ───────────────────────────────────────────────────────────────────────────

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const redirectTarget = sanitizeRedirect(searchParams.get("next"));

  const baseId = useId();
  const emailId = `${baseId}-email`;
  const passwordId = `${baseId}-password`;
  const emailErrorId = `${baseId}-email-error`;
  const passwordErrorId = `${baseId}-password-error`;

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);

  const errorRef = useRef<HTMLDivElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);

  // Move focus to the error banner whenever a submit failure occurs, so
  // that a screen-reader user is taken directly to the message instead of
  // having to tab back through the whole form.
  useEffect(() => {
    if (submitError !== null) {
      errorRef.current?.focus();
    }
  }, [submitError]);

  async function handleSubmit(
    event: FormEvent<HTMLFormElement>,
  ): Promise<void> {
    event.preventDefault();
    if (isSubmitting) {
      return;
    }

    // Clear any errors from a previous submission.
    setSubmitError(null);
    setEmailError(null);
    setPasswordError(null);

    // Client-side validation.  The backend validates too, but a network
    // round-trip to say "email is required" is a poor experience.
    let hasError = false;
    if (email.trim().length === 0) {
      setEmailError("Email is required.");
      hasError = true;
    }
    if (password.length === 0) {
      setPasswordError("Password is required.");
      hasError = true;
    }
    if (hasError) {
      // Focus the first invalid field — there is no error banner yet,
      // and the user's job is to fix a specific input.
      emailRef.current?.focus();
      return;
    }

    setIsSubmitting(true);

    try {
      const response = await api.login({
        email: email.trim(),
        password,
      });
      persistAuthTokens(response);
      // ``replace`` (not ``push``) — after logging in, the user must not
      // be able to go "back" to the login page.
      router.replace(redirectTarget);
      // ``refresh`` so that any server-rendered state (middleware,
      // server components that read the new cookie) re-evaluates.
      router.refresh();
    } catch (err) {
      const apiError =
        err instanceof NexusApiError
          ? err
          : new NexusApiError(
              err instanceof Error ? err.message : String(err),
              { code: "INTERNAL_ERROR", status: 0, cause: err },
            );
      setSubmitError(describeLoginError(apiError));
      setIsSubmitting(false);
    }
    // On success we deliberately leave ``isSubmitting`` true — the
    // component is about to be unmounted by the navigation, and
    // re-enabling the button for a fraction of a frame would flicker.
  }

  return (
    <main className="flex min-h-[100dvh] w-full items-center justify-center p-4">
      <div className="w-full max-w-sm">
        {/* Brand ------------------------------------------------ */}
        <div className="mb-6 flex items-center justify-center gap-2">
          <span
            aria-hidden
            className={cn(
              "grid h-10 w-10 place-items-center rounded-lg",
              "bg-primary text-lg font-bold text-primary-foreground",
              "shadow-[0_0_20px_hsl(var(--primary)/0.4)]",
            )}
          >
            N
          </span>
          <span className="neon-text text-2xl font-bold tracking-tight">
            NexusDL
          </span>
        </div>

        {/* Card ------------------------------------------------ */}
        <form
          onSubmit={handleSubmit}
          noValidate
          aria-label="Sign in"
          className="neon-card p-6"
        >
          <header className="mb-6">
            <h1 className="text-xl font-semibold tracking-tight">
              Welcome back
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Sign in to continue.
            </p>
          </header>

          {/* Error banner ------------------------------------ */}
          {submitError !== null && (
            <div
              ref={errorRef}
              role="alert"
              aria-live="assertive"
              tabIndex={-1}
              className={cn(
                "mb-4 flex items-start gap-2 rounded-md",
                "border border-destructive/30 bg-destructive/10",
                "p-3 text-sm text-destructive",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-destructive/50",
              )}
            >
              <AlertCircle
                className="mt-0.5 h-4 w-4 shrink-0"
                aria-hidden
              />
              <span>{submitError}</span>
            </div>
          )}

          {/* Email ------------------------------------------- */}
          <div className="mb-4 flex flex-col gap-1.5">
            <label htmlFor={emailId} className="text-sm font-medium">
              Email
            </label>
            <input
              ref={emailRef}
              id={emailId}
              type="email"
              name="email"
              autoComplete="email"
              autoFocus
              required
              value={email}
              onChange={(event) => {
                setEmail(event.target.value);
                if (emailError !== null) {
                  setEmailError(null);
                }
              }}
              disabled={isSubmitting}
              aria-invalid={emailError !== null}
              aria-describedby={
                emailError !== null ? emailErrorId : undefined
              }
              placeholder="you@example.com"
              className={cn(
                "h-10 w-full rounded-md border bg-background px-3 text-sm",
                "placeholder:text-muted-foreground",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring focus-visible:ring-offset-2",
                "focus-visible:ring-offset-background",
                "disabled:cursor-not-allowed disabled:opacity-50",
                emailError !== null && "border-destructive",
              )}
            />
            {emailError !== null && (
              <p id={emailErrorId} className="text-xs text-destructive">
                {emailError}
              </p>
            )}
          </div>

          {/* Password ---------------------------------------- */}
          <div className="mb-4 flex flex-col gap-1.5">
            <label htmlFor={passwordId} className="text-sm font-medium">
              Password
            </label>
            <div className="relative">
              <input
                id={passwordId}
                type={showPassword ? "text" : "password"}
                name="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                  if (passwordError !== null) {
                    setPasswordError(null);
                  }
                }}
                disabled={isSubmitting}
                aria-invalid={passwordError !== null}
                aria-describedby={
                  passwordError !== null ? passwordErrorId : undefined
                }
                placeholder="••••••••"
                className={cn(
                  "h-10 w-full rounded-md border bg-background pl-3 pr-10",
                  "text-sm placeholder:text-muted-foreground",
                  "focus-visible:outline-none focus-visible:ring-2",
                  "focus-visible:ring-ring focus-visible:ring-offset-2",
                  "focus-visible:ring-offset-background",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                  passwordError !== null && "border-destructive",
                )}
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={
                  showPassword ? "Hide password" : "Show password"
                }
                aria-pressed={showPassword}
                disabled={isSubmitting}
                className={cn(
                  "absolute right-2 top-1/2 -translate-y-1/2 rounded p-1",
                  "text-muted-foreground hover:bg-accent hover:text-foreground",
                  "focus-visible:outline-none focus-visible:ring-2",
                  "focus-visible:ring-ring",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                )}
              >
                {showPassword ? (
                  <EyeOff className="h-4 w-4" aria-hidden />
                ) : (
                  <Eye className="h-4 w-4" aria-hidden />
                )}
              </button>
            </div>
            {passwordError !== null && (
              <p
                id={passwordErrorId}
                className="text-xs text-destructive"
              >
                {passwordError}
              </p>
            )}
          </div>

          {/* Submit ------------------------------------------ */}
          <button
            type="submit"
            disabled={isSubmitting}
            className={cn(
              "mt-2 inline-flex h-10 w-full items-center justify-center gap-2",
              "rounded-md bg-primary text-sm font-medium",
              "text-primary-foreground",
              "transition-[background-color,box-shadow] duration-150",
              "hover:bg-primary/90",
              "hover:shadow-[0_0_24px_hsl(var(--primary)/0.5)]",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring focus-visible:ring-offset-2",
              "focus-visible:ring-offset-background",
              "disabled:cursor-not-allowed disabled:opacity-50",
              "disabled:hover:shadow-none",
            )}
          >
            {isSubmitting ? (
              <>
                <Loader2
                  className="h-4 w-4 animate-spin"
                  aria-hidden
                />
                <span>Signing in…</span>
              </>
            ) : (
              <>
                <LogIn className="h-4 w-4" aria-hidden />
                <span>Sign in</span>
              </>
            )}
          </button>
        </form>

        {/* Footer ---------------------------------------------- */}
        <p className="mt-6 text-center text-xs text-muted-foreground">
          NexusDL v3.0 · Contact your administrator if you need an
          account.
        </p>
      </div>
    </main>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Fallback (Suspense)
// ───────────────────────────────────────────────────────────────────────────

/**
 * Skeleton rendered while the Suspense boundary resolves
 * ``useSearchParams()``.
 *
 * The footprint matches the real card exactly — same max-width, same
 * vertical rhythm, same number of blocks — so there is no layout shift
 * when the real form replaces the skeleton.
 */
function LoginPageFallback() {
  return (
    <main className="flex min-h-[100dvh] w-full items-center justify-center p-4">
      <div className="w-full max-w-sm animate-pulse">
        <div className="mb-6 flex items-center justify-center gap-2">
          <div className="h-10 w-10 rounded-lg bg-muted" />
          <div className="h-8 w-28 rounded bg-muted" />
        </div>
        <div className="neon-card space-y-4 p-6">
          <div className="h-6 w-40 rounded bg-muted" />
          <div className="h-4 w-32 rounded bg-muted" />
          <div className="h-10 rounded bg-muted" />
          <div className="h-10 rounded bg-muted" />
          <div className="h-10 rounded bg-muted" />
        </div>
      </div>
    </main>
  );
}
