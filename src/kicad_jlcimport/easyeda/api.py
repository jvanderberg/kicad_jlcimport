"""EasyEDA/LCSC HTTP client using only urllib."""

import gzip
import json
import logging
import os
import re
import socket
import ssl
import sys
import urllib.request
import warnings
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DNS cache — avoids repeated lookups that can trigger CDN rate-limiting.
# Transparent to urllib: SNI, Host headers, and cert verification all still
# use the original hostname.
#
# Two layers:
#   1. In-memory dict — avoids re-resolving within a single session.
#   2. Persistent JSON file — falls back to last known good IPs when DNS
#      fails entirely (e.g. CDN blocking automated lookups).
# ---------------------------------------------------------------------------
_dns_cache: Dict[tuple, list] = {}
_original_getaddrinfo = socket.getaddrinfo


def _dns_cache_path() -> str:
    """Path to the persistent DNS cache file."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Preferences/kicad")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("APPDATA", ""), "kicad")
    else:
        base = os.path.expanduser("~/.config/kicad")
    return os.path.join(base, "jlcimport_dns_cache.json")


def _load_dns_cache() -> dict:
    """Load the persistent DNS cache from disk."""
    try:
        with open(_dns_cache_path(), encoding="utf-8") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return {}


def _save_dns_cache(cache: dict) -> None:
    """Persist the DNS cache to disk."""
    path = _dns_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass  # non-fatal — cache is best-effort


def _result_to_json(result: list) -> list:
    """Convert getaddrinfo result (list of tuples) to JSON-safe lists."""
    return [[fam, typ, proto, canon, list(addr)] for fam, typ, proto, canon, addr in result]


def _result_from_json(data: list) -> list:
    """Convert JSON lists back to getaddrinfo-style tuples."""
    return [(fam, typ, proto, canon, tuple(addr)) for fam, typ, proto, canon, addr in data]


def _cached_getaddrinfo(*args, **kwargs):
    key = (args, tuple(sorted(kwargs.items())))
    if key in _dns_cache:
        return _dns_cache[key]

    host = args[0] if args else ""
    try:
        result = _original_getaddrinfo(*args, **kwargs)
        _dns_cache[key] = result
        # Persist successful lookups keyed by hostname
        if host:
            disk = _load_dns_cache()
            disk[host] = _result_to_json(result)
            _save_dns_cache(disk)
        return result
    except socket.gaierror:
        # DNS failed — try the persistent cache
        if host:
            disk = _load_dns_cache()
            if host in disk:
                logger.debug("DNS lookup failed for %s, using cached address", host)
                result = _result_from_json(disk[host])
                _dns_cache[key] = result
                return result
        raise


socket.getaddrinfo = _cached_getaddrinfo

_CACERTS_PEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cacerts.pem")


def _make_ssl_context():
    """Create a verified SSL context from the best available CA source.

    Tries in order:
    1. Bundled cacerts.pem (endpoint-specific root CAs, works everywhere)
    2. certifi package (Mozilla's CA bundle)
    3. System certificate store (may be incomplete in KiCad's bundled Python)

    Returns None only if no certificate source is usable.
    """
    # 1. Bundled endpoint-specific CAs — preferred, works in KiCad's Python
    if os.path.isfile(_CACERTS_PEM):
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(cafile=_CACERTS_PEM)
            logger.debug("TLS: using bundled cacerts.pem")
            return ctx
        except Exception:
            logger.debug("TLS: cacerts.pem exists but failed to load", exc_info=True)

    # 2. certifi — good cross-platform fallback
    try:
        import certifi

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=certifi.where())
        logger.debug("TLS: using certifi CA bundle")
        return ctx
    except ImportError:
        pass
    except Exception:
        logger.debug("TLS: certifi available but failed to load", exc_info=True)

    # 3. System certificate store
    try:
        ctx = ssl.create_default_context()
        logger.debug("TLS: using system certificate store")
        return ctx
    except Exception:
        logger.debug("TLS: system certificate store unavailable", exc_info=True)

    return None


_SSL_CTX = _make_ssl_context()


def validate_lcsc_id(lcsc_id: str) -> str:
    """Validate and normalize an LCSC part number.

    Raises ValueError if the ID doesn't match the expected C<digits> format.
    """
    lcsc_id = lcsc_id.strip().upper()
    if not lcsc_id.startswith("C"):
        lcsc_id = "C" + lcsc_id
    if not re.match(r"^C\d{1,12}$", lcsc_id):
        raise ValueError(f"Invalid LCSC part number: {lcsc_id}")
    return lcsc_id


EASYEDA_API = "https://easyeda.com/api"
EASYEDA_3D_BUCKET = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y"
EASYEDA_3D_API = "https://easyeda.com/analyzer/api/3dmodel"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class APIError(Exception):
    """Raised when an API call fails."""

    pass


class SSLCertError(APIError):
    """Raised when TLS certificate verification fails.

    This is a subclass of APIError so existing ``except APIError`` handlers
    still work as a fallback.  UIs can catch ``SSLCertError`` first to show
    a targeted warning and offer an ``--insecure`` override.
    """

    pass


_allow_unverified = False


def allow_unverified_ssl():
    """Enable unverified HTTPS for the remainder of this process.

    Once called, ``_urlopen`` will skip certificate verification.  The flag
    is write-once (False → True) and never reset, so it is safe under the
    GIL without additional locking.
    """
    global _allow_unverified
    _allow_unverified = True


def _urlopen(req, timeout=30):
    """Open a URL with certificate verification.

    Uses the best available verified SSL context (bundled CAs → certifi →
    system store).  Falls back to unverified HTTPS **only** when no CA source
    could be loaded at all, and emits a warning each time this happens.

    When a verified context *is* available but the remote certificate cannot
    be validated, raises ``SSLCertError`` so the UI layer can decide how to
    handle it (e.g. prompt the user or accept ``--insecure``).

    If ``allow_unverified_ssl()`` has been called, all requests use an
    unverified context regardless of ``_SSL_CTX``.
    """
    if _allow_unverified:
        return urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())

    if _SSL_CTX is not None:
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise SSLCertError(
                    f"TLS certificate verification failed: {e.reason}. "
                    "A proxy or firewall may be intercepting HTTPS traffic. "
                    "Use --insecure to bypass certificate checks."
                ) from e
            raise

    warnings.warn(
        "No TLS certificate source available — using unverified HTTPS. "
        "Run 'python -m kicad_jlcimport.easyeda.fetch_cacerts' to fix this.",
        stacklevel=2,
    )
    return urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())


def _get_json(url: str) -> Any:
    """Fetch JSON from a URL."""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with _urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise APIError(f"HTTP {e.code} fetching {url}") from e
    except urllib.error.URLError as e:
        raise APIError(f"Network error fetching {url}: {e.reason}") from e
    return data


def fetch_component_uuids(lcsc_id: str) -> List[Dict[str, Any]]:
    """Get component UUIDs from LCSC part number.

    Returns list of dicts with 'component_uuid' keys.
    Last entry is footprint, earlier entries are symbol parts.
    """
    url = f"{EASYEDA_API}/products/{lcsc_id}/svgs"
    data = _get_json(url)
    if not data.get("success") or not data.get("result"):
        raise APIError(f"No component found for {lcsc_id}")
    return data["result"]


def fetch_component_data(uuid: str) -> Dict[str, Any]:
    """Get component shape data by UUID."""
    url = f"{EASYEDA_API}/components/{uuid}"
    data = _get_json(url)
    if not data.get("result"):
        raise APIError(f"No data for component UUID {uuid}")
    return data["result"]


JLCPCB_SEARCH_API = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"


def search_components(
    keyword: str, page: int = 1, page_size: int = 10, part_type: Optional[str] = None
) -> Dict[str, Any]:
    """Search JLCPCB parts library.

    Args:
        keyword: Search term
        page: Page number (1-based)
        page_size: Results per page
        part_type: Filter by "base" or "expand" (None = all)

    Returns dict with 'total' and 'results' list. Each result has:
        lcsc, name, model, brand, package, category, stock, type, price, description
    """
    payload = {
        "keyword": keyword,
        "currentPage": page,
        "pageSize": page_size,
    }
    if part_type:
        payload["componentLibraryType"] = part_type

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        JLCPCB_SEARCH_API,
        data=data,
        headers={
            **_HEADERS,
            "Content-Type": "application/json",
            "Origin": "https://jlcpcb.com",
            "Referer": "https://jlcpcb.com/parts",
        },
    )
    try:
        with _urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise APIError(f"Search failed: {e}") from e

    page_info = raw.get("data") or {}
    page_info = page_info.get("componentPageInfo") or {}
    total = page_info.get("total", 0)
    items = page_info.get("list") or []

    results = []
    for item in items:
        prices = item.get("componentPrices", [])
        unit_price = prices[0]["productPrice"] if prices else None
        results.append(
            {
                "lcsc": item.get("componentCode", ""),
                "name": item.get("componentName", ""),
                "model": item.get("componentModelEn", ""),
                "brand": item.get("componentBrandEn", ""),
                "package": item.get("componentSpecificationEn", ""),
                "category": item.get("componentTypeEn", ""),
                "stock": item.get("stockCount", 0),
                "type": "Basic" if item.get("componentLibraryType") == "base" else "Extended",
                "price": unit_price,
                "description": item.get("describe", ""),
                "url": item.get("lcscGoodsUrl", ""),
                "datasheet": item.get("dataManualUrl", ""),
            }
        )

    return {"total": total, "results": results}


def filter_by_min_stock(results: list, min_stock: int) -> list:
    """Filter search results by minimum stock count.

    Args:
        results: List of result dicts from search_components()
        min_stock: Minimum stock threshold (0 means no filter)

    Returns filtered list (original list unchanged).
    """
    if min_stock <= 0:
        return list(results)
    return [r for r in results if r.get("stock") and r["stock"] >= min_stock]


def filter_by_type(results: list, part_type: str) -> list:
    """Filter search results by part type.

    Args:
        results: List of result dicts from search_components()
        part_type: "Basic", "Extended", or None/empty for all

    Returns filtered list (original list unchanged).
    """
    if not part_type:
        return list(results)
    return [r for r in results if r.get("type") == part_type]


_ALLOWED_IMAGE_HOSTS = ("jlcpcb.com", "www.jlcpcb.com", "lcsc.com", "www.lcsc.com")


def fetch_product_image(lcsc_url: str) -> Optional[bytes]:
    """Fetch product image from LCSC/JLCPCB product page. Returns JPEG bytes or None."""
    if not lcsc_url:
        return None
    # SSRF protection: only allow fetching from known LCSC/JLCPCB domains
    try:
        from urllib.parse import urlparse

        parsed = urlparse(lcsc_url)
        if parsed.hostname not in _ALLOWED_IMAGE_HOSTS:
            return None
        if parsed.scheme not in ("http", "https"):
            return None
    except Exception:
        return None

    req = urllib.request.Request(
        lcsc_url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with _urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None

    # Find product image URL
    match = re.search(r'https://assets\.lcsc\.com/images/lcsc/900x900/[^\s"<>]+', html)
    if not match:
        return None

    img_url = match.group(0)
    if not img_url.startswith("https://assets.lcsc.com/"):
        return None
    req2 = urllib.request.Request(
        img_url,
        headers={
            "User-Agent": _UA,
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": lcsc_url,
        },
    )
    try:
        with _urlopen(req2, timeout=10) as resp:
            return resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


def download_step(uuid_3d: str) -> Optional[bytes]:
    """Download STEP file binary for a 3D model UUID."""
    url = f"{EASYEDA_3D_BUCKET}/{uuid_3d}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with _urlopen(req, timeout=60) as resp:
            data = resp.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None


def download_wrl_source(uuid_3d: str) -> Optional[str]:
    """Download OBJ-like text source for WRL conversion."""
    url = f"{EASYEDA_3D_API}/{uuid_3d}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with _urlopen(req, timeout=60) as resp:
            data = resp.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data.decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None


def _strip_cjk_parens(text: str) -> str:
    """Strip parenthesized CJK text from strings like 'UMW(友台半导体)'."""
    return re.sub(r"\([^\x00-\x7F]+\)", "", text).strip()


def fetch_full_component(lcsc_id: str) -> Dict[str, Any]:
    """High-level: fetch all data needed for a component.

    Returns dict with keys:
        title, prefix, lcsc_id, datasheet,
        symbol_uuids, footprint_uuid,
        symbol_data (list of shape data dicts),
        footprint_data (shape data dict),
        description, manufacturer, manufacturer_part
    """
    lcsc_id = validate_lcsc_id(lcsc_id)
    uuids = fetch_component_uuids(lcsc_id)

    # Last UUID is footprint, others are symbol parts
    footprint_uuid = uuids[-1]["component_uuid"]
    symbol_uuids = [u["component_uuid"] for u in uuids[:-1]]

    # Fetch footprint data (from the footprint UUID)
    fp_data = fetch_component_data(footprint_uuid)

    # Fetch symbol data (from first symbol UUID - contains packageDetail too)
    sym_data_list = []
    for suuid in symbol_uuids:
        sym_data_list.append(fetch_component_data(suuid))

    # Primary symbol data has the main metadata
    primary = sym_data_list[0] if sym_data_list else fp_data

    # Extract metadata from primary symbol's c_para
    c_para = {}
    if sym_data_list:
        c_para = primary.get("dataStr", {}).get("head", {}).get("c_para", {})

    # Fallback to footprint head c_para
    fp_c_para = fp_data.get("dataStr", {}).get("head", {}).get("c_para", {})

    prefix = c_para.get("pre", fp_c_para.get("pre", "U?"))
    if prefix.endswith("?"):
        prefix = prefix[:-1]

    datasheet = c_para.get("link", fp_c_para.get("link", ""))
    if datasheet and not datasheet.startswith("http"):
        datasheet = "https:" + datasheet if datasheet.startswith("//") else ""

    # 3D model UUID from footprint head
    uuid_3d = fp_data.get("dataStr", {}).get("head", {}).get("uuid_3d", "")

    return {
        "title": primary.get("title", fp_data.get("title", lcsc_id)),
        "prefix": prefix,
        "lcsc_id": lcsc_id,
        "datasheet": datasheet,
        "description": primary.get("description", ""),
        "package": c_para.get("package", fp_c_para.get("package", "")),
        "manufacturer": _strip_cjk_parens(c_para.get("Manufacturer", "")),
        "manufacturer_part": c_para.get("Manufacturer Part", ""),
        "symbol_uuids": symbol_uuids,
        "footprint_uuid": footprint_uuid,
        "symbol_data_list": sym_data_list,
        "footprint_data": fp_data,
        "uuid_3d": uuid_3d,
        "fp_origin_x": fp_data.get("dataStr", {}).get("head", {}).get("x", 0),
        "fp_origin_y": fp_data.get("dataStr", {}).get("head", {}).get("y", 0),
        "sym_origin_x": primary.get("dataStr", {}).get("head", {}).get("x", 0) if sym_data_list else 0,
        "sym_origin_y": primary.get("dataStr", {}).get("head", {}).get("y", 0) if sym_data_list else 0,
    }
