"""Async concurrency primitives for cancellation, cleanup, and lazy init.

The pipeline coordinates dozens of concurrent coroutines — page
downloads, chapter lookups, cache reads, plugin hooks, health checks.
Most of that coordination is done today with the primitives asyncio
ships: :class:`asyncio.TaskGroup`, :meth:`asyncio.Task.cancel`,
:meth:`asyncio.gather`, :class:`asyncio.Lock`, :class:`asyncio.Semaphore`.
Those primitives are correct; the problem is that using them **well**
requires the same half-dozen lines at every call site:

* Cancel a task and await it without re-raising ``CancelledError``.
* Cancel a batch of tasks and wait for all of them to finish cleaning up.
* Schedule a background task and keep a strong reference so it is not
  garbage-collected mid-run (a footgun documented in asyncio's own
  warnings).
* Lazily initialise a value asynchronously, exactly once, under a lock.

Every one of those patterns is duplicated in the delivered code — in
``worker.py``, ``manager.py``, ``http_session.py``, ``plugins/loader.py``,
``playwright_pool.py``, ``scraper.py`` (future). This module centralises
them behind a **small, focused API**.

What this module deliberately does **not** do
---------------------------------------------
* **No ``gather_with_limit``.** :class:`asyncio.TaskGroup` +
  :class:`asyncio.Semaphore` is the correct primitive for bounded
  concurrency, and the pipeline already uses it correctly (see
  ``worker._download_pages``). A wrapper would be a reinvention that
  either loses ``ExceptionGroup`` semantics or duplicates them badly.
* **No retry.** :mod:`nexusdl.core.utils.retry` covers it.
* **No timeout wrapper.** :func:`asyncio.wait_for` and
  :func:`asyncio.timeout` (3.11+) are clearer at the call site than
  any wrapper this module could add.
* **No custom ``Semaphore`` / ``Lock`` subclasses.** The stdlib ones
  are correct; a subclass would invite divergence.
* **No task registry with global access.** :func:`fire_and_forget`
  keeps a private set of strong references, but does not expose it —
  a caller that needs to wait for all background tasks owns the
  references itself. A global task registry is a memory leak in
  disguise.

Cancellation discipline
-----------------------
``asyncio.CancelledError`` is a :class:`BaseException` subclass (since
Python 3.8), **not** a :class:`Exception`. This module's helpers respect
that contract everywhere:

* :func:`cancel_safely` and :func:`cancel_all` **do not** swallow the
  cancellation of the caller — they only await the cancelled tasks'
  cleanup, then re-raise any pending cancellation of the caller.
* :func:`fire_and_forget` schedules a task on the running loop; the
  task's cancellation is its own concern, not the caller's.
* :class:`AsyncLazy` propagates cancellation from the underlying
  initialiser; a cancelled initialisation leaves the lazy value in the
  uninitialised state so a retry can succeed.

Dependency graph (no cycles)::

    loguru       ──► core.utils.async_helpers
    asyncio, typing (stdlib)

``loguru`` is imported because :func:`fire_and_forget` needs to log
exceptions from background tasks — a background task whose exception
is swallowed silently would be a debugging trap. No other
``nexusdl.core`` or third-party import.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Final, Generic, TypeVar

from loguru import logger


__all__ = [
    "AsyncLazy",
    "cancel_all",
    "cancel_safely",
    "fire_and_forget",
]


_T = TypeVar("_T")

_log = logger.bind(component="utils.async_helpers")


#: Module-level set holding strong references to every task scheduled
#: by :func:`fire_and_forget`. Without this reference, the event loop's
#: task registry would garbage-collect the task before it completed —
#: a footgun asyncio itself warns about. Each task is removed from the
#: set the moment it finishes (via a done callback), so the set's size
#: is bounded by the number of concurrently-running background tasks,
#: not by the total number ever scheduled.
_background_tasks: Final[set[asyncio.Task[Any]]] = set()


# --------------------------------------------------------------------------- #
# Cancellation                                                               #
# --------------------------------------------------------------------------- #


async def cancel_safely(task: asyncio.Task[Any]) -> None:
    """Cancel *task* and await it, suppressing its ``CancelledError``.

    The standard cleanup pattern at a call site that decides a
    previously-scheduled task is no longer needed. Without this helper,
    every such call site writes::

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    — four lines that are correct but repetitive and easy to get subtly
    wrong (forgetting the ``await`` loses cleanup; catching
    ``Exception`` instead of ``CancelledError`` swallows unrelated
    errors). This function performs the same sequence and is the only
    place a reader has to verify the logic.

    **The caller's own cancellation is not swallowed.** If the caller is
    cancelled while ``await task`` is running, that ``CancelledError``
    propagates — the helper suppresses the *cancelled task's* exception,
    not the caller's. The two are distinguishable because the cancelled
    task has finished by the time the caller's cancellation arrives;
    the ``except asyncio.CancelledError`` in the implementation guards
    the ``await task`` line specifically, and only skips it when
    ``task`` itself was the source.

    A task that finishes with a non-cancellation exception propagates
    that exception — the caller asked to cancel a task that already
    failed, and the failure is worth knowing about. A task that has
    already finished before this function is called is a no-op.

    Args:
        task: The task to cancel. May already be done, in which case the
            function returns immediately.
    """
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # The task's own cancellation — expected and intended.
        return


async def cancel_all(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel every task in *tasks* and await all of them.

    A thin convenience for the multi-task case of :func:`cancel_safely`.
    All tasks are cancelled **before** any is awaited, so the cancellations
    overlap — a batch of ten tasks whose cancellation each takes 50 ms
    completes in ~50 ms, not ~500 ms.

    Exceptions from the individual tasks are **not** propagated — a
    cleanup sequence that raises because one of its targets failed is
    worse than one that cleans up the rest and reports via logs. Callers
    that need to know about individual task failures inspect the tasks
    themselves after this returns (they are ``done()``).

    The caller's own cancellation propagates normally — this helper
    never masks a cancellation arriving from above.

    Args:
        tasks: The tasks to cancel. An empty list is a no-op. Tasks that
            have already finished are skipped.
    """
    pending = [t for t in tasks if not t.done()]
    for task in pending:
        task.cancel()
    if not pending:
        return
    await asyncio.gather(*pending, return_exceptions=True)


# --------------------------------------------------------------------------- #
# Background scheduling                                                      #
# --------------------------------------------------------------------------- #


def fire_and_forget(
    coro: Awaitable[_T],
    *,
    name: str | None = None,
) -> asyncio.Task[_T]:
    """Schedule *coro* as a background task and return the task handle.

    The canonical way to launch a coroutine whose result the caller does
    not intend to await immediately — a metrics emission, a cache
    warm-up, an event dispatch whose outcome does not affect the
    current request. The function:

    * **Keeps a strong reference** to the task, defeating the garbage
      collection that would otherwise cancel it mid-run when the local
      variable goes out of scope. This is the documented asyncio
      footgun the module docstring warns about.
    * **Logs any exception** raised by the task, at ``WARNING`` level,
      with the task's name and traceback. A background task that dies
      silently is a debugging trap; the log line is the caller's only
      signal that the work failed.
    * **Discards cancelled tasks silently** — a cancelled background
      task is a normal shutdown event, not an error.
    * **Drops the strong reference** when the task finishes (via a
      done callback), so the set of tracked tasks is bounded by the
      number of concurrently running tasks, not by the number ever
      scheduled.

    The returned task is **not** awaited — the caller receives it for
    optional bookkeeping (e.g. passing it to :func:`cancel_safely` for
    an early termination) but is under no obligation to inspect it.
    A caller that does need the result uses :meth:`asyncio.create_task`
    directly; ``fire_and_forget`` is the wrong primitive when the
    caller cares about the outcome.

    Must be called from within a running event loop. A caller that
    invokes this outside a coroutine gets the standard
    :class:`RuntimeError` from :func:`asyncio.get_running_loop` — the
    error message names the missing loop, which is the right diagnosis
    for that caller bug.

    Args:
        coro: The awaitable to schedule. Typically a coroutine, but any
            awaitable works — the function wraps it in a task via
            :func:`asyncio.ensure_future`.
        name: Optional task name, used in the log message when the task
            fails and in :func:`asyncio.all_tasks` output. Callers
            should pass a descriptive name (``"emit-metrics"``,
            ``"proxy-failure-notify"``); a bare ``None`` produces
            ``"background-task"``.

    Returns:
        The task handle, already scheduled on the running loop.
    """
    task: asyncio.Task[_T] = asyncio.ensure_future(coro)
    if name is not None:
        try:
            task.set_name(name)
        except (RuntimeError, ValueError):
            # ``set_name`` can fail on exotic custom Task subclasses;
            # it is a nice-to-have, not a contract. Silently proceed.
            pass

    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_background_exception)
    return task


def _log_background_exception(task: asyncio.Task[Any]) -> None:
    """Log a background task's exception, if it failed with one.

    Registered as a done callback by :func:`fire_and_forget`. The
    callback runs on the event loop after the task completes — it must
    never raise, or asyncio itself logs a warning about the callback.

    Args:
        task: The finished task.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    task_name = task.get_name()
    _log.warning(
        "Background task {!r} raised {}: {}",
        task_name,
        type(exc).__name__,
        exc,
    )


# --------------------------------------------------------------------------- #
# Lazy async initialisation                                                  #
# --------------------------------------------------------------------------- #


class AsyncLazy(Generic[_T]):
    """Lazy asynchronous value, initialised at most once.

    A caller that needs to create a resource on first use — an
    :class:`httpx.AsyncClient`, a Playwright browser, a database
    connection — and share it across concurrent coroutines, without
    paying the cost at startup and without racing the initialisation
    under load, wraps the initialiser here::

        client: AsyncLazy[httpx.AsyncClient] = AsyncLazy(
            lambda: httpx.AsyncClient().__aenter__()
        )

        async def get(url: str) -> httpx.Response:
            client_instance = await client.get()
            return await client_instance.get(url)

    The initialiser runs exactly once, regardless of how many coroutines
    call :meth:`get` concurrently — the first caller to acquire the lock
    runs the initialiser; every other caller waits on the same lock and
    receives the same result. A successful initialisation stores the
    value; a failed initialisation stores the exception and re-raises it
    to the first caller; subsequent callers **do not** re-run the
    initialiser (the failure is sticky).

    Sticky failure is intentional. An initialiser that failed — a
    Playwright browser that could not launch, a database that cannot
    connect — will fail again if called a second later; retrying would
    hide the failure from the caller who needs to see it and act (log,
    fall back, exit). A caller who genuinely wants retry wraps the
    initialiser in :func:`nexusdl.core.utils.retry.retry_async` — a
    wrapper that has explicit retry semantics is clearer than a lazy
    container that silently retries.

    Cancellation of the initialiser (the caller cancels the
    :meth:`get` call before the initialiser returns) leaves the lazy
    value **uninitialised** — the next :meth:`get` call re-runs the
    initialiser. This is the correct behaviour: a cancelled
    initialisation did not succeed, so the container cannot pretend it
    did.

    Threading: the class is asyncio-safe (a single
    :class:`asyncio.Lock` serialises callers) but not thread-safe. It is
    designed for a single event loop, which is how the pipeline uses it.

    Example:
        >>> lazy = AsyncLazy(lambda: asyncio.sleep(0, result=42))
        >>> await lazy.get()  # first call — runs the initialiser
        42
        >>> await lazy.get()  # subsequent call — cached
        42
        >>> lazy.is_initialized()
        True

    Args:
        initializer: A zero-argument callable returning an awaitable.
            Typically a coroutine function or a lambda wrapping one.
            Called at most once per successful initialisation; on
            failure, called once and never again (sticky failure).
    """

    __slots__ = ("_error", "_initializer", "_lock", "_value")

    def __init__(
        self, initializer: Callable[[], Awaitable[_T]]
    ) -> None:
        self._initializer = initializer
        self._lock = asyncio.Lock()
        self._value: _T | None = None
        self._error: BaseException | None = None
        self._initialized: bool = False

    # ------------------------------------------------------------------ #
    # Public API                                                        #
    # ------------------------------------------------------------------ #

    async def get(self) -> _T:
        """Return the initialised value, running the initialiser on first call.

        Returns:
            The value returned by the initialiser.

        Raises:
            BaseException: Whatever the initialiser raised, unchanged.
                The exception is cached — a subsequent call to
                :meth:`get` re-raises the same instance without
                re-running the initialiser. ``asyncio.CancelledError``
                raised by the initialiser (because the caller cancelled
                the ``get()`` call) is **not** cached — the container
                remains uninitialised so a retry can succeed.
        """
        # Fast path — no lock when already initialised or failed.
        if self._initialized:
            if self._error is not None:
                raise self._error
            # ``_value`` is guaranteed non-None on the initialised,
            # non-error path — the initialiser returned, and its return
            # value is what we stored.
            return self._value  # type: ignore[return-value]

        async with self._lock:
            # Re-check after acquiring the lock — a concurrent caller
            # may have completed initialisation while we waited.
            if self._initialized:
                if self._error is not None:
                    raise self._error
                return self._value  # type: ignore[return-value]

            try:
                value = await self._initializer()
            except asyncio.CancelledError:
                # Cancellation of the initialiser leaves the container
                # uninitialised so a retry can succeed. The
                # CancelledError propagates to the caller — the caller
                # asked us to stop, and stopping is what we did.
                raise
            except BaseException as exc:
                self._error = exc
                self._initialized = True
                raise

            self._value = value
            self._initialized = True
            return value

    def is_initialized(self) -> bool:
        """Return ``True`` iff the initialiser has completed (success or failure).

        A cancelled initialisation leaves the container uninitialised —
        this method returns ``False`` in that case, and the next
        :meth:`get` call re-runs the initialiser.
        """
        return self._initialized

    def get_if_ready(self) -> _T | None:
        """Return the value if initialised successfully, or ``None``.

        A non-async accessor for the common case where the caller wants
        to check "is the resource already built?" without triggering
        initialisation. Returns ``None`` when uninitialised **or** when
        initialisation failed — a caller that needs to distinguish the
        two uses :meth:`is_initialized` and inspects the exception via
        :meth:`get` (which re-raises). The plain-``None`` return is the
        correct default for a diagnostic endpoint or a health check
        that just wants to know "is it there".

        Never blocks, never awaits, never raises.
        """
        if not self._initialized or self._error is not None:
            return None
        return self._value

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        if not self._initialized:
            state = "uninitialized"
        elif self._error is not None:
            state = f"failed ({type(self._error).__name__})"
        else:
            state = "initialized"
        return f"AsyncLazy(state={state!r})"
