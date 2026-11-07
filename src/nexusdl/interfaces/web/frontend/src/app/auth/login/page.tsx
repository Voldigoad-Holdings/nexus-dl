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
 *    :func:`sanitizeRedirect` to reject absolute URLs, protocol-relative
 *    paths, and percent-encoded / whitespace-normalised bypasses — a
 *    login page is a classic open-redirect target, and this is not
 *    negotiable.
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
 *   programmatically focusable (``tabIndex={-1}``) and focused on every
 *   *new* error occurrence (a monotonic ``errorSeq`` counter is used so
 *   that re-emitting the same message still re-announces it).
 * * Per-field errors use ``aria-invalid`` plus ``aria-describedby``
 *   pointing at the message, so the reason is announced alongside the
 *   field.  Focus goes to the *first* invalid field, not blindly to
 *   email.
 * * The password reveal toggle is an ``aria-pressed`` button with an
 *   explicit ``aria-label`` that describes its action.
 * * ``autoComplete`` hints let password managers fill the form correctly.
 * * No ``autoFocus``: on mobile it pops the virtual keyboard before the
 *   user can read the form, and it fights the error-focus effect.
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
 * Decode a raw ``?next=`` value *once* and normalise it for safety checks.
 *
 * ``decodeURIComponent`` is called defensively: a malformed escape
 * sequence (``%zz``) throws, and a login page must never crash on a
 * hostile query string.  On failure we return the raw value unchanged so
 * the subsequent checks still see something to reject.
 */
function decodeRedirect(raw: string): string {
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

/**
 * Return a **safe** redirect target for the given raw ``?next=`` value.
 *
 * Only same-origin absolute paths are accepted.  The check is performed
 * on the *decoded* value so that percent-encoded bypasses
 * (``/%5Cevil``, ``/%09/evil``) and whitespace-normalised bypasses
 * (``/\\t/evil``, ``/\n/evil``) are rejected alongside the plain
 * ``//evil`` and ``/\\evil`` forms.
 *
 * Rules:
 *
 * * the decoded value must start with ``/``;
 * * it must **not** start with ``//`` (protocol-relative URL — an
 *   attacker could otherwise redirect to ``//evil.example``);
 * * it must **not** start with ``/\\`` (some browsers normalise
 *   ``/\\evil.example`` to ``//evil.example``);
 * * it must not contain a control character or backslash anywhere in
 *   the first path segment, since browsers strip tabs/newlines and
 *   treat backslashes as slashes during URL parsing.
 *
 * Anything else collapses to :data:`DEFAULT_REDIRECT`.
 */
function sanitizeRedirect(raw: string | null): string {
  if (raw === null) {
    return DEFAULT_REDIRECT;
  }
  const decoded = decodeRedirect(raw);
  if (!decoded.startsWith("/")) {
    return DEFAULT_REDIRECT;
  }
  if (decoded.startsWith("//") || decoded.startsWith("/\\")) {
    return DEFAULT_REDIRECT;
  }
  // Reject control chars and backslashes anywhere in the value: the
  // browser would strip/normalise them, which is exactly the class of
  // bypass we are defending against.
  if (/[\u0000-\u001f\u007f\\]/.test(decoded)) {
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

/**
 * Normalise anything thrown by :func:`api.login` into a
 * :class:`NexusApiError`.
 *
 * Kept as a standalone helper so the ``catch`` block stays exhaustive
 * and so the exact constructor shape is validated in one place.
 */
function toNexusApiError(err: unknown): NexusApiError {
  if (err instanceof NexusApiError) {
    return err;
  }
  const message = err instanceof Error ? err.message : String(err);
  return new NexusApiError(message, {
    code: "INTERNAL_ERROR",
    status: 0,
    cause: err,
  });
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
  // Monotonic counter: bumped every time a *new* submit error is
  // produced.  The focus effect depends on this counter rather than on
  // the message string, so re-emitting the same message (e.g. the user
  // retries with the same wrong password) still re-announces it to
  // screen readers.  (B5)
  const [errorSeq, setErrorSeq] = useState(0);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);

  const errorRef = useRef<HTMLDivElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  // Synchronous re-entrancy lock.  ``isSubmitting`` is a React state and
  // therefore only reflects after the next render — a fast double-submit
  // (Enter twice, or click + Enter) could otherwise fire two logins.
  // (B11)
  const inFlightRef = useRef(false);

  // Move focus to the error banner whenever a *new* submit failure
  // occurs, so that a screen-reader user is taken directly to the
  // message instead of having to tab back through the whole form.
  useEffect(() => {
    if (errorSeq > 0) {
      errorRef.current?.focus();
    }
  }, [errorSeq]);

  async function handleSubmit(
    event: FormEvent<HTMLFormElement>,
  ): Promise<void> {
    event.preventDefault();
    if (inFlightRef.current || isSubmitting) {
      return;
    }

    // Clear any errors from a previous submission.
    setSubmitError(null);
    setEmailError(null);
    setPasswordError(null);

    // Client-side validation.  The backend validates too, but a network
    // round-trip to say "email is required" is a poor experience.
    // Note: ``type="email"`` is non-functional here because the form is
    // ``noValidate`` — this is intentional (custom validation path).
    let firstInvalidRef: HTMLInputElement | null = null;
    if (email.trim().length === 0) {
      setEmailError("Email is required.");
      firstInvalidRef = emailRef.current;
    }
    if (password.length === 0) {
      setPasswordError("Password is required.");
      firstInvalidRef = firstInvalidRef ?? passwordRef.current;
    }
    if (firstInvalidRef !== null) {
      // Focus the *first* invalid field — there is no error banner yet,
      // and the user's job is to fix a specific input.  (B3)
      firstInvalidRef.focus();
      return;
    }

    inFlightRef.current = true;
    setIsSubmitting(true);

    try {
      const response = await api.login({
        email: email.trim(),
        password,
      });

      // Persist tokens first.  If this throws (quota exceeded, private
      // mode, …) the credentials are still valid server-side; we must
      // not surface a misleading "invalid credentials" message.  (B8)
      try {
        persistAuthTokens(response);
      } catch (persistErr) {
        // eslint-disable-next-line no-console
        console.error("Failed to persist auth tokens:", persistErr);
        setSubmitError(
          "Signed in, but your session could not be saved in this " +
            "browser. Please enable storage for this site and try again.",
        );
        setErrorSeq((n) => n + 1);
        inFlightRef.current = false;
        setIsSubmitting(false);
        return;
      }

      // ``replace`` (not ``push``) — after logging in, the user must not
      // be able to go "back" to the login page.
      router.replace(redirectTarget);
      // ``refresh`` so that any server-rendered state (middleware,
      // server components that read the new cookie) re-evaluates.
      router.refresh();
      // If the navigation above actually unmounts us, ``isSubmitting``
      // staying ``true`` is fine.  But if the navigation fails (middleware
      // loop, route not found, network error during RSC fetch), we stay
      // mounted — and a form stuck in ``isSubmitting`` with no feedback
      // is a dead end.  Release the lock unconditionally on the next
      // macrotask so the user can retry.  (B2)
      window.setTimeout(() => {
        inFlightRef.current = false;
        setIsSubmitting(false);
      }, 0);
    } catch (err) {
      const apiError = toNexusApiError(err);
      setSubmitError(describeLoginError(apiError));
      setErrorSeq((n) => n + 1);
      inFlightRef.current = false;
      setIsSubmitting(false);
    }
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
              required
              value={email}
              onChange={(event) => {
                setEmail(event.target.value);
                if (emailError !== null) {
                  setEmailError(null);
                }
                // Clear the banner as soon as the user edits either
                // field — it is stale feedback at that point.  (B6)
                if (submitError !== null) {
                  setSubmitError(null);
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
                ref={passwordRef}
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
                  if (submitError !== null) {
                    setSubmitError(null);
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
