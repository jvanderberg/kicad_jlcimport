"""Tests for api.py: SSL helpers, curl fallback, and _urlopen routing."""

import gzip
import json
import os
import socket
import subprocess
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

import pytest

from kicad_jlcimport.easyeda import api
from kicad_jlcimport.easyeda.api import (
    APIError,
    SSLCertError,
    _curl_fetch,
    _CurlResponse,
    _strip_cjk_parens,
    _urlopen,
    download_step,
    download_wrl_source,
    fetch_product_image,
    filter_by_min_stock,
    filter_by_type,
    validate_lcsc_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_ssl_state():
    """Reset module-level SSL caching between tests."""
    api._SSL_AVAILABLE = None
    api._SSL_CTX = None
    api._SSL_CTX_INITIALIZED = False


def _reset_curl_state():
    """Reset module-level curl path cache between tests."""
    api._CURL_PATH = None


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Reset cached module state before each test."""
    _reset_ssl_state()
    _reset_curl_state()
    saved = api._allow_unverified
    saved_dns = api._dns_cache.copy()
    yield
    _reset_ssl_state()
    _reset_curl_state()
    api._allow_unverified = saved
    api._dns_cache.clear()
    api._dns_cache.update(saved_dns)


# ===================================================================
# 1. _check_ssl_available()
# ===================================================================


class TestCheckSslAvailable:
    def test_returns_true_when_ssl_importable(self):
        # ssl is available in the test environment
        assert api._check_ssl_available() is True

    def test_caches_result(self):
        api._check_ssl_available()
        first = api._SSL_AVAILABLE
        api._check_ssl_available()
        assert api._SSL_AVAILABLE is first

    def test_returns_false_when_ssl_missing(self):
        with mock.patch.dict("sys.modules", {"ssl": None}):
            api._SSL_AVAILABLE = None  # force re-check
            # Importing a module mapped to None raises ImportError
            assert api._check_ssl_available() is False

    def test_cached_false_is_not_rechecked(self):
        api._SSL_AVAILABLE = False
        # Even though ssl is actually available, cached False sticks
        assert api._check_ssl_available() is False


# ===================================================================
# 2. _get_ssl_ctx() lazy initialization
# ===================================================================


class TestGetSslCtx:
    def test_initializes_once(self):
        ctx1 = api._get_ssl_ctx()
        ctx2 = api._get_ssl_ctx()
        assert api._SSL_CTX_INITIALIZED is True
        assert ctx1 is ctx2

    def test_returns_none_when_ssl_unavailable(self):
        api._SSL_AVAILABLE = False
        ctx = api._get_ssl_ctx()
        assert ctx is None
        assert api._SSL_CTX_INITIALIZED is True

    def test_returns_context_when_ssl_available(self):
        api._get_ssl_ctx()
        # In a normal Python install this should return a real context
        # (or None if no CA bundle exists, but never raises)
        assert api._SSL_CTX_INITIALIZED is True

    def test_does_not_reinitialize(self):
        sentinel = object()
        api._SSL_CTX = sentinel
        api._SSL_CTX_INITIALIZED = True
        assert api._get_ssl_ctx() is sentinel


# ===================================================================
# 3. _curl_fetch() success path
# ===================================================================


def _fake_proc(stdout=b"", stderr=b"", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestCurlFetchSuccess:
    def test_basic_get(self):
        proc = _fake_proc(stdout=b'{"ok":true}', stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc) as m:
                resp = _curl_fetch("https://example.com/api", timeout=10)
                assert resp.read() == b'{"ok":true}'
                assert resp.status == 200
                cmd = m.call_args[0][0]
                assert "-L" in cmd
                assert "-sS" in cmd

    def test_post_with_data(self):
        proc = _fake_proc(stdout=b"ok", stderr=b"201", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc) as m:
                resp = _curl_fetch("https://example.com", data=b"body")
                assert resp.status == 201
                cmd = m.call_args[0][0]
                assert "--data-binary" in cmd
                assert "@-" in cmd

    def test_custom_headers(self):
        proc = _fake_proc(stdout=b"x", stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc) as m:
                _curl_fetch("https://example.com", headers={"X-Key": "val"})
                cmd = m.call_args[0][0]
                idx = cmd.index("-H")
                assert cmd[idx + 1] == "X-Key: val"

    def test_insecure_flag(self):
        proc = _fake_proc(stdout=b"x", stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc) as m:
                _curl_fetch("https://example.com", insecure=True)
                assert "-k" in m.call_args[0][0]

    def test_response_context_manager(self):
        resp = _CurlResponse(b"data", 200, "https://example.com")
        with resp as r:
            assert r.read() == b"data"
            assert r.url == "https://example.com"


# ===================================================================
# 4. _curl_fetch() error paths
# ===================================================================


class TestCurlFetchErrors:
    def test_curl_not_found_in_path(self):
        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(APIError, match="curl not found"):
                _curl_fetch("https://example.com")

    def test_curl_binary_missing_at_runtime(self):
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                with pytest.raises(APIError, match="curl binary not found"):
                    _curl_fetch("https://example.com")

    def test_curl_timeout(self):
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 30)):
                with pytest.raises(APIError, match="timed out"):
                    _curl_fetch("https://example.com")

    def test_http_4xx_error(self):
        proc = _fake_proc(stdout=b"Not Found", stderr=b"404", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                with pytest.raises(APIError, match="HTTP 404"):
                    _curl_fetch("https://example.com")

    def test_http_5xx_error(self):
        proc = _fake_proc(stdout=b"error", stderr=b"502", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                with pytest.raises(APIError, match="HTTP 502"):
                    _curl_fetch("https://example.com")

    def test_nonzero_returncode(self):
        proc = _fake_proc(stdout=b"", stderr=b"curl: (6) Could not resolve host", returncode=6)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                with pytest.raises(APIError, match="curl error"):
                    _curl_fetch("https://example.com")


# ===================================================================
# 5. _urlopen() routing to curl when ssl unavailable
# ===================================================================


class TestUrlopenRouting:
    def test_routes_to_curl_when_ssl_unavailable(self):
        api._SSL_AVAILABLE = False
        proc = _fake_proc(stdout=b"hello", stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                req = urllib.request.Request(
                    "https://example.com",
                    headers={"Accept": "text/html"},
                )
                resp = _urlopen(req, timeout=5)
                assert resp.read() == b"hello"
                assert isinstance(resp, _CurlResponse)

    def test_passes_insecure_flag_when_allowed(self):
        api._SSL_AVAILABLE = False
        api._allow_unverified = True
        proc = _fake_proc(stdout=b"ok", stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc) as m:
                req = urllib.request.Request("https://example.com")
                _urlopen(req, timeout=5)
                cmd = m.call_args[0][0]
                assert "-k" in cmd

    def test_uses_urllib_when_ssl_available(self):
        # ssl is available in test environment — _urlopen should use urllib
        fake_resp = SimpleNamespace(
            read=lambda: b"urllib response",
            __enter__=lambda self: self,
            __exit__=lambda self, *a: None,
        )
        with mock.patch("urllib.request.urlopen", return_value=fake_resp) as m:
            req = urllib.request.Request("https://example.com")
            resp = _urlopen(req, timeout=5)
            assert m.called
            assert not isinstance(resp, _CurlResponse)

    def test_extracts_headers_from_request(self):
        api._SSL_AVAILABLE = False
        proc = _fake_proc(stdout=b"ok", stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc) as m:
                req = urllib.request.Request(
                    "https://example.com",
                    headers={"X-Custom": "value"},
                )
                _urlopen(req, timeout=5)
                cmd = m.call_args[0][0]
                # Header should appear in the curl command
                assert "X-custom: value" in cmd

    def test_extracts_post_data_from_request(self):
        api._SSL_AVAILABLE = False
        proc = _fake_proc(stdout=b"ok", stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc) as m:
                req = urllib.request.Request(
                    "https://example.com",
                    data=b"payload",
                )
                _urlopen(req, timeout=5)
                kwargs = m.call_args
                assert kwargs[1].get("input") == b"payload" or m.call_args.kwargs.get("input") == b"payload"


# ===================================================================
# 6. Status code parsing edge cases in _curl_fetch
# ===================================================================


class TestStatusCodeParsing:
    """The -w '%{stderr}%{http_code}' writes status as last token on stderr."""

    def test_status_with_curl_warnings_on_stderr(self):
        # curl -S may write warnings before the status code
        proc = _fake_proc(
            stdout=b"body",
            stderr=b"Warning: something odd\n200",
            returncode=0,
        )
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                resp = _curl_fetch("https://example.com")
                assert resp.status == 200
                assert resp.read() == b"body"

    def test_status_only_on_stderr(self):
        proc = _fake_proc(stdout=b"data", stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                resp = _curl_fetch("https://example.com")
                assert resp.status == 200

    def test_empty_stderr_means_status_zero(self):
        proc = _fake_proc(stdout=b"data", stderr=b"", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                with pytest.raises(APIError, match="curl error"):
                    _curl_fetch("https://example.com")

    def test_garbage_stderr_means_status_zero(self):
        proc = _fake_proc(stdout=b"data", stderr=b"not-a-number", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                with pytest.raises(APIError, match="curl error"):
                    _curl_fetch("https://example.com")

    def test_body_with_trailing_newlines_unaffected(self):
        # Body on stdout is never conflated with status on stderr
        body = b'{"result": true}\n\n\n'
        proc = _fake_proc(stdout=body, stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                resp = _curl_fetch("https://example.com")
                assert resp.read() == body
                assert resp.status == 200

    def test_binary_body_preserved(self):
        body = bytes(range(256))
        proc = _fake_proc(stdout=body, stderr=b"200", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                resp = _curl_fetch("https://example.com")
                assert resp.read() == body

    def test_301_redirect_treated_as_error(self):
        # With -L, curl follows redirects, so a 301 final status means
        # the redirect chain failed somehow
        proc = _fake_proc(stdout=b"", stderr=b"301", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/curl"):
            with mock.patch("subprocess.run", return_value=proc):
                # 301 < 400 so it won't hit the HTTP error check,
                # but it's a valid status
                resp = _curl_fetch("https://example.com")
                assert resp.status == 301


# ===================================================================
# 7. DNS cache
# ===================================================================


class TestDnsCache:
    def test_dns_cache_path_linux(self):
        with mock.patch("sys.platform", "linux"):
            path = api._dns_cache_path()
            assert path.endswith("jlcimport_dns_cache.json")
            assert ".config/kicad" in path

    def test_dns_cache_path_darwin(self):
        with mock.patch("sys.platform", "darwin"):
            path = api._dns_cache_path()
            assert "Library/Preferences/kicad" in path

    def test_dns_cache_path_win32(self):
        with mock.patch("sys.platform", "win32"):
            with mock.patch.dict(os.environ, {"APPDATA": "C:\\Users\\test\\AppData"}):
                path = api._dns_cache_path()
                assert "kicad" in path

    def test_load_dns_cache_missing_file(self):
        with mock.patch.object(api, "_dns_cache_path", return_value="/nonexistent/path.json"):
            assert api._load_dns_cache() == {}

    def test_load_and_save_dns_cache(self, tmp_path):
        cache_file = str(tmp_path / "dns_cache.json")
        with mock.patch.object(api, "_dns_cache_path", return_value=cache_file):
            api._save_dns_cache({"example.com": [[2, 1, 6, "", ["93.184.216.34", 80]]]})
            loaded = api._load_dns_cache()
            assert "example.com" in loaded

    def test_save_dns_cache_oserror(self, tmp_path):
        # Saving to an unwritable path should not raise
        with mock.patch.object(api, "_dns_cache_path", return_value="/proc/nonexistent/cache.json"):
            api._save_dns_cache({"test": "data"})  # should not raise

    def test_result_to_json_and_back(self):
        original = [(2, 1, 6, "", ("93.184.216.34", 80))]
        as_json = api._result_to_json(original)
        roundtripped = api._result_from_json(as_json)
        assert roundtripped == original

    def test_cached_getaddrinfo_caches_in_memory(self):
        fake_result = [(2, 1, 6, "", ("1.2.3.4", 0))]
        with mock.patch.object(api, "_original_getaddrinfo", return_value=fake_result):
            with mock.patch.object(api, "_save_dns_cache"):
                api._dns_cache.clear()
                r1 = api._cached_getaddrinfo("example.com", 443)
                r2 = api._cached_getaddrinfo("example.com", 443)
                assert r1 == r2
                # _original_getaddrinfo only called once due to caching
                assert api._original_getaddrinfo.call_count == 1

    def test_cached_getaddrinfo_falls_back_to_disk(self):
        disk_data = {"example.com": [[2, 1, 6, "", ["1.2.3.4", 0]]]}
        with mock.patch.object(api, "_original_getaddrinfo", side_effect=socket.gaierror):
            with mock.patch.object(api, "_load_dns_cache", return_value=disk_data):
                api._dns_cache.clear()
                result = api._cached_getaddrinfo("example.com", 443)
                assert result[0][4] == ("1.2.3.4", 0)

    def test_cached_getaddrinfo_raises_when_no_disk_cache(self):
        with mock.patch.object(api, "_original_getaddrinfo", side_effect=socket.gaierror):
            with mock.patch.object(api, "_load_dns_cache", return_value={}):
                api._dns_cache.clear()
                with pytest.raises(socket.gaierror):
                    api._cached_getaddrinfo("nonexistent.example", 443)

    def test_cached_getaddrinfo_no_host(self):
        # Edge case: called with no args
        with mock.patch.object(api, "_original_getaddrinfo", side_effect=socket.gaierror):
            api._dns_cache.clear()
            with pytest.raises(socket.gaierror):
                api._cached_getaddrinfo()


# ===================================================================
# 8. _make_ssl_context()
# ===================================================================


class TestMakeSslContext:
    def test_uses_bundled_cacerts_pem(self, tmp_path):
        # Create a fake cacerts.pem — it won't load real certs but tests the path
        fake_pem = tmp_path / "cacerts.pem"
        fake_pem.write_text("not a real cert")
        with mock.patch.object(api, "_CACERTS_PEM", str(fake_pem)):
            # The load will fail, so it should fall through
            api._make_ssl_context()
            # Should still return something (system fallback)
            # We just verify it doesn't crash

    def test_falls_through_when_no_cacerts(self):
        with mock.patch.object(api, "_CACERTS_PEM", "/nonexistent/cacerts.pem"):
            with mock.patch.dict("sys.modules", {"certifi": None}):
                # Should fall through to system store
                ctx = api._make_ssl_context()
                # On a normal system, system store works
                assert ctx is not None

    def test_uses_certifi_when_available(self):
        fake_certifi = SimpleNamespace(where=lambda: "/fake/certifi.pem")
        with mock.patch.object(api, "_CACERTS_PEM", "/nonexistent/cacerts.pem"):
            with mock.patch.dict("sys.modules", {"certifi": fake_certifi}):
                # certifi.where() returns a bad path, so load will fail
                # and fall through to system store
                api._make_ssl_context()

    def test_returns_none_when_all_fail(self):
        import ssl

        with mock.patch.object(api, "_CACERTS_PEM", "/nonexistent/cacerts.pem"):
            with mock.patch.dict("sys.modules", {"certifi": None}):
                with mock.patch.object(ssl, "create_default_context", side_effect=Exception("no certs")):
                    ctx = api._make_ssl_context()
                    assert ctx is None


# ===================================================================
# 9. validate_lcsc_id()
# ===================================================================


class TestValidateLcscId:
    def test_valid_id(self):
        assert validate_lcsc_id("C12345") == "C12345"

    def test_lowercase(self):
        assert validate_lcsc_id("c12345") == "C12345"

    def test_without_prefix(self):
        assert validate_lcsc_id("12345") == "C12345"

    def test_whitespace(self):
        assert validate_lcsc_id("  C99  ") == "C99"

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="Invalid LCSC"):
            validate_lcsc_id("")

    def test_invalid_letters(self):
        with pytest.raises(ValueError, match="Invalid LCSC"):
            validate_lcsc_id("CABC")

    def test_invalid_too_long(self):
        with pytest.raises(ValueError, match="Invalid LCSC"):
            validate_lcsc_id("C" + "1" * 13)

    def test_single_digit(self):
        assert validate_lcsc_id("C1") == "C1"

    def test_valid_12_digits(self):
        assert validate_lcsc_id("C123456789012") == "C123456789012"

    def test_invalid_special_chars(self):
        with pytest.raises(ValueError, match="Invalid LCSC"):
            validate_lcsc_id("C123/../../etc")

    def test_invalid_url_injection(self):
        with pytest.raises(ValueError, match="Invalid LCSC"):
            validate_lcsc_id("C123?param=value")

    def test_invalid_mixed_alpha_numeric(self):
        with pytest.raises(ValueError, match="Invalid LCSC"):
            validate_lcsc_id("C12AB34")

    def test_whitespace_only(self):
        with pytest.raises(ValueError, match="Invalid LCSC"):
            validate_lcsc_id("   ")


# ===================================================================
# 10. _urlopen() SSL code paths
# ===================================================================


class TestUrlopenSslPaths:
    def test_allow_unverified_ssl(self):
        api.allow_unverified_ssl()
        assert api._allow_unverified is True

    def test_unverified_mode_uses_unverified_context(self):
        api._allow_unverified = True
        fake_resp = SimpleNamespace(
            read=lambda: b"ok",
            __enter__=lambda self: self,
            __exit__=lambda self, *a: None,
        )
        with mock.patch("urllib.request.urlopen", return_value=fake_resp) as m:
            req = urllib.request.Request("https://example.com")
            _urlopen(req, timeout=5)
            ctx = (
                m.call_args[1].get("context") or m.call_args[0][2]
                if len(m.call_args[0]) > 2
                else m.call_args[1].get("context")
            )
            # It should have been called with an unverified context
            assert m.called
            assert ctx is not None
            assert ctx.check_hostname is False

    def test_ssl_cert_error_raised(self):
        import ssl

        cert_error = ssl.SSLCertVerificationError("cert failed")
        url_error = urllib.error.URLError(cert_error)
        with mock.patch("urllib.request.urlopen", side_effect=url_error):
            with mock.patch.object(api, "_get_ssl_ctx", return_value=mock.MagicMock()):
                req = urllib.request.Request("https://example.com")
                with pytest.raises(SSLCertError, match="TLS certificate verification failed"):
                    _urlopen(req, timeout=5)

    def test_non_ssl_url_error_reraised(self):
        url_error = urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=url_error):
            with mock.patch.object(api, "_get_ssl_ctx", return_value=mock.MagicMock()):
                req = urllib.request.Request("https://example.com")
                with pytest.raises(urllib.error.URLError):
                    _urlopen(req, timeout=5)

    def test_no_ssl_ctx_warns_and_uses_unverified(self):
        fake_resp = SimpleNamespace(
            read=lambda: b"ok",
            __enter__=lambda self: self,
            __exit__=lambda self, *a: None,
        )
        with mock.patch.object(api, "_get_ssl_ctx", return_value=None):
            with mock.patch("urllib.request.urlopen", return_value=fake_resp):
                req = urllib.request.Request("https://example.com")
                with pytest.warns(match="No TLS certificate source"):
                    _urlopen(req, timeout=5)


# ===================================================================
# 11. _get_json() and high-level API functions
# ===================================================================


class _FakeResponse:
    """Minimal context-manager response for mocking _urlopen."""

    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _mock_urlopen(body, status=200):
    """Return a context-manager mock that _urlopen callers can use."""
    resp = _FakeResponse(body, status)
    return mock.patch.object(api, "_urlopen", return_value=resp)


class TestGetJson:
    def test_success(self):
        with _mock_urlopen(b'{"success": true}'):
            result = api._get_json("https://example.com/api")
            assert result == {"success": True}

    def test_http_error(self):
        err = urllib.error.HTTPError("https://example.com", 500, "err", {}, None)
        with mock.patch.object(api, "_urlopen", side_effect=err):
            with pytest.raises(APIError, match="HTTP 500"):
                api._get_json("https://example.com/api")

    def test_url_error(self):
        err = urllib.error.URLError("connection refused")
        with mock.patch.object(api, "_urlopen", side_effect=err):
            with pytest.raises(APIError, match="Network error"):
                api._get_json("https://example.com/api")

    def test_api_error_passthrough(self):
        """APIError from curl fallback propagates without being re-wrapped."""
        with mock.patch.object(api, "_urlopen", side_effect=APIError("HTTP 502 fetching url")):
            with pytest.raises(APIError, match="HTTP 502"):
                api._get_json("https://example.com/api")


class TestFetchComponentUuids:
    def test_success(self):
        data = {"success": True, "result": [{"component_uuid": "abc"}, {"component_uuid": "def"}]}
        with mock.patch.object(api, "_get_json", return_value=data):
            result = api.fetch_component_uuids("C12345")
            assert len(result) == 2

    def test_no_result(self):
        with mock.patch.object(api, "_get_json", return_value={"success": False}):
            with pytest.raises(APIError, match="No component found"):
                api.fetch_component_uuids("C12345")


class TestFetchComponentData:
    def test_success(self):
        data = {"result": {"dataStr": {"head": {}}}}
        with mock.patch.object(api, "_get_json", return_value=data):
            result = api.fetch_component_data("some-uuid")
            assert "dataStr" in result

    def test_no_data(self):
        with mock.patch.object(api, "_get_json", return_value={"result": None}):
            with pytest.raises(APIError, match="No data"):
                api.fetch_component_data("some-uuid")


class TestSearchComponents:
    def test_parses_response(self):
        raw = {
            "data": {
                "componentPageInfo": {
                    "total": 1,
                    "list": [
                        {
                            "componentCode": "C12345",
                            "componentName": "Resistor",
                            "componentModelEn": "RC0805",
                            "componentBrandEn": "Yageo",
                            "componentSpecificationEn": "0805",
                            "componentTypeEn": "Resistors",
                            "stockCount": 5000,
                            "componentLibraryType": "base",
                            "componentPrices": [{"productPrice": 0.01}],
                            "describe": "100R 1%",
                            "lcscGoodsUrl": "https://jlcpcb.com/parts/C12345",
                            "dataManualUrl": "https://example.com/ds.pdf",
                        }
                    ],
                }
            }
        }
        resp_body = json.dumps(raw).encode()
        with _mock_urlopen(resp_body):
            result = api.search_components("100R")
            assert result["total"] == 1
            assert result["results"][0]["lcsc"] == "C12345"
            assert result["results"][0]["type"] == "Basic"
            assert result["results"][0]["price"] == 0.01

    def test_empty_response(self):
        raw = {"data": {"componentPageInfo": {"total": 0, "list": []}}}
        with _mock_urlopen(json.dumps(raw).encode()):
            result = api.search_components("nonexistent")
            assert result["total"] == 0
            assert result["results"] == []

    def test_extended_part_type(self):
        raw = {
            "data": {
                "componentPageInfo": {
                    "total": 1,
                    "list": [
                        {
                            "componentCode": "C99",
                            "componentLibraryType": "expand",
                            "componentPrices": [],
                        }
                    ],
                }
            }
        }
        with _mock_urlopen(json.dumps(raw).encode()):
            result = api.search_components("test")
            assert result["results"][0]["type"] == "Extended"
            assert result["results"][0]["price"] is None

    def test_network_error(self):
        err = urllib.error.URLError("timeout")
        with mock.patch.object(api, "_urlopen", side_effect=err):
            with pytest.raises(APIError, match="Search failed"):
                api.search_components("test")

    def test_curl_api_error_propagates(self):
        """APIError from curl fallback propagates without being swallowed."""
        with mock.patch.object(api, "_urlopen", side_effect=APIError("HTTP 502 fetching url")):
            with pytest.raises(APIError, match="HTTP 502"):
                api.search_components("test")

    def test_part_type_included_in_payload(self):
        raw = {"data": {"componentPageInfo": {"total": 0, "list": []}}}
        with _mock_urlopen(json.dumps(raw).encode()) as m:
            api.search_components("test", part_type="base")
            req = m.call_args[0][0]
            payload = json.loads(req.data.decode())
            assert payload["componentLibraryType"] == "base"


# ===================================================================
# 12. filter_by_min_stock() and filter_by_type()
# ===================================================================


class TestFilters:
    _RESULTS = [
        {"name": "A", "stock": 100, "type": "Basic"},
        {"name": "B", "stock": 5000, "type": "Extended"},
        {"name": "C", "stock": 0, "type": "Basic"},
    ]

    _EXTENDED_RESULTS = [
        {"lcsc": "C1", "stock": 0, "model": "R1"},
        {"lcsc": "C2", "stock": 5, "model": "R2"},
        {"lcsc": "C3", "stock": 50, "model": "R3"},
        {"lcsc": "C4", "stock": 500, "model": "R4"},
        {"lcsc": "C5", "stock": 5000, "model": "R5"},
        {"lcsc": "C6", "stock": 50000, "model": "R6"},
        {"lcsc": "C7", "stock": None, "model": "R7"},
    ]

    def test_filter_min_stock_zero_returns_all(self):
        filtered = filter_by_min_stock(self._RESULTS, 0)
        assert len(filtered) == 3

    def test_filter_min_stock_positive(self):
        filtered = filter_by_min_stock(self._RESULTS, 1000)
        assert len(filtered) == 1
        assert filtered[0]["name"] == "B"

    def test_filter_min_stock_negative(self):
        filtered = filter_by_min_stock(self._RESULTS, -1)
        assert len(filtered) == 3

    def test_filter_by_type_basic(self):
        filtered = filter_by_type(self._RESULTS, "Basic")
        assert len(filtered) == 2

    def test_filter_by_type_extended(self):
        filtered = filter_by_type(self._RESULTS, "Extended")
        assert len(filtered) == 1

    def test_filter_by_type_empty(self):
        filtered = filter_by_type(self._RESULTS, "")
        assert len(filtered) == 3

    def test_filter_by_type_none(self):
        filtered = filter_by_type(self._RESULTS, None)
        assert len(filtered) == 3

    def test_min_stock_one_excludes_zero_and_none(self):
        result = filter_by_min_stock(self._EXTENDED_RESULTS, 1)
        assert len(result) == 5
        codes = [r["lcsc"] for r in result]
        assert "C1" not in codes
        assert "C7" not in codes

    def test_min_stock_higher_than_all_returns_empty(self):
        result = filter_by_min_stock(self._EXTENDED_RESULTS, 100000)
        assert result == []

    def test_does_not_mutate_input(self):
        original = [{"lcsc": "C1", "stock": 10}]
        filter_by_min_stock(original, 100)
        assert len(original) == 1

    def test_missing_stock_key_treated_as_zero(self):
        results = [{"lcsc": "C1", "model": "R1"}]
        assert filter_by_min_stock(results, 1) == []

    def test_exact_threshold_included(self):
        results = [{"lcsc": "C1", "stock": 100}]
        assert len(filter_by_min_stock(results, 100)) == 1

    def test_filter_type_does_not_mutate(self):
        original = [{"lcsc": "C1", "type": "Basic"}]
        filter_by_type(original, "Extended")
        assert len(original) == 1

    def test_filter_type_missing_key_excluded(self):
        results = [{"lcsc": "C1", "stock": 10}]
        assert filter_by_type(results, "Basic") == []

    def test_filter_type_unmatched_returns_empty(self):
        result = filter_by_type(self._RESULTS, "Unknown")
        assert result == []


# ===================================================================
# 13. fetch_product_image()
# ===================================================================


class TestFetchProductImage:
    def test_empty_url_returns_none(self):
        assert fetch_product_image("") is None

    def test_none_url_returns_none(self):
        assert fetch_product_image(None) is None

    def test_disallowed_host_returns_none(self):
        assert fetch_product_image("https://evil.com/page") is None

    def test_non_http_scheme_returns_none(self):
        assert fetch_product_image("ftp://jlcpcb.com/page") is None

    def test_rejects_internal_ip(self):
        assert fetch_product_image("http://169.254.169.254/metadata") is None

    def test_rejects_localhost(self):
        assert fetch_product_image("http://localhost/secret") is None

    def test_rejects_file_scheme(self):
        assert fetch_product_image("file:///etc/passwd") is None

    def test_success(self):
        html = '<img src="https://assets.lcsc.com/images/lcsc/900x900/abc.jpg">'
        img_bytes = b"\xff\xd8\xff\xe0fake-jpeg"
        call_count = [0]

        def fake_urlopen(req, timeout=30):
            if call_count[0] == 0:
                call_count[0] += 1
                return _FakeResponse(html.encode())
            else:
                return _FakeResponse(img_bytes)

        with mock.patch.object(api, "_urlopen", side_effect=fake_urlopen):
            result = fetch_product_image("https://jlcpcb.com/product/123")
            assert result == img_bytes

    def test_no_image_in_html(self):
        html = "<html>no image here</html>"
        with _mock_urlopen(html.encode()):
            assert fetch_product_image("https://jlcpcb.com/product/123") is None

    def test_network_error_returns_none(self):
        with mock.patch.object(api, "_urlopen", side_effect=APIError("fail")):
            assert fetch_product_image("https://lcsc.com/product/123") is None

    def test_image_download_error_returns_none(self):
        """Second _urlopen (image fetch) fails."""
        html = '<img src="https://assets.lcsc.com/images/lcsc/900x900/abc.jpg">'
        call_count = [0]

        def fake_urlopen(req, timeout=30):
            if call_count[0] == 0:
                call_count[0] += 1
                return _FakeResponse(html.encode())
            raise APIError("image download failed")

        with mock.patch.object(api, "_urlopen", side_effect=fake_urlopen):
            assert fetch_product_image("https://jlcpcb.com/product/123") is None


# ===================================================================
# 14. download_step() and download_wrl_source()
# ===================================================================


class TestDownloadStep:
    def test_success_plain(self):
        data = b"STEP file content"
        with _mock_urlopen(data):
            result = download_step("some-uuid")
            assert result == data

    def test_success_gzipped(self):
        raw = b"STEP file content"
        compressed = gzip.compress(raw)
        with _mock_urlopen(compressed):
            result = download_step("some-uuid")
            assert result == raw

    def test_network_error_returns_none(self):
        with mock.patch.object(api, "_urlopen", side_effect=APIError("fail")):
            assert download_step("bad-uuid") is None

    def test_http_error_returns_none(self):
        err = urllib.error.HTTPError("url", 404, "not found", {}, None)
        with mock.patch.object(api, "_urlopen", side_effect=err):
            assert download_step("bad-uuid") is None


class TestDownloadWrlSource:
    def test_success_plain(self):
        data = b"OBJ text content"
        with _mock_urlopen(data):
            result = download_wrl_source("some-uuid")
            assert result == "OBJ text content"

    def test_success_gzipped(self):
        raw = b"OBJ text content"
        compressed = gzip.compress(raw)
        with _mock_urlopen(compressed):
            result = download_wrl_source("some-uuid")
            assert result == "OBJ text content"

    def test_network_error_returns_none(self):
        with mock.patch.object(api, "_urlopen", side_effect=APIError("fail")):
            assert download_wrl_source("bad-uuid") is None


# ===================================================================
# 15. _strip_cjk_parens()
# ===================================================================


class TestStripCjkParens:
    def test_strips_cjk_parens(self):
        assert _strip_cjk_parens("UMW(友台半导体)") == "UMW"

    def test_no_cjk(self):
        assert _strip_cjk_parens("Texas Instruments") == "Texas Instruments"

    def test_empty_string(self):
        assert _strip_cjk_parens("") == ""

    def test_ascii_parens_preserved(self):
        assert _strip_cjk_parens("TI (Texas Instruments)") == "TI (Texas Instruments)"

    def test_multiple_cjk_groups(self):
        # Only CJK parenthesized groups should be stripped
        result = _strip_cjk_parens("Brand(中文)Extra(日本語)")
        assert result == "BrandExtra"


# ===================================================================
# 16. fetch_full_component() — integration-level
# ===================================================================


class TestFetchFullComponent:
    def test_success(self):
        uuids = [{"component_uuid": "sym1"}, {"component_uuid": "fp1"}]
        sym_data = {
            "title": "100R Resistor",
            "description": "SMD Resistor",
            "dataStr": {
                "head": {
                    "c_para": {
                        "pre": "R?",
                        "link": "https://example.com/ds.pdf",
                        "package": "0805",
                        "Manufacturer": "Yageo(国巨)",
                        "Manufacturer Part": "RC0805",
                    },
                    "x": 100,
                    "y": 200,
                }
            },
        }
        fp_data = {
            "title": "0805",
            "dataStr": {
                "head": {
                    "c_para": {"pre": "R?", "package": "0805"},
                    "uuid_3d": "3d-model-uuid",
                    "x": 300,
                    "y": 400,
                }
            },
        }
        with mock.patch.object(api, "fetch_component_uuids", return_value=uuids):
            with mock.patch.object(api, "fetch_component_data", side_effect=[fp_data, sym_data]):
                result = api.fetch_full_component("C12345")
                assert result["lcsc_id"] == "C12345"
                assert result["prefix"] == "R"
                assert result["manufacturer"] == "Yageo"
                assert result["uuid_3d"] == "3d-model-uuid"
                assert result["datasheet"] == "https://example.com/ds.pdf"
                assert result["footprint_uuid"] == "fp1"
                assert result["symbol_uuids"] == ["sym1"]

    def test_datasheet_protocol_relative(self):
        uuids = [{"component_uuid": "sym1"}, {"component_uuid": "fp1"}]
        sym_data = {
            "dataStr": {"head": {"c_para": {"pre": "U?", "link": "//example.com/ds.pdf"}}},
        }
        fp_data = {"dataStr": {"head": {"c_para": {}}}}
        with mock.patch.object(api, "fetch_component_uuids", return_value=uuids):
            with mock.patch.object(api, "fetch_component_data", side_effect=[fp_data, sym_data]):
                result = api.fetch_full_component("C1")
                assert result["datasheet"] == "https://example.com/ds.pdf"

    def test_datasheet_invalid_url(self):
        uuids = [{"component_uuid": "sym1"}, {"component_uuid": "fp1"}]
        sym_data = {
            "dataStr": {"head": {"c_para": {"pre": "U?", "link": "not-a-url"}}},
        }
        fp_data = {"dataStr": {"head": {"c_para": {}}}}
        with mock.patch.object(api, "fetch_component_uuids", return_value=uuids):
            with mock.patch.object(api, "fetch_component_data", side_effect=[fp_data, sym_data]):
                result = api.fetch_full_component("C1")
                assert result["datasheet"] == ""
