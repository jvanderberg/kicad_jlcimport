"""Extended tests for api.py to improve coverage."""

import json
import ssl
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from kicad_jlcimport.easyeda import api
from kicad_jlcimport.easyeda.api import APIError


class TestMakeSslContext:
    """Tests for _make_ssl_context."""

    def test_creates_verified_context(self):
        # SSL context is lazily initialized; trigger it via _get_ssl_ctx()
        ctx = api._get_ssl_ctx()
        assert ctx is not None
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_prefers_bundled_cacerts(self, monkeypatch, tmp_path):
        """When cacerts.pem exists and is valid, it should be used."""
        import os

        # Use the project's own cacerts.pem as a known-good CA bundle
        project_pem = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "src", "kicad_jlcimport", "easyeda", "cacerts.pem"
        )
        pem = tmp_path / "cacerts.pem"
        pem.write_text(open(project_pem).read())
        monkeypatch.setattr(api, "_CACERTS_PEM", str(pem))

        ctx = api._make_ssl_context()
        assert ctx is not None
        assert ctx.check_hostname is True

    def test_falls_back_to_certifi(self, monkeypatch, tmp_path):
        """When no cacerts.pem exists, certifi should be tried."""
        import os

        monkeypatch.setattr(api, "_CACERTS_PEM", "/nonexistent/cacerts.pem")

        # certifi may not be installed; provide a fake module using the project's real CA bundle
        project_pem = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "src", "kicad_jlcimport", "easyeda", "cacerts.pem"
        )
        fake_certifi = MagicMock()
        fake_certifi.where.return_value = project_pem

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "certifi":
                return fake_certifi
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        ctx = api._make_ssl_context()
        assert ctx is not None
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_returns_none_when_nothing_available(self, monkeypatch):
        """When all CA sources fail, returns None."""
        monkeypatch.setattr(api, "_CACERTS_PEM", "/nonexistent/cacerts.pem")
        # Block certifi import
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "certifi":
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        # Block system store
        monkeypatch.setattr(ssl, "create_default_context", lambda: (_ for _ in ()).throw(OSError("mocked")))

        ctx = api._make_ssl_context()
        assert ctx is None

    def test_certifi_load_failure_falls_through(self, monkeypatch, tmp_path):
        """When certifi is importable but load_verify_locations fails, fall through to system store."""
        monkeypatch.setattr(api, "_CACERTS_PEM", "/nonexistent/cacerts.pem")

        # Create a file with invalid cert content so load_verify_locations fails
        bad_pem = tmp_path / "bad_certifi.pem"
        bad_pem.write_text("this is not a valid certificate")

        fake_certifi = MagicMock()
        fake_certifi.where.return_value = str(bad_pem)

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "certifi":
                return fake_certifi
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        # System store fallback should still work
        ctx = api._make_ssl_context()
        assert ctx is not None


class TestUrlopen:
    """Tests for _urlopen function."""

    def test_urlopen_uses_verified_context(self, monkeypatch):
        mock_response = MagicMock()
        # Ensure a verified context is set
        monkeypatch.setattr(api, "_SSL_CTX", ssl.create_default_context())

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            api._urlopen("https://example.com", timeout=10)
            assert mock_urlopen.called
            _, kwargs = mock_urlopen.call_args
            assert kwargs["context"].check_hostname is True

    def test_urlopen_warns_when_no_context(self, monkeypatch):
        monkeypatch.setattr(api, "_SSL_CTX", None)

        mock_response = MagicMock()
        with patch("urllib.request.urlopen", return_value=mock_response):
            with pytest.warns(UserWarning, match="No TLS certificate source"):
                api._urlopen("https://example.com", timeout=10)

    def test_urlopen_unverified_when_no_context(self, monkeypatch):
        monkeypatch.setattr(api, "_SSL_CTX", None)

        mock_response = MagicMock()
        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                api._urlopen("https://example.com", timeout=10)
            _, kwargs = mock_urlopen.call_args
            # Should use an unverified context as last resort
            assert kwargs["context"].check_hostname is False


class TestGetJson:
    """Tests for _get_json function."""

    def test_get_json_http_error(self, monkeypatch):
        def raise_http_error(*args, **kwargs):
            raise urllib.error.HTTPError("https://example.com", 404, "Not Found", {}, None)

        with patch.object(api, "_urlopen", raise_http_error):
            with pytest.raises(APIError, match="HTTP 404"):
                api._get_json("https://example.com/test")

    def test_get_json_url_error(self, monkeypatch):
        def raise_url_error(*args, **kwargs):
            raise urllib.error.URLError("Connection refused")

        with patch.object(api, "_urlopen", raise_url_error):
            with pytest.raises(APIError, match="Network error"):
                api._get_json("https://example.com/test")

    def test_get_json_success(self, monkeypatch):
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b'{"key": "value"}'

        with patch.object(api, "_urlopen", return_value=mock_response):
            result = api._get_json("https://example.com/test")
            assert result == {"key": "value"}


class TestFetchComponentUuids:
    """Tests for fetch_component_uuids function."""

    def test_fetch_component_uuids_success(self, monkeypatch):
        mock_data = {
            "success": True,
            "result": [
                {"component_uuid": "uuid1"},
                {"component_uuid": "uuid2"},
            ],
        }

        with patch.object(api, "_get_json", return_value=mock_data):
            result = api.fetch_component_uuids("C123")
            assert len(result) == 2
            assert result[0]["component_uuid"] == "uuid1"

    def test_fetch_component_uuids_not_found(self, monkeypatch):
        mock_data = {"success": False, "result": None}

        with patch.object(api, "_get_json", return_value=mock_data):
            with pytest.raises(APIError, match="No component found"):
                api.fetch_component_uuids("C999999")

    def test_fetch_component_uuids_empty_result(self, monkeypatch):
        mock_data = {"success": True, "result": []}

        with patch.object(api, "_get_json", return_value=mock_data):
            with pytest.raises(APIError, match="No component found"):
                api.fetch_component_uuids("C999999")


class TestFetchComponentData:
    """Tests for fetch_component_data function."""

    def test_fetch_component_data_success(self, monkeypatch):
        mock_data = {
            "result": {
                "title": "Test Component",
                "dataStr": {"shape": []},
            }
        }

        with patch.object(api, "_get_json", return_value=mock_data):
            result = api.fetch_component_data("uuid123")
            assert result["title"] == "Test Component"

    def test_fetch_component_data_not_found(self, monkeypatch):
        mock_data = {"result": None}

        with patch.object(api, "_get_json", return_value=mock_data):
            with pytest.raises(APIError, match="No data for component UUID"):
                api.fetch_component_data("invalid_uuid")


class TestSearchComponents:
    """Tests for search_components function."""

    def test_search_components_success(self, monkeypatch):
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = json.dumps(
            {
                "data": {
                    "componentPageInfo": {
                        "total": 1,
                        "list": [
                            {
                                "componentCode": "C123",
                                "componentName": "Resistor",
                                "componentModelEn": "100K",
                                "componentBrandEn": "ACME",
                                "componentSpecificationEn": "0402",
                                "componentTypeEn": "Resistors",
                                "stockCount": 1000,
                                "componentLibraryType": "base",
                                "componentPrices": [{"productPrice": 0.01}],
                                "describe": "100K Resistor",
                                "lcscGoodsUrl": "https://lcsc.com/product",
                                "dataManualUrl": "https://datasheet.com",
                            }
                        ],
                    }
                }
            }
        ).encode()

        with patch.object(api, "_urlopen", return_value=mock_response):
            result = api.search_components("resistor", page=1, page_size=10)
            assert result["total"] == 1
            assert len(result["results"]) == 1
            assert result["results"][0]["lcsc"] == "C123"
            assert result["results"][0]["type"] == "Basic"
            assert result["results"][0]["price"] == 0.01

    def test_search_components_extended_type(self, monkeypatch):
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = json.dumps(
            {
                "data": {
                    "componentPageInfo": {
                        "total": 1,
                        "list": [
                            {
                                "componentCode": "C456",
                                "componentName": "Capacitor",
                                "componentModelEn": "100uF",
                                "componentBrandEn": "ACME",
                                "componentSpecificationEn": "0805",
                                "componentTypeEn": "Capacitors",
                                "stockCount": 500,
                                "componentLibraryType": "expand",
                                "componentPrices": [],
                                "describe": "100uF Capacitor",
                                "lcscGoodsUrl": "",
                                "dataManualUrl": "",
                            }
                        ],
                    }
                }
            }
        ).encode()

        with patch.object(api, "_urlopen", return_value=mock_response):
            result = api.search_components("capacitor")
            assert result["results"][0]["type"] == "Extended"
            assert result["results"][0]["price"] is None

    def test_search_components_error(self, monkeypatch):
        def raise_error(*args, **kwargs):
            raise urllib.error.HTTPError("url", 500, "Server Error", {}, None)

        with patch.object(api, "_urlopen", raise_error):
            with pytest.raises(APIError, match="Search failed"):
                api.search_components("test")

    def test_search_components_empty_data(self, monkeypatch):
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b'{"data": null}'

        with patch.object(api, "_urlopen", return_value=mock_response):
            result = api.search_components("nonexistent")
            assert result["total"] == 0
            assert result["results"] == []


class TestDownloadStep:
    """Tests for download_step function."""

    def test_download_step_success(self, monkeypatch):
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b"STEP file content"

        with patch.object(api, "_urlopen", return_value=mock_response):
            result = api.download_step("uuid123")
            assert result == b"STEP file content"

    def test_download_step_http_error(self, monkeypatch):
        def raise_error(*args, **kwargs):
            raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)

        with patch.object(api, "_urlopen", raise_error):
            result = api.download_step("invalid_uuid")
            assert result is None

    def test_download_step_url_error(self, monkeypatch):
        def raise_error(*args, **kwargs):
            raise urllib.error.URLError("Connection refused")

        with patch.object(api, "_urlopen", raise_error):
            result = api.download_step("uuid123")
            assert result is None


class TestDownloadWrlSource:
    """Tests for download_wrl_source function."""

    def test_download_wrl_source_success(self, monkeypatch):
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b"v 0 0 0\nf 1 2 3"

        with patch.object(api, "_urlopen", return_value=mock_response):
            result = api.download_wrl_source("uuid123")
            assert result == "v 0 0 0\nf 1 2 3"

    def test_download_wrl_source_error(self, monkeypatch):
        def raise_error(*args, **kwargs):
            raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)

        with patch.object(api, "_urlopen", raise_error):
            result = api.download_wrl_source("invalid_uuid")
            assert result is None


class TestFetchFullComponent:
    """Tests for fetch_full_component function."""

    def test_fetch_full_component_success(self, monkeypatch):
        mock_uuids = [
            {"component_uuid": "sym_uuid"},
            {"component_uuid": "fp_uuid"},
        ]

        mock_sym_data = {
            "title": "Test IC",
            "description": "Test description",
            "dataStr": {
                "shape": [],
                "head": {
                    "x": 100,
                    "y": 200,
                    "c_para": {
                        "pre": "U?",
                        "link": "//datasheet.com/test.pdf",
                        "Manufacturer": "ACME",
                        "Manufacturer Part": "MPN123",
                    },
                },
            },
        }

        mock_fp_data = {
            "title": "Test IC",
            "dataStr": {
                "shape": [],
                "head": {
                    "x": 0,
                    "y": 0,
                    "uuid_3d": "3d_uuid",
                    "c_para": {"pre": "U?"},
                },
            },
        }

        with patch.object(api, "fetch_component_uuids", return_value=mock_uuids):
            with patch.object(api, "fetch_component_data") as mock_fetch:
                mock_fetch.side_effect = [mock_fp_data, mock_sym_data]
                result = api.fetch_full_component("C123")

                assert result["title"] == "Test IC"
                assert result["prefix"] == "U"
                assert result["lcsc_id"] == "C123"
                assert result["datasheet"] == "https://datasheet.com/test.pdf"
                assert result["manufacturer"] == "ACME"
                assert result["manufacturer_part"] == "MPN123"
                assert result["uuid_3d"] == "3d_uuid"

    def test_fetch_full_component_no_symbol(self, monkeypatch):
        mock_uuids = [
            {"component_uuid": "fp_uuid"},
        ]

        mock_fp_data = {
            "title": "Test Footprint",
            "dataStr": {
                "shape": [],
                "head": {
                    "x": 0,
                    "y": 0,
                    "c_para": {"pre": "R?"},
                },
            },
        }

        with patch.object(api, "fetch_component_uuids", return_value=mock_uuids):
            with patch.object(api, "fetch_component_data", return_value=mock_fp_data):
                result = api.fetch_full_component("C456")

                assert result["title"] == "Test Footprint"
                assert result["prefix"] == "R"
                assert len(result["symbol_uuids"]) == 0
                assert len(result["symbol_data_list"]) == 0

    def test_fetch_full_component_datasheet_normalization(self, monkeypatch):
        """Test that datasheets are properly normalized to https URLs."""
        mock_uuids = [{"component_uuid": "fp_uuid"}]

        mock_fp_data = {
            "title": "Test",
            "dataStr": {
                "shape": [],
                "head": {
                    "x": 0,
                    "y": 0,
                    "c_para": {"pre": "R?", "link": "datasheet.com/test.pdf"},
                },
            },
        }

        with patch.object(api, "fetch_component_uuids", return_value=mock_uuids):
            with patch.object(api, "fetch_component_data", return_value=mock_fp_data):
                result = api.fetch_full_component("C789")
                # Links not starting with http or // should be empty
                assert result["datasheet"] == ""


class TestFetchProductImageExtended:
    """Extended tests for fetch_product_image."""

    def test_fetch_product_image_parse_error(self, monkeypatch):
        """Test handling of URL parse errors."""
        # Pass an invalid URL that might cause urlparse issues
        result = api.fetch_product_image("not-a-valid-url")
        assert result is None

    def test_fetch_product_image_network_error(self, monkeypatch):
        """Test handling of network errors."""

        def raise_error(*args, **kwargs):
            raise OSError("Network unreachable")

        with patch.object(api, "_urlopen", raise_error):
            result = api.fetch_product_image("https://jlcpcb.com/product/C123")
            assert result is None

    def test_fetch_product_image_no_image_match(self, monkeypatch):
        """Test when no product image URL is found in HTML."""
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b"<html><body>No image here</body></html>"

        with patch.object(api, "_urlopen", return_value=mock_response):
            result = api.fetch_product_image("https://jlcpcb.com/product/C123")
            assert result is None

    def test_fetch_product_image_invalid_image_domain(self, monkeypatch):
        """Test that non-assets.lcsc.com image URLs are rejected."""
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b'<html><img src="https://evil.com/images/lcsc/900x900/test.jpg"></html>'

        with patch.object(api, "_urlopen", return_value=mock_response):
            result = api.fetch_product_image("https://jlcpcb.com/product/C123")
            assert result is None

    def test_fetch_product_image_image_fetch_error(self, monkeypatch):
        """Test when product image fetch fails."""
        html_response = MagicMock()
        html_response.__enter__ = MagicMock(return_value=html_response)
        html_response.__exit__ = MagicMock(return_value=False)
        html_response.read.return_value = b'<img src="https://assets.lcsc.com/images/lcsc/900x900/test.jpg">'

        call_count = [0]

        def mock_urlopen(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return html_response
            raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)

        with patch.object(api, "_urlopen", mock_urlopen):
            result = api.fetch_product_image("https://jlcpcb.com/product/C123")
            assert result is None


class TestSSLCertError:
    """Tests for SSLCertError and the allow_unverified_ssl mechanism."""

    def test_ssl_cert_error_is_api_error_subclass(self):
        assert issubclass(api.SSLCertError, api.APIError)

    def test_ssl_cert_error_caught_by_api_error_handler(self):
        err = api.SSLCertError("cert failed")
        with pytest.raises(APIError):
            raise err

    def test_urlopen_raises_ssl_cert_error_on_verification_failure(self, monkeypatch):
        """_urlopen raises SSLCertError when the cert fails verification."""
        monkeypatch.setattr(api, "_SSL_CTX", ssl.create_default_context())
        monkeypatch.setattr(api, "_allow_unverified", False)

        cert_err = ssl.SSLCertVerificationError("certificate verify failed")
        url_err = urllib.error.URLError(cert_err)

        with patch("urllib.request.urlopen", side_effect=url_err):
            with pytest.raises(api.SSLCertError, match="TLS certificate verification failed"):
                api._urlopen("https://example.com", timeout=10)

    def test_urlopen_reraises_non_cert_url_error(self, monkeypatch):
        """_urlopen re-raises URLError normally for non-cert failures."""
        monkeypatch.setattr(api, "_SSL_CTX", ssl.create_default_context())
        monkeypatch.setattr(api, "_allow_unverified", False)

        url_err = urllib.error.URLError("Connection refused")

        with patch("urllib.request.urlopen", side_effect=url_err):
            with pytest.raises(urllib.error.URLError, match="Connection refused"):
                api._urlopen("https://example.com", timeout=10)

    def test_allow_unverified_ssl_enables_unverified_context(self, monkeypatch):
        """After allow_unverified_ssl(), _urlopen uses an unverified context."""
        monkeypatch.setattr(api, "_SSL_CTX", ssl.create_default_context())
        monkeypatch.setattr(api, "_allow_unverified", False)

        api.allow_unverified_ssl()
        assert api._allow_unverified is True

        mock_response = MagicMock()
        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            api._urlopen("https://example.com", timeout=10)
            _, kwargs = mock_urlopen.call_args
            assert kwargs["context"].check_hostname is False

        # Reset for other tests
        monkeypatch.setattr(api, "_allow_unverified", False)

    def test_full_flow_cert_error_then_allow_retry(self, monkeypatch):
        """Full flow: cert error → allow_unverified → retry succeeds."""
        monkeypatch.setattr(api, "_SSL_CTX", ssl.create_default_context())
        monkeypatch.setattr(api, "_allow_unverified", False)

        cert_err = ssl.SSLCertVerificationError("certificate verify failed")
        url_err = urllib.error.URLError(cert_err)
        mock_response = MagicMock()

        call_count = [0]

        def mock_urlopen(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise url_err
            return mock_response

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            # First call fails with SSLCertError
            with pytest.raises(api.SSLCertError):
                api._urlopen("https://example.com", timeout=10)

            # User or UI enables unverified SSL
            api.allow_unverified_ssl()

            # Second call succeeds
            result = api._urlopen("https://example.com", timeout=10)
            assert result is mock_response

        # Reset
        monkeypatch.setattr(api, "_allow_unverified", False)
