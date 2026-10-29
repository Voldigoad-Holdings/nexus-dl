# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for core.exceptions.

NexusDL uses a structured exception hierarchy:

    NexusDLError (base)
    ├── ConfigError
    ├── ParserError
    │   ├── ParserNotFoundError
    │   ├── ParserResponseError
    │   └── ParserParseError
    ├── DownloadError
    │   ├── ChapterDownloadError
    │   ├── PageDownloadError
    │   └── EmptyChapterError
    ├── PackagingError
    ├── LibraryError
    ├── ComplianceError
    ├── SessionError
    │   ├── SessionTimeoutError
    │   ├── SessionRateLimitError
    │   ├── SessionCloudflareError
    │   ├── CookieEncryptionError
    │   ├── CircuitBreakerOpenError
    │   └── RateLimitTimeoutError
    ├── RetryError
    │   └── RetryTimeoutError
    ├── PluginError
    ├── InvalidStateTransitionError
    ├── ImageValidationError
    └── ImageConversionError

Contract:
    - All exceptions inherit from NexusDLError
    - NexusDLError inherits from Exception
    - __str__ returns a readable message
    - .args contains the message (standard)
    - Optional: .context (dict) for structured data
    - Optional: .to_dict() for JSON logging
    - Chaining via `raise ... from ...` works

What is tested:
    - Hierarchy (issubclass)
    - Instantiation (message, args)
    - __str__ / __repr__
    - .context
    - Chaining
    - Catching as NexusDLError
    - Catching as Exception
    - to_dict() (if implemented)
"""
from __future__ import annotations

import pytest

from nexusdl.core import exceptions as exc


# ────────────────────────────────────────────────────────────────────
# Test 1: Base class
# ────────────────────────────────────────────────────────────────────


def test_nexusdl_error_inherits_exception() -> None:
    """NexusDLError inherits from Exception."""
    assert issubclass(exc.NexusDLError, Exception)


def test_nexusdl_error_instantiation() -> None:
    """NexusDLError can be instantiated with a message."""
    err = exc.NexusDLError("something went wrong")
    assert str(err) == "something went wrong"
    assert err.args == ("something went wrong",)


def test_nexusdl_error_no_args() -> None:
    """NexusDLError can be instantiated with no args."""
    err = exc.NexusDLError()
    assert str(err) == ""
    assert err.args == ()


# ────────────────────────────────────────────────────────────────────
# Test 2: Hierarchy (issubclass)
# ────────────────────────────────────────────────────────────────────


def test_all_exceptions_inherit_base() -> None:
    """All custom exceptions inherit from NexusDLError."""
    exception_classes = [
        exc.ConfigError,
        exc.ParserError,
        exc.ParserNotFoundError,
        exc.ParserResponseError,
        exc.ParserParseError,
        exc.DownloadError,
        exc.ChapterDownloadError,
        exc.PageDownloadError,
        exc.EmptyChapterError,
        exc.PackagingError,
        exc.LibraryError,
        exc.ComplianceError,
        exc.SessionError,
        exc.SessionTimeoutError,
        exc.SessionRateLimitError,
        exc.SessionCloudflareError,
        exc.CookieEncryptionError,
        exc.CircuitBreakerOpenError,
        exc.RateLimitTimeoutError,
        exc.RetryError,
        exc.RetryTimeoutError,
        exc.PluginError,
        exc.InvalidStateTransitionError,
        exc.ImageValidationError,
        exc.ImageConversionError,
    ]

    for cls in exception_classes:
        assert issubclass(cls, exc.NexusDLError), f"{cls.__name__} does not inherit from NexusDLError"


def test_parser_errors_inherit_parser_error() -> None:
    """Parser sub-errors inherit from ParserError."""
    assert issubclass(exc.ParserNotFoundError, exc.ParserError)
    assert issubclass(exc.ParserResponseError, exc.ParserError)
    assert issubclass(exc.ParserParseError, exc.ParserError)


def test_download_errors_inherit_download_error() -> None:
    """Download sub-errors inherit from DownloadError."""
    assert issubclass(exc.ChapterDownloadError, exc.DownloadError)
    assert issubclass(exc.PageDownloadError, exc.DownloadError)
    assert issubclass(exc.EmptyChapterError, exc.DownloadError)


def test_session_errors_inherit_session_error() -> None:
    """Session sub-errors inherit from SessionError."""
    assert issubclass(exc.SessionTimeoutError, exc.SessionError)
    assert issubclass(exc.SessionRateLimitError, exc.SessionError)
    assert issubclass(exc.SessionCloudflareError, exc.SessionError)
    assert issubclass(exc.CookieEncryptionError, exc.SessionError)
    assert issubclass(exc.CircuitBreakerOpenError, exc.SessionError)
    assert issubclass(exc.RateLimitTimeoutError, exc.SessionError)


def test_retry_timeout_inherits_retry_error() -> None:
    """RetryTimeoutError inherits from RetryError."""
    assert issubclass(exc.RetryTimeoutError, exc.RetryError)


# ────────────────────────────────────────────────────────────────────
# Test 3: __str__ / __repr__
# ────────────────────────────────────────────────────────────────────


def test_config_error_str() -> None:
    """ConfigError __str__ returns the message."""
    err = exc.ConfigError("invalid config")
    assert str(err) == "invalid config"


def test_config_error_repr() -> None:
    """ConfigError __repr__ is readable."""
    err = exc.ConfigError("invalid config")
    repr_str = repr(err)
    assert "ConfigError" in repr_str
    assert "invalid config" in repr_str


def test_parser_not_found_str() -> None:
    """ParserNotFoundError __str__ returns the message."""
    err = exc.ParserNotFoundError("site 'xyz' not found")
    assert "xyz" in str(err)
    assert "not found" in str(err)


# ────────────────────────────────────────────────────────────────────
# Test 4: Context (structured data)
# ────────────────────────────────────────────────────────────────────


def test_config_error_context() -> None:
    """ConfigError accepts a context dict."""
    err = exc.ConfigError(
        "invalid config",
        context={"field": "log_level", "value": "VERBOSE"},
    )
    # If context is supported
    if hasattr(err, "context"):
        assert err.context["field"] == "log_level"
        assert err.context["value"] == "VERBOSE"


def test_parser_response_error_context() -> None:
    """ParserResponseError accepts a context dict."""
    err = exc.ParserResponseError(
        "HTTP 500",
        context={"status_code": 500, "url": "https://test.com"},
    )
    if hasattr(err, "context"):
        assert err.context["status_code"] == 500


# ────────────────────────────────────────────────────────────────────
# Test 5: Chaining
# ────────────────────────────────────────────────────────────────────


def test_exception_chaining() -> None:
    """Exceptions can be chained with `raise ... from ...`."""
    try:
        try:
            raise ValueError("original")
        except ValueError as e:
            raise exc.ConfigError("config failed") from e
    except exc.ConfigError as e:
        assert e.__cause__ is not None
        assert isinstance(e.__cause__, ValueError)
        assert str(e.__cause__) == "original"


# ────────────────────────────────────────────────────────────────────
# Test 6: Catching as base
# ────────────────────────────────────────────────────────────────────


def test_catch_as_nexusdl_error() -> None:
    """All exceptions can be caught as NexusDLError."""
    with pytest.raises(exc.NexusDLError):
        raise exc.ConfigError("test")

    with pytest.raises(exc.NexusDLError):
        raise exc.ParserError("test")

    with pytest.raises(exc.NexusDLError):
        raise exc.DownloadError("test")


def test_catch_as_exception() -> None:
    """All exceptions can be caught as Exception."""
    with pytest.raises(Exception):
        raise exc.ConfigError("test")


def test_catch_parser_error_specific() -> None:
    """Parser errors can be caught as ParserError."""
    with pytest.raises(exc.ParserError):
        raise exc.ParserNotFoundError("not found")


def test_catch_download_error_specific() -> None:
    """Download errors can be caught as DownloadError."""
    with pytest.raises(exc.DownloadError):
        raise exc.ChapterDownloadError("chapter failed")


# ────────────────────────────────────────────────────────────────────
# Test 7: EmptyChapterError
# ────────────────────────────────────────────────────────────────────


def test_empty_chapter_error() -> None:
    """EmptyChapterError inherits from DownloadError."""
    err = exc.EmptyChapterError("chapter has 0 pages")
    assert isinstance(err, exc.DownloadError)
    assert isinstance(err, exc.NexusDLError)
    assert "0 pages" in str(err)


# ────────────────────────────────────────────────────────────────────
# Test 8: InvalidStateTransitionError
# ────────────────────────────────────────────────────────────────────


def test_invalid_state_transition_error() -> None:
    """InvalidStateTransitionError is raised on bad transitions."""
    err = exc.InvalidStateTransitionError(
        "cannot transition from DONE to RUNNING",
    )
    assert isinstance(err, exc.NexusDLError)
    assert "DONE" in str(err)
    assert "RUNNING" in str(err)


# ────────────────────────────────────────────────────────────────────
# Test 9: to_dict (if implemented)
# ────────────────────────────────────────────────────────────────────


def test_exception_to_dict() -> None:
    """Exceptions can be serialized to dict (if supported)."""
    err = exc.ConfigError(
        "invalid config",
        context={"field": "log_level"},
    )

    if hasattr(err, "to_dict"):
        data = err.to_dict()
        assert data["type"] == "ConfigError"
        assert "invalid config" in data["message"]
        if "context" in data:
            assert data["context"]["field"] == "log_level"


# ────────────────────────────────────────────────────────────────────
# Test 10: All subclasses are instantiable
# ────────────────────────────────────────────────────────────────────


def test_all_exceptions_instantiable() -> None:
    """All exceptions can be instantiated with a message."""
    exception_classes = [
        exc.ConfigError,
        exc.ParserError,
        exc.ParserNotFoundError,
        exc.ParserResponseError,
        exc.ParserParseError,
        exc.DownloadError,
        exc.ChapterDownloadError,
        exc.PageDownloadError,
        exc.EmptyChapterError,
        exc.PackagingError,
        exc.LibraryError,
        exc.ComplianceError,
        exc.SessionError,
        exc.SessionTimeoutError,
        exc.SessionRateLimitError,
        exc.SessionCloudflareError,
        exc.CookieEncryptionError,
        exc.CircuitBreakerOpenError,
        exc.RateLimitTimeoutError,
        exc.RetryError,
        exc.RetryTimeoutError,
        exc.PluginError,
        exc.InvalidStateTransitionError,
        exc.ImageValidationError,
        exc.ImageConversionError,
    ]

    for cls in exception_classes:
        err = cls("test message")
        assert str(err) == "test message"
        assert err.args == ("test message",)
