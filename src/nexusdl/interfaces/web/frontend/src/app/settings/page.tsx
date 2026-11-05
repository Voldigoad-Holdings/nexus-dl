"use client";

/**
 * Settings page — the ``/settings`` route.
 *
 * Two distinct kinds of settings live on this page, in two visually
 * distinct sections:
 *
 * 1. **Your preferences** — the browser-local settings persisted by
 *    :class:`~../../store/settings.SettingsStore` (``localStorage``).  These
 *    are edited with proper form controls and saved automatically on every
 *    change.  No server round-trip, no "Save" button.  They are per-browser,
 *    not per-account.
 *
 * 2. **Server settings** — the backend configuration read from
 *    ``GET /api/v1/settings`` and written through
 *    ``PATCH /api/v1/settings``.  This section is **ADMIN-only** per the
 *    backend contract.  It is collapsible and loads on first expand;
 *    non-admins see a "requires admin" notice rather than an error banner.
 *    Editing is a raw JSON textarea because the backend's Settings model
 *    is not fixed at the wire level (see
 *    :type:`~../../lib/api.SettingsSnapshot`); the backend validates on
 *    write, so the frontend does not need to.
 *
 * Sections on the page
 * --------------------
 * * Appearance — UI language, sidebar collapsed, library view.
 * * Content — allow adult content, include unhealthy sites by default.
 * * Downloads — packaging format, priority, skip existing.
 * * Live updates — enable the ``/ws/v1/events`` WebSocket.
 * * Session — sign out.
 * * Server settings (admin) — collapsible JSON editor.
 * * About — build version, commit, build date.
 *
 * Design decisions worth flagging
 * -------------------------------
 * * **Auto-save.**  Every preference control mutates the store on change;
 *   the store persists to ``localStorage`` synchronously via Zustand's
 *   ``persist`` middleware.  There is no "Save" button, and there is
 *   nothing to accidentally lose by navigating away.
 *
 * * **"Reset to defaults" is confirmed.**  The reset restores every
 *   preference — including ones the user may not remember they set (sidebar
 *   state, view mode).  A one-shot ``window.confirm`` is the standard,
 *   accessible way to gate that.
 *
 * * **No shadcn dependencies.**  Every control — toggle, select, library
 *   view segmented control, admin JSON editor — is a native HTML form
 *   element styled with the Tailwind tokens defined in
 *   ``src/app/globals.css``.  When the shadcn primitives land, the visual
 *   style can be upgraded per-section without changing the page logic.
 *
 * * **Sign out is a full client reset.**  It disconnects the WebSocket
 *   singleton, resets the API client singleton, clears the tokens from
 *   ``localStorage``, and empties the downloads store so that the next
 *   sign-in on the same browser does not briefly render the previous
 *   user's queue.  The browser-local preferences are intentionally
 *   **preserved** — they belong to the browser, not the account.
 *
 * * **Version info is refetched at most once.**  ``api.getVersion()``
 *   returns static build metadata that only changes across deployments, so
 *   the query uses ``staleTime: Infinity`` and disables window-focus
 *   refetching.  A redeploy resets the SPA anyway.
 *
 * Hydration
 * ---------
 * The settings store uses ``skipHydration`` and returns the defaults
 * (``library_view: "grid"``, ``language: "en"``, …) on both the server and
 * the first client render — there is no hydration mismatch.  The page
 * **does** gate on :func:`~../../store/settings.useSettingsHydrated` to
 * avoid a visible "snap" from defaults to the user's persisted values
 * immediately after hydration.  The skeleton mirrors the page's structure,
 * so there is no layout shift when the real content replaces it.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Check,
  ChevronDown,
  Loader2,
  LogOut,
  RotateCcw,
  Server,
  ShieldAlert,
  X,
} from "lucide-react";
import { useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useId,
  useState,
  type ReactNode,
} from "react";

import {
  api,
  clearAuthTokens,
  NexusApiError,
  resetApiClient,
  type SettingsSnapshot,
  type VersionResponse,
} from "../../lib/api";
import { resetWebSocketClient } from "../../lib/ws";
import { cn } from "../../lib/utils";
import { useDownloadsStore } from "../../store/downloads";
import {
  UI_LOCALES,
  useSettingsHydrated,
  useSettingsStore,
  type UILocale,
  type LibraryView,
} from "../../store/settings";
import {
  DOWNLOAD_PRIORITIES,
  PACKAGING_FORMATS,
  type DownloadPriority,
  type PackagingFormat,
} from "../../types/download";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/** Human-readable labels for each UI locale. */
const LOCALE_LABELS: Record<UILocale, string> = {
  en: "English",
  fr: "Français",
  es: "Español",
  de: "Deutsch",
};

/** Human-readable labels for each packaging format. */
const FORMAT_LABELS: Record<PackagingFormat, string> = {
  folder: "Folder (no archive)",
  zip: "ZIP archive",
  cbz: "CBZ (Comic Book ZIP)",
  cb7: "CB7 (Comic Book 7-Zip)",
  pdf: "PDF document",
  cbt: "CBT (Comic Book TAR)",
};

/** Human-readable labels for each download priority. */
const PRIORITY_LABELS: Record<DownloadPriority, string> = {
  low: "Low — after everything else",
  normal: "Normal — the default",
  high: "High — before everything else",
};

// ───────────────────────────────────────────────────────────────────────────
// Page
// ───────────────────────────────────────────────────────────────────────────

/**
 * The ``/settings`` route.  See the module docstring for the full
 * contract.
 */
export default function SettingsPage() {
  const hydrated = useSettingsHydrated();

  if (!hydrated) {
    return <SettingsPageSkeleton />;
  }

  return <SettingsBody />;
}

// ───────────────────────────────────────────────────────────────────────────
// Body
// ───────────────────────────────────────────────────────────────────────────

function SettingsBody() {
  const router = useRouter();

  // -------------------------------------------------------------------
  // Store state and actions
  // -------------------------------------------------------------------
  const language = useSettingsStore((s) => s.language);
  const allowAdult = useSettingsStore((s) => s.allow_adult);
  const defaultIncludeUnhealthy = useSettingsStore(
    (s) => s.default_include_unhealthy,
  );
  const defaultFormat = useSettingsStore((s) => s.default_packaging_format);
  const defaultPriority = useSettingsStore((s) => s.default_priority);
  const defaultSkipExisting = useSettingsStore(
    (s) => s.default_skip_existing,
  );
  const libraryView = useSettingsStore((s) => s.library_view);
  const sidebarCollapsed = useSettingsStore((s) => s.sidebar_collapsed);
  const webEventsEnabled = useSettingsStore((s) => s.web_events_enabled);

  const setLanguage = useSettingsStore((s) => s.setLanguage);
  const setAllowAdult = useSettingsStore((s) => s.setAllowAdult);
  const setDefaultIncludeUnhealthy = useSettingsStore(
    (s) => s.setDefaultIncludeUnhealthy,
  );
  const setDefaultFormat = useSettingsStore(
    (s) => s.setDefaultPackagingFormat,
  );
  const setDefaultPriority = useSettingsStore((s) => s.setDefaultPriority);
  const setDefaultSkipExisting = useSettingsStore(
    (s) => s.setDefaultSkipExisting,
  );
  const setLibraryView = useSettingsStore((s) => s.setLibraryView);
  const setSidebarCollapsed = useSettingsStore((s) => s.setSidebarCollapsed);
  const setWebEventsEnabled = useSettingsStore((s) => s.setWebEventsEnabled);
  const resetSettings = useSettingsStore((s) => s.reset);

  // -------------------------------------------------------------------
  // Reset-to-defaults — one-shot confirmation
  // -------------------------------------------------------------------
  const handleReset = useCallback(() => {
    if (typeof window === "undefined") {
      return;
    }
    const confirmed = window.confirm(
      "Restore all preferences to their default values?\n\n" +
        "This affects every setting on this page — language, adult " +
        "content, download defaults, library view, sidebar state and " +
        "live updates. Your account and server settings are not affected.",
    );
    if (confirmed) {
      resetSettings();
    }
  }, [resetSettings]);

  // -------------------------------------------------------------------
  // Sign out
  // -------------------------------------------------------------------
  const handleSignOut = useCallback(() => {
    if (typeof window === "undefined") {
      return;
    }
    const confirmed = window.confirm("Sign out of NexusDL?");
    if (!confirmed) {
      return;
    }

    // Tear down every client-side singleton that holds per-session
    // state.  Order matters: stop the WebSocket first (so no event
    // arrives mid-teardown), then clear the API client, then the
    // tokens, then the downloads store.
    resetWebSocketClient();
    resetApiClient();
    clearAuthTokens();
    useDownloadsStore.getState().reset();

    router.replace("/auth/login");
    router.refresh();
  }, [router]);

  // -------------------------------------------------------------------
  // Version info (About)
  // -------------------------------------------------------------------
  const versionQuery = useQuery<VersionResponse, NexusApiError>({
    queryKey: ["version"],
    queryFn: () => api.getVersion(),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: false,
  });

  // -------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
      {/* Header ---------------------------------------------- */}
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="neon-text text-2xl font-bold tracking-tight sm:text-3xl">
            Settings
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Preferences are saved automatically to this browser.
          </p>
        </div>
        <button
          type="button"
          onClick={handleReset}
          aria-label="Restore all preferences to their default values"
          title="Restore all preferences to their default values"
          className={cn(
            "inline-flex h-9 items-center gap-1.5 rounded-md",
            "border bg-background px-3 text-sm font-medium",
            "text-muted-foreground transition-colors",
            "hover:bg-accent hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
          )}
        >
          <RotateCcw className="h-4 w-4" aria-hidden />
          <span className="hidden sm:inline">Reset to defaults</span>
          <span className="sm:hidden">Reset</span>
        </button>
      </header>

      {/* Appearance ------------------------------------------ */}
      <Section
        title="Appearance"
        description="How NexusDL looks and behaves in your browser."
      >
        <FormRow
          label="Interface language"
          description="Controls the language of every label, button and message."
          htmlFor="pref-language"
        >
          <select
            id="pref-language"
            value={language}
            onChange={(event) =>
              setLanguage(event.target.value as UILocale)
            }
            className={nativeSelectClasses}
          >
            {UI_LOCALES.map((locale) => (
              <option key={locale} value={locale}>
                {LOCALE_LABELS[locale]}
              </option>
            ))}
          </select>
        </FormRow>

        <FormRow
          label="Sidebar state"
          description="The sidebar can be expanded (icons and labels) or collapsed (icons only)."
        >
          <SegmentedControl
            value={sidebarCollapsed ? "collapsed" : "expanded"}
            onChange={(value) =>
              setSidebarCollapsed(value === "collapsed")
            }
            options={[
              { value: "expanded", label: "Expanded" },
              { value: "collapsed", label: "Collapsed" },
            ]}
            ariaLabel="Sidebar state"
          />
        </FormRow>

        <FormRow
          label="Library layout"
          description="Default layout used by the /library page."
        >
          <SegmentedControl<LibraryView>
            value={libraryView}
            onChange={setLibraryView}
            options={[
              { value: "grid", label: "Grid" },
              { value: "list", label: "List" },
            ]}
            ariaLabel="Library layout"
          />
        </FormRow>
      </Section>

      {/* Content --------------------------------------------- */}
      <Section
        title="Content"
        description="What NexusDL shows you across the app."
      >
        <FormRow
          label="Adult content"
          description="Show sites flagged adult and series rated erotica or pornographic. Off by default."
        >
          <Toggle
            checked={allowAdult}
            onChange={setAllowAdult}
            ariaLabel="Show adult content"
          />
        </FormRow>

        <FormRow
          label="Include unhealthy sites"
          description="By default, searches skip sites whose health is down or degraded. Enable to search them anyway."
        >
          <Toggle
            checked={defaultIncludeUnhealthy}
            onChange={setDefaultIncludeUnhealthy}
            ariaLabel="Include unhealthy sites in search"
          />
        </FormRow>
      </Section>

      {/* Downloads ------------------------------------------- */}
      <Section
        title="Downloads"
        description="Defaults applied to every new download task."
      >
        <FormRow
          label="Packaging format"
          description="How chapters are packaged after download."
          htmlFor="pref-format"
        >
          <select
            id="pref-format"
            value={defaultFormat}
            onChange={(event) =>
              setDefaultFormat(event.target.value as PackagingFormat)
            }
            className={nativeSelectClasses}
          >
            {PACKAGING_FORMATS.map((format) => (
              <option key={format} value={format}>
                {FORMAT_LABELS[format]}
              </option>
            ))}
          </select>
        </FormRow>

        <FormRow
          label="Queue priority"
          description="Where new tasks sit relative to other downloads."
          htmlFor="pref-priority"
        >
          <select
            id="pref-priority"
            value={defaultPriority}
            onChange={(event) =>
              setDefaultPriority(event.target.value as DownloadPriority)
            }
            className={nativeSelectClasses}
          >
            {DOWNLOAD_PRIORITIES.map((priority) => (
              <option key={priority} value={priority}>
                {PRIORITY_LABELS[priority]}
              </option>
            ))}
          </select>
        </FormRow>

        <FormRow
          label="Skip chapters already on disk"
          description="Chapters present in the library database are not downloaded again."
        >
          <Toggle
            checked={defaultSkipExisting}
            onChange={setDefaultSkipExisting}
            ariaLabel="Skip chapters already on disk"
          />
        </FormRow>
      </Section>

      {/* Live updates ---------------------------------------- */}
      <Section
        title="Live updates"
        description="Real-time progress for downloads and library changes."
      >
        <FormRow
          label="Enable WebSocket"
          description="When off, download progress and library changes are only refreshed on demand. Useful on metered connections."
        >
          <Toggle
            checked={webEventsEnabled}
            onChange={setWebEventsEnabled}
            ariaLabel="Enable live WebSocket updates"
          />
        </FormRow>
      </Section>

      {/* Session --------------------------------------------- */}
      <Section
        title="Session"
        description="Sign out of this browser."
      >
        <FormRow
          label="Sign out"
          description="Clears your session tokens and disconnects live updates. Your preferences on this page are preserved."
        >
          <button
            type="button"
            onClick={handleSignOut}
            className={cn(
              "inline-flex h-9 items-center gap-1.5 rounded-md px-3",
              "border border-destructive/40 bg-destructive/10",
              "text-sm font-medium text-destructive",
              "transition-colors hover:bg-destructive/20",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-destructive/50 focus-visible:ring-offset-2",
              "focus-visible:ring-offset-background",
            )}
          >
            <LogOut className="h-4 w-4" aria-hidden />
            <span>Sign out</span>
          </button>
        </FormRow>
      </Section>

      {/* Server settings (admin) ----------------------------- */}
      <ServerSettingsSection />

      {/* About ----------------------------------------------- */}
      <Section title="About" description="Build information.">
        <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-3">
          <AboutItem
            label="Version"
            value={versionQuery.data?.version ?? "—"}
            loading={versionQuery.isLoading}
          />
          <AboutItem
            label="Commit"
            value={
              versionQuery.data?.git_commit?.slice(0, 8) ?? "—"
            }
            loading={versionQuery.isLoading}
          />
          <AboutItem
            label="Built"
            value={versionQuery.data?.build_date ?? "—"}
            loading={versionQuery.isLoading}
          />
        </dl>
      </Section>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Admin section
// ───────────────────────────────────────────────────────────────────────────

/**
 * The admin-only "Server settings" section.
 *
 * Loads the full settings snapshot on first expand; shows a JSON editor.
 * The PATCH body is the **entire edited object** — the backend's
 * :class:`Settings` model accepts a partial update via its own merge
 * semantics, but sending the full snapshot is simpler to reason about and
 * matches what the API documents ("secrets are masked").
 *
 * Behaviour per state:
 *
 * * **Collapsed** — a button that expands the section.  No request is
 *   made until the user expands.
 * * **Loading** — spinner.
 * * **Forbidden / unauthorized** — a clear "requires admin" notice.
 * * **Success** — textarea with the current snapshot + Save / Discard.
 * * **Save error** — inline error banner, textarea stays editable.
 *
 * The secrets warning is prominent: the backend masks any secret value
 * (typically as ``"***"`` or a similar placeholder).  Saving unchanged
 * will preserve those values — the backend recognises the placeholder and
 * skips them.  Overwriting a placeholder with a real value is the correct
 * way to change a secret.
 */
function ServerSettingsSection() {
  const [expanded, setExpanded] = useState(false);
  const [editedJson, setEditedJson] = useState<string>("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const queryClient = useQueryClient();

  const settingsQuery = useQuery<SettingsSnapshot, NexusApiError>({
    queryKey: ["server-settings"],
    queryFn: () => api.getSettings(),
    enabled: expanded,
    staleTime: 60 * 1000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  // Seed the textarea from the fetched settings once per successful load.
  // Subsequent edits are local until the user saves or discards.
  useEffect(() => {
    if (settingsQuery.data !== undefined && editedJson === "") {
      setEditedJson(JSON.stringify(settingsQuery.data, null, 2));
    }
  }, [settingsQuery.data, editedJson]);

  // Reset the textarea + status when the section is collapsed, so a
  // re-expand starts from a fresh load.
  useEffect(() => {
    if (!expanded) {
      setEditedJson("");
      setSaveError(null);
      setSavedAt(null);
    }
  }, [expanded]);

  const handleSave = useCallback(async () => {
    setSaveError(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(editedJson);
    } catch (err) {
      setSaveError(
        `Invalid JSON: ${err instanceof Error ? err.message : String(err)}`,
      );
      return;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      setSaveError("The settings object must be a JSON object.");
      return;
    }
    try {
      const updated = await api.updateSettings(
        parsed as SettingsSnapshot,
      );
      // Reformat the returned snapshot so masked secrets and server
      // normalisations are reflected in the textarea.
      setEditedJson(JSON.stringify(updated, null, 2));
      queryClient.setQueryData(["server-settings"], updated);
      setSavedAt(Date.now());
    } catch (err) {
      const apiError =
        err instanceof NexusApiError
          ? err
          : new NexusApiError(
              err instanceof Error ? err.message : String(err),
              { code: "INTERNAL_ERROR", status: 0, cause: err },
            );
      setSaveError(apiError.message);
    }
  }, [editedJson, queryClient]);

  const handleDiscard = useCallback(() => {
    if (settingsQuery.data !== undefined) {
      setEditedJson(JSON.stringify(settingsQuery.data, null, 2));
    }
    setSaveError(null);
  }, [settingsQuery.data]);

  // Detect the "admin only" case from the error envelope.
  const isForbidden =
    settingsQuery.error !== null &&
    (settingsQuery.error.code === "FORBIDDEN" ||
      settingsQuery.error.status === 403);
  const isUnauthorized =
    settingsQuery.error !== null &&
    (settingsQuery.error.code === "UNAUTHORIZED" ||
      settingsQuery.error.status === 401);

  const showEditor =
    expanded && !isForbidden && !isUnauthorized && settingsQuery.data !== undefined;

  const isDirty =
    settingsQuery.data !== undefined &&
    editedJson !== "" &&
    editedJson.trim() !== JSON.stringify(settingsQuery.data, null, 2).trim();

  return (
    <section
      aria-labelledby="server-settings-heading"
      className={cn(
        "flex flex-col gap-3 rounded-lg border bg-card p-4",
        "text-card-foreground shadow-sm",
      )}
    >
      <header className="flex items-start justify-between gap-3">
        <div>
          <h2
            id="server-settings-heading"
            className="flex items-center gap-2 text-base font-semibold"
          >
            <Server className="h-4 w-4 text-muted-foreground" aria-hidden />
            Server settings
          </h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Backend configuration shared by every user. Admin only.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-controls="server-settings-body"
          className={cn(
            "inline-flex h-8 items-center gap-1 rounded-md px-2",
            "text-xs font-medium text-muted-foreground",
            "hover:bg-accent hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
          )}
        >
          <span>{expanded ? "Hide" : "Show"}</span>
          <ChevronDown
            className={cn(
              "h-3.5 w-3.5 transition-transform",
              expanded && "rotate-180",
            )}
            aria-hidden
          />
        </button>
      </header>

      {expanded && (
        <div id="server-settings-body" className="flex flex-col gap-3">
          {settingsQuery.isLoading && (
            <div
              className="flex items-center gap-2 text-sm text-muted-foreground"
              aria-live="polite"
            >
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              Loading server settings…
            </div>
          )}

          {(isForbidden || isUnauthorized) && (
            <div
              role="status"
              className={cn(
                "flex items-start gap-2 rounded-md border",
                "border-amber-500/30 bg-amber-500/10 p-3",
                "text-sm text-amber-700 dark:text-amber-400",
              )}
            >
              <ShieldAlert
                className="mt-0.5 h-4 w-4 shrink-0"
                aria-hidden
              />
              <p>
                {isUnauthorized
                  ? "Sign in with an admin account to edit server settings."
                  : "Your account does not have permission to view or edit server settings."}
              </p>
            </div>
          )}

          {settingsQuery.error !== null &&
            !isForbidden &&
            !isUnauthorized && (
              <div
                role="alert"
                className={cn(
                  "flex items-start gap-2 rounded-md border",
                  "border-destructive/30 bg-destructive/10 p-3",
                  "text-sm text-destructive",
                )}
              >
                <AlertCircle
                  className="mt-0.5 h-4 w-4 shrink-0"
                  aria-hidden
                />
                <div className="flex-1">
                  <p className="font-medium">
                    Could not load server settings
                  </p>
                  <p className="mt-0.5 text-xs opacity-80">
                    {settingsQuery.error.message}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void settingsQuery.refetch()}
                  className={cn(
                    "rounded px-2 py-1 text-xs font-medium",
                    "hover:bg-destructive/20",
                    "focus-visible:outline-none focus-visible:ring-2",
                    "focus-visible:ring-destructive/50",
                  )}
                >
                  Retry
                </button>
              </div>
            )}

          {showEditor && (
            <>
              {/* Secrets warning -------------------------------- */}
              <div
                role="note"
                className={cn(
                  "flex items-start gap-2 rounded-md border",
                  "border-amber-500/30 bg-amber-500/10 p-2.5",
                  "text-xs text-amber-700 dark:text-amber-400",
                )}
              >
                <AlertCircle
                  className="mt-0.5 h-3.5 w-3.5 shrink-0"
                  aria-hidden
                />
                <p>
                  Secret values are masked by the backend (typically as{" "}
                  <code className="rounded bg-black/20 px-1 font-mono">
                    ***
                  </code>
                  ). Leave them unchanged to preserve the current value;
                  replace a masked field with a real value to change the
                  secret.  Removing a field will revert it to its default.
                </p>
              </div>

              {/* Editor --------------------------------------- */}
              <label
                htmlFor="server-settings-editor"
                className="text-sm font-medium"
              >
                Server settings JSON
              </label>
              <textarea
                id="server-settings-editor"
                value={editedJson}
                onChange={(event) => setEditedJson(event.target.value)}
                spellCheck={false}
                autoCorrect="off"
                autoCapitalize="off"
                rows={20}
                className={cn(
                  "w-full resize-y rounded-md border bg-background",
                  "p-3 font-mono text-xs leading-relaxed",
                  "focus-visible:outline-none focus-visible:ring-2",
                  "focus-visible:ring-ring focus-visible:ring-offset-2",
                  "focus-visible:ring-offset-background",
                )}
              />

              {saveError !== null && (
                <div
                  role="alert"
                  className={cn(
                    "flex items-start gap-2 rounded-md border",
                    "border-destructive/30 bg-destructive/10 p-2.5",
                    "text-xs text-destructive",
                  )}
                >
                  <AlertCircle
                    className="mt-0.5 h-3.5 w-3.5 shrink-0"
                    aria-hidden
                  />
                  <span>{saveError}</span>
                </div>
              )}

              <div className="flex items-center justify-between gap-2">
                <span
                  className="text-xs text-muted-foreground"
                  aria-live="polite"
                >
                  {savedAt !== null
                    ? "Saved just now."
                    : isDirty
                      ? "Unsaved changes."
                      : "No changes to save."}
                </span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={handleDiscard}
                    disabled={!isDirty}
                    className={cn(
                      "inline-flex h-8 items-center gap-1 rounded-md px-2",
                      "text-xs font-medium text-muted-foreground",
                      "hover:bg-accent hover:text-foreground",
                      "focus-visible:outline-none focus-visible:ring-2",
                      "focus-visible:ring-ring",
                      "disabled:cursor-not-allowed disabled:opacity-50",
                    )}
                  >
                    <X className="h-3.5 w-3.5" aria-hidden />
                    Discard
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleSave()}
                    disabled={!isDirty || settingsQuery.isFetching}
                    className={cn(
                      "inline-flex h-8 items-center gap-1 rounded-md px-3",
                      "bg-primary text-xs font-medium text-primary-foreground",
                      "hover:bg-primary/90",
                      "focus-visible:outline-none focus-visible:ring-2",
                      "focus-visible:ring-ring focus-visible:ring-offset-2",
                      "focus-visible:ring-offset-background",
                      "disabled:cursor-not-allowed disabled:opacity-50",
                    )}
                  >
                    {settingsQuery.isFetching ? (
                      <Loader2
                        className="h-3.5 w-3.5 animate-spin"
                        aria-hidden
                      />
                    ) : (
                      <Check className="h-3.5 w-3.5" aria-hidden />
                    )}
                    Save changes
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Reusable sub-components
// ───────────────────────────────────────────────────────────────────────────

/** Shared class string for native ``<select>`` elements on this page. */
const nativeSelectClasses = cn(
  "h-9 rounded-md border bg-background px-3 text-sm",
  "focus-visible:outline-none focus-visible:ring-2",
  "focus-visible:ring-ring focus-visible:ring-offset-2",
  "focus-visible:ring-offset-background",
  "disabled:cursor-not-allowed disabled:opacity-50",
);

/**
 * A section wrapper — the visual unit of this page.
 *
 * Every section has the same structure: a heading, a one-line
 * description, then a series of :func:`FormRow` children inside a card.
 * The consistent rhythm is what makes the page scannable.
 */
function Section({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className={cn(
        "flex flex-col gap-3 rounded-lg border bg-card p-4",
        "text-card-foreground shadow-sm",
      )}
    >
      <div>
        <h2 className="text-base font-semibold">{title}</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {description}
        </p>
      </div>
      <div className="flex flex-col divide-y divide-border">
        {children}
      </div>
    </section>
  );
}

/**
 * A single row inside a :func:`Section`.
 *
 * The label and description occupy the flexible left column; the control
 * occupies the shrink-0 right column.  On narrow screens the two stack
 * vertically.
 *
 * When ``htmlFor`` is provided, the label is a ``<label>`` bound to that
 * control id — the accessible name of the control is the row's label.
 * When the control is a composite (a toggle button, a segmented control),
 * the caller is expected to give that control its own ``aria-label``; the
 * row's label is then purely visual and the ``<span>`` element is used.
 */
function FormRow({
  label,
  description,
  htmlFor,
  children,
}: {
  label: string;
  description?: string;
  htmlFor?: string;
  children: ReactNode;
}) {
  const descriptionId = useId();

  return (
    <div className="flex flex-col gap-2 py-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <div className="min-w-0 flex-1">
        {htmlFor !== undefined ? (
          <label
            htmlFor={htmlFor}
            className="text-sm font-medium"
          >
            {label}
          </label>
        ) : (
          <span className="text-sm font-medium">{label}</span>
        )}
        {description !== undefined && (
          <p
            id={descriptionId}
            className="mt-0.5 text-xs text-muted-foreground"
          >
            {description}
          </p>
        )}
      </div>
      <div className="shrink-0 sm:pt-0.5">{children}</div>
    </div>
  );
}

/**
 * An accessible toggle switch.
 *
 * Renders as a ``<button role="switch" aria-checked={…}>`` — the standard
 * ARIA pattern for a two-state control.  The ``aria-label`` is required so
 * that a screen reader announces what the switch controls; it is passed
 * by the caller rather than inherited from an adjacent ``<label>``
 * because the label lives in a different row of the DOM and would not
 * label the button natively.
 */
function Toggle({
  checked,
  onChange,
  disabled = false,
  ariaLabel,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  ariaLabel: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full",
        "transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2",
        "focus-visible:ring-ring focus-visible:ring-offset-2",
        "focus-visible:ring-offset-background",
        "disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "bg-primary" : "bg-muted",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "inline-block h-5 w-5 transform rounded-full",
          "bg-foreground shadow-sm transition-transform duration-150",
          checked ? "translate-x-[22px]" : "translate-x-0.5",
        )}
      />
    </button>
  );
}

/**
 * A segmented control — a compact radio group rendered as connected
 * buttons.
 *
 * The outer container has ``role="radiogroup"`` and each button has
 * ``role="radio"`` with ``aria-checked``.  This is the WAI-ARIA pattern
 * for a group of mutually-exclusive options presented as buttons.
 *
 * Generic over the value type so that the ``libraryView`` control can
 * pass ``"grid" | "list"`` without casts.
 */
function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  disabled = false,
}: {
  value: T;
  onChange: (value: T) => void;
  options: ReadonlyArray<{ value: T; label: string }>;
  ariaLabel: string;
  disabled?: boolean;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        "inline-flex items-center rounded-md border bg-background p-0.5",
        "text-xs font-medium",
      )}
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            className={cn(
              "inline-flex h-7 items-center rounded px-2.5",
              "transition-colors",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring focus-visible:ring-offset-1",
              "focus-visible:ring-offset-background",
              "disabled:cursor-not-allowed disabled:opacity-50",
              selected
                ? "bg-primary text-primary-foreground shadow-[0_0_8px_hsl(var(--primary)/0.5)]"
                : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * A single item in the About section — a label / value pair.
 *
 * Rendered as ``<dt>``/``<dd>`` inside the parent ``<dl>`` (which lives in
 * the About :func:`Section` above).
 */
function AboutItem({
  label,
  value,
  loading,
}: {
  label: string;
  value: string;
  loading: boolean;
}) {
  return (
    <div className="flex flex-col gap-0.5 rounded-md border bg-background p-3">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd
        className={cn(
          "truncate font-mono text-sm",
          loading && "animate-pulse text-muted-foreground",
        )}
        title={value}
      >
        {loading ? "…" : value}
      </dd>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Skeleton
// ───────────────────────────────────────────────────────────────────────────

/**
 * Full-page skeleton shown until the settings store hydrates.
 *
 * Mirrors the page's structure — header, three form sections, session
 * section — so there is no layout shift when the real content replaces
 * it.
 */
function SettingsPageSkeleton() {
  return (
    <div
      className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-4 sm:p-6 lg:p-8"
      aria-busy="true"
      aria-live="polite"
    >
      <span className="sr-only">Loading settings…</span>
      <div className="space-y-2">
        <div className="h-9 w-40 animate-pulse rounded bg-muted" />
        <div className="h-4 w-64 animate-pulse rounded bg-muted" />
      </div>
      {[0, 1, 2].map((sectionIndex) => (
        <div
          key={sectionIndex}
          className="flex flex-col gap-3 rounded-lg border bg-card p-4"
        >
          <div className="h-5 w-32 animate-pulse rounded bg-muted" />
          {[0, 1, 2].map((rowIndex) => (
            <div
              key={rowIndex}
              className="flex items-center justify-between gap-4 py-3"
            >
              <div className="flex flex-1 flex-col gap-1">
                <div className="h-4 w-40 animate-pulse rounded bg-muted" />
                <div className="h-3 w-64 animate-pulse rounded bg-muted" />
              </div>
              <div className="h-8 w-24 animate-pulse rounded bg-muted" />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
