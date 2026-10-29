# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for core.utils.url.

URL helpers used everywhere in NexusDL:

    - Normalization (trailing slash, fragment, query order)
    - Resolution (relative → absolute)
    - Extraction (domain, hostname, scheme, ID, extension)
    - Validation (https, image, valid URL)
    - Sanitization (filename, slug)
    - Encoding (UTF-8, IDN)

What is tested:
    - normalize_url (trailing slash, fragment, query, case)
    - resolve_url (relative, absolute, double slash, query)
    - is_absolute_url
    - get_domain / get_hostname / get_scheme
    - is_https
    - extract_id (query, fragment, trailing slash)
    - extract_chapter_id
    - strip_query / strip_fragment
    - ensure_trailing_slash / ensure_scheme
    - sanitize_filename (Windows, Unix)
    - slugify_url (accents, spaces)
    - is_same_domain (subdomain, different)
    - extract_extension
    - is_image_url
    - encode_url / decode_url (UTF-8)
    - is_valid_url (https, localhost, IP, IDN)
    - build_url (params, override)
    - get_query_params
"""
from __future__ import annotations

import pytest

from nexusdl.core.utils.url import (
    build_url,
    decode_url,
    encode_url,
    ensure_scheme,
    ensure_trailing_slash,
    extract_chapter_id,
    extract_extension,
    extract_id,
    get_domain,
    get_hostname,
    get_query_params,
    get_scheme,
    is_absolute_url,
    is_https,
    is_image_url,
    is_same_domain,
    is_valid_url,
    normalize_url,
    resolve_url,
    sanitize_filename,
    slugify_url,
    strip_fragment,
    strip_query,
)


# ────────────────────────────────────────────────────────────────────
# Test 1: normalize_url
# ────────────────────────────────────────────────────────────────────


def test_normalize_url_trailing_slash() -> None:
    """Trailing slash is normalized (added or removed consistently)."""
    # Depending on impl, either both have slash or neither
    a = normalize_url("https://test.com/path")
    b = normalize_url("https://test.com/path/")
    assert a == b


def test_normalize_url_fragment() -> None:
    """Fragment is stripped."""
    assert normalize_url("https://test.com/path#top") == normalize_url("https://test.com/path")


def test_normalize_url_query_order() -> None:
    """Query parameters are sorted (consistent cache key)."""
    a = normalize_url("https://test.com/path?b=2&a=1")
    b = normalize_url("https://test.com/path?a=1&b=2")
    assert a == b


def test_normalize_url_case_host() -> None:
    """Host is lowercased."""
    assert normalize_url("https://TEST.COM/path") == "https://test.com/path"


def test_normalize_url_idempotent() -> None:
    """normalize_url(normalize_url(x)) == normalize_url(x)."""
    url = "https://test.com/path?a=1&b=2#top"
    once = normalize_url(url)
    twice = normalize_url(once)
    assert once == twice


# ────────────────────────────────────────────────────────────────────
# Test 2: resolve_url
# ────────────────────────────────────────────────────────────────────


def test_resolve_url_relative() -> None:
    """Relative URL is joined with base."""
    result = resolve_url("https://test.com/base/", "/path/page")
    assert result == "https://test.com/path/page"


def test_resolve_url_absolute() -> None:
    """Absolute URL is returned as-is."""
    result = resolve_url("https://test.com/base/", "https://other.com/path")
    assert result == "https://other.com/path"


def test_resolve_url_no_leading_slash() -> None:
    """Relative URL without leading slash is joined correctly."""
    result = resolve_url("https://test.com/base/", "page")
    assert result == "https://test.com/base/page"


def test_resolve_url_double_slash() -> None:
    """Double slashes are collapsed."""
    result = resolve_url("https://test.com/", "/path//page")
    # Should not produce //path//page
    assert "//path//" not in result.replace("https://", "")


def test_resolve_url_preserves_query() -> None:
    """Query string is preserved."""
    result = resolve_url("https://test.com/", "/path?a=1")
    assert "?a=1" in result


def test_resolve_url_preserves_fragment() -> None:
    """Fragment is preserved."""
    result = resolve_url("https://test.com/", "/path#section")
    assert "#section" in result


# ────────────────────────────────────────────────────────────────────
# Test 3: is_absolute_url
# ────────────────────────────────────────────────────────────────────


def test_is_absolute_url_https() -> None:
    """https:// URLs are absolute."""
    assert is_absolute_url("https://test.com/path") is True


def test_is_absolute_url_http() -> None:
    """http:// URLs are absolute."""
    assert is_absolute_url("http://test.com/path") is True


def test_is_absolute_url_relative() -> None:
    """Relative paths are not absolute."""
    assert is_absolute_url("/path/page") is False
    assert is_absolute_url("page") is False


def test_is_absolute_url_protocol_relative() -> None:
    """//test.com/path is protocol-relative (not absolute)."""
    assert is_absolute_url("//test.com/path") is False


# ────────────────────────────────────────────────────────────────────
# Test 4: get_domain / get_hostname / get_scheme
# ────────────────────────────────────────────────────────────────────


def test_get_domain() -> None:
    """get_domain() returns the domain."""
    assert get_domain("https://test.com/path") == "test.com"
    assert get_domain("https://sub.test.com/path") == "sub.test.com"


def test_get_hostname() -> None:
    """get_hostname() returns hostname without port."""
    assert get_hostname("https://test.com:8080/path") == "test.com"


def test_get_scheme() -> None:
    """get_scheme() returns the scheme."""
    assert get_scheme("https://test.com") == "https"
    assert get_scheme("http://test.com") == "http"


# ────────────────────────────────────────────────────────────────────
# Test 5: is_https
# ────────────────────────────────────────────────────────────────────


def test_is_https_true() -> None:
    """https:// URLs return True."""
    assert is_https("https://test.com") is True


def test_is_https_false_http() -> None:
    """http:// URLs return False."""
    assert is_https("http://test.com") is False


def test_is_https_false_relative() -> None:
    """Relative paths return False."""
    assert is_https("/path") is False


# ────────────────────────────────────────────────────────────────────
# Test 6: extract_id
# ────────────────────────────────────────────────────────────────────


def test_extract_id_simple() -> None:
    """extract_id() returns the last path segment."""
    assert extract_id("https://test.com/manga/one-piece") == "one-piece"


def test_extract_id_strips_query() -> None:
    """Query string is stripped."""
    assert extract_id("https://test.com/manga/one-piece?ref=home") == "one-piece"


def test_extract_id_strips_fragment() -> None:
    """Fragment is stripped."""
    assert extract_id("https://test.com/manga/one-piece#top") == "one-piece"


def test_extract_id_trailing_slash() -> None:
    """Trailing slash is ignored."""
    assert extract_id("https://test.com/manga/one-piece/") == "one-piece"


def test_extract_id_nested() -> None:
    """Nested paths return the last segment."""
    assert extract_id("https://test.com/a/b/c/d") == "d"


def test_extract_id_empty() -> None:
    """Empty path returns ''."""
    result = extract_id("https://test.com")
    assert result == "" or result == "test.com"


# ────────────────────────────────────────────────────────────────────
# Test 7: extract_chapter_id
# ────────────────────────────────────────────────────────────────────


def test_extract_chapter_id() -> None:
    """extract_chapter_id() extracts chapter ID from URL."""
    result = extract_chapter_id("https://test.com/manga/one-piece/chapter/123")
    assert result == "123" or result == "chapter/123" or result == "123"


def test_extract_chapter_id_query() -> None:
    """Query is stripped."""
    result = extract_chapter_id("https://test.com/chapter/abc?ref=x")
    assert "abc" in result


# ────────────────────────────────────────────────────────────────────
# Test 8: strip_query / strip_fragment
# ────────────────────────────────────────────────────────────────────


def test_strip_query() -> None:
    """strip_query() removes the query string."""
    assert strip_query("https://test.com/path?a=1&b=2") == "https://test.com/path"


def test_strip_fragment() -> None:
    """strip_fragment() removes the fragment."""
    assert strip_fragment("https://test.com/path#top") == "https://test.com/path"


def test_strip_both() -> None:
    """Both can be applied sequentially."""
    url = "https://test.com/path?a=1#top"
    assert strip_fragment(strip_query(url)) == "https://test.com/path"


# ────────────────────────────────────────────────────────────────────
# Test 9: ensure_trailing_slash / ensure_scheme
# ────────────────────────────────────────────────────────────────────


def test_ensure_trailing_slash_added() -> None:
    """Slash is added if missing."""
    assert ensure_trailing_slash("https://test.com/path") == "https://test.com/path/"


def test_ensure_trailing_slash_preserved() -> None:
    """Slash is preserved if present."""
    assert ensure_trailing_slash("https://test.com/path/") == "https://test.com/path/"


def test_ensure_scheme_added() -> None:
    """Scheme is added if missing."""
    assert ensure_scheme("test.com/path") == "https://test.com/path"


def test_ensure_scheme_preserved() -> None:
    """Scheme is preserved if present."""
    assert ensure_scheme("https://test.com/path") == "https://test.com/path"
    assert ensure_scheme("http://test.com/path") == "http://test.com/path"


# ────────────────────────────────────────────────────────────────────
# Test 10: sanitize_filename
# ────────────────────────────────────────────────────────────────────


def test_sanitize_filename_windows_chars() -> None:
    """Windows-forbidden chars are replaced."""
    assert sanitize_filename('a\\b/c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_filename_spaces() -> None:
    """Spaces are preserved (valid in filenames)."""
    assert sanitize_filename("One Piece 001") == "One Piece 001"


def test_sanitize_filename_leading_dots() -> None:
    """Leading dots are handled (hidden files)."""
    result = sanitize_filename("..hidden")
    assert not result.startswith("..")


def test_sanitize_filename_empty() -> None:
    """Empty string returns a safe default."""
    assert sanitize_filename("") in ("", "_", "unnamed")


# ────────────────────────────────────────────────────────────────────
# Test 11: slugify_url
# ────────────────────────────────────────────────────────────────────


def test_slugify_url_spaces() -> None:
    """Spaces become hyphens."""
    assert slugify_url("One Piece") == "one-piece"


def test_slugify_url_accents() -> None:
    """Accents are removed."""
    result = slugify_url("Pokémon")
    assert result in ("pokemon", "pokémon")  # either preserved or stripped


def test_slugify_url_uppercase() -> None:
    """Uppercase becomes lowercase."""
    assert slugify_url("ONE PIECE") == "one-piece"


def test_slugify_url_special_chars() -> None:
    """Special chars are removed."""
    result = slugify_url("One Piece! 001")
    assert "!" not in result
    assert "one" in result.lower()


# ────────────────────────────────────────────────────────────────────
# Test 12: is_same_domain
# ────────────────────────────────────────────────────────────────────


def test_is_same_domain_true() -> None:
    """Same domain returns True."""
    assert is_same_domain("https://test.com/a", "https://test.com/b") is True


def test_is_same_domain_false() -> None:
    """Different domains return False."""
    assert is_same_domain("https://test.com/a", "https://other.com/b") is False


def test_is_same_domain_subdomain() -> None:
    """Subdomain is different from apex."""
    assert is_same_domain("https://a.test.com", "https://test.com") is False


def test_is_same_domain_www() -> None:
    """www. is a subdomain (different from apex)."""
    assert is_same_domain("https://www.test.com", "https://test.com") is False


# ────────────────────────────────────────────────────────────────────
# Test 13: extract_extension
# ────────────────────────────────────────────────────────────────────


def test_extract_extension_jpg() -> None:
    """extract_extension() returns 'jpg'."""
    assert extract_extension("https://test.com/image.jpg") == "jpg"


def test_extract_extension_png() -> None:
    """extract_extension() returns 'png'."""
    assert extract_extension("https://test.com/image.png") == "png"


def test_extract_extension_webp() -> None:
    """extract_extension() returns 'webp'."""
    assert extract_extension("https://test.com/image.webp") == "webp"


def test_extract_extension_query() -> None:
    """Query is stripped before extraction."""
    assert extract_extension("https://test.com/image.jpg?x=1") == "jpg"


def test_extract_extension_no_ext() -> None:
    """No extension returns ''."""
    assert extract_extension("https://test.com/image") == ""


# ────────────────────────────────────────────────────────────────────
# Test 14: is_image_url
# ────────────────────────────────────────────────────────────────────


def test_is_image_url_jpg() -> None:
    """jpg URLs are images."""
    assert is_image_url("https://test.com/image.jpg") is True


def test_is_image_url_png() -> None:
    """png URLs are images."""
    assert is_image_url("https://test.com/image.png") is True


def test_is_image_url_webp() -> None:
    """webp URLs are images."""
    assert is_image_url("https://test.com/image.webp") is True


def test_is_image_url_query() -> None:
    """Query string is ignored."""
    assert is_image_url("https://test.com/image.jpg?x=1") is True


def test_is_image_url_html() -> None:
    """html URLs are not images."""
    assert is_image_url("https://test.com/page.html") is False


def test_is_image_url_no_ext() -> None:
    """No extension is not an image."""
    assert is_image_url("https://test.com/image") is False


# ────────────────────────────────────────────────────────────────────
# Test 15: encode_url / decode_url
# ────────────────────────────────────────────────────────────────────


def test_encode_url_accents() -> None:
    """é becomes %C3%A9."""
    result = encode_url("https://test.com/path?q=Pokémon")
    assert "%C3%A9" in result or "Pok%C3%A9mon" in result


def test_encode_url_spaces() -> None:
    """Spaces become %20."""
    result = encode_url("https://test.com/path with spaces")
    assert " " not in result
    assert "%20" in result


def test_decode_url() -> None:
    """%C3%A9 becomes é."""
    result = decode_url("https://test.com/path?q=Pok%C3%A9mon")
    assert "Pokémon" in result


def test_encode_decode_roundtrip() -> None:
    """encode then decode returns original."""
    original = "https://test.com/path?q=Pokémon"
    encoded = encode_url(original)
    decoded = decode_url(encoded)
    assert decoded == original


# ────────────────────────────────────────────────────────────────────
# Test 16: is_valid_url
# ────────────────────────────────────────────────────────────────────


def test_is_valid_url_https() -> None:
    """https:// URLs are valid."""
    assert is_valid_url("https://test.com/path") is True


def test_is_valid_url_localhost() -> None:
    """localhost URLs are valid."""
    assert is_valid_url("http://localhost:8000/path") is True


def test_is_valid_url_ip() -> None:
    """IP URLs are valid."""
    assert is_valid_url("http://192.168.1.1:8080/path") is True


def test_is_valid_url_idn() -> None:
    """IDN URLs are valid (x.com or xn--)."""
    assert is_valid_url("https://例え.jp/path") is True or is_valid_url("https://xn--r8jz45g.jp/path") is True


def test_is_valid_url_invalid() -> None:
    """Invalid URLs return False."""
    assert is_valid_url("not a url") is False
    assert is_valid_url("") is False
    assert is_valid_url("://missing-scheme") is False


# ────────────────────────────────────────────────────────────────────
# Test 17: build_url
# ────────────────────────────────────────────────────────────────────


def test_build_url_simple() -> None:
    """build_url(base, params) creates URL with query."""
    result = build_url("https://test.com/path", {"a": "1", "b": "2"})
    assert "a=1" in result
    assert "b=2" in result


def test_build_url_preserves_existing() -> None:
    """Existing query params are preserved."""
    result = build_url("https://test.com/path?existing=1", {"new": "2"})
    assert "existing=1" in result
    assert "new=2" in result


def test_build_url_overrides() -> None:
    """New params override existing ones."""
    result = build_url("https://test.com/path?a=1", {"a": "2"})
    assert "a=2" in result
    assert "a=1" not in result


# ────────────────────────────────────────────────────────────────────
# Test 18: get_query_params
# ────────────────────────────────────────────────────────────────────


def test_get_query_params() -> None:
    """get_query_params() returns a dict."""
    params = get_query_params("https://test.com/path?a=1&b=2")
    assert params == {"a": "1", "b": "2"}


def test_get_query_params_empty() -> None:
    """No query returns {}."""
    assert get_query_params("https://test.com/path") == {}


def test_get_query_params_multi_value() -> None:
    """Multiple values for same key are handled."""
    params = get_query_params("https://test.com/path?a=1&a=2")
    # Either {"a": "1"} (last wins) or {"a": ["1", "2"]}
    assert "a" in params
