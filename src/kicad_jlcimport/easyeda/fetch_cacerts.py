"""Fetch root CA certificates for JLCPCB/EasyEDA/LCSC endpoints.

Run this to generate the bundled cacerts.pem used by api.py for TLS
verification.  This eliminates the need for the system certificate store
(which is often broken in KiCad's bundled Python) and removes any
unverified-HTTPS fallback.

Usage:
    python -m kicad_jlcimport.easyeda.fetch_cacerts
"""

import base64
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.request

# Every host the plugin connects to (see api.py, _ALLOWED_IMAGE_HOSTS and _ALLOWED_DIRECT_IMAGE_HOSTS).
_HOSTS = [
    "easyeda.com",
    "modules.easyeda.com",
    "jlcpcb.com",
    "www.jlcpcb.com",
    "lcsc.com",
    "www.lcsc.com",
    "assets.lcsc.com",
    "so.szlcsc.com",
    "alimg.szlcsc.com",
]

_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cacerts.pem")


def _extract_pem_blocks(text: str) -> list:
    """Extract all PEM certificate blocks from text."""
    blocks = []
    current: list = []
    in_block = False
    for line in text.splitlines():
        if "-----BEGIN CERTIFICATE-----" in line:
            in_block = True
            current = [line]
        elif "-----END CERTIFICATE-----" in line:
            current.append(line)
            blocks.append("\n".join(current))
            in_block = False
        elif in_block:
            current.append(line)
    return blocks


def _is_self_signed(pem: str) -> bool:
    """Check if a PEM certificate is self-signed (subject == issuer)."""
    result = subprocess.run(
        ["openssl", "x509", "-noout", "-subject", "-issuer"],
        input=pem.encode(),
        capture_output=True,
        timeout=5,
    )
    lines = result.stdout.decode().strip().splitlines()
    if len(lines) < 2:
        return False
    subject = lines[0].replace("subject=", "").strip()
    issuer = lines[1].replace("issuer=", "").strip()
    return subject == issuer


def _get_aia_issuer_url(pem: str) -> str:
    """Extract the CA Issuers URL from a certificate's AIA extension."""
    result = subprocess.run(
        ["openssl", "x509", "-noout", "-text"],
        input=pem.encode(),
        capture_output=True,
        timeout=5,
    )
    text = result.stdout.decode("utf-8", errors="replace")
    match = re.search(r"CA Issuers - URI:(http\S+)", text)
    return match.group(1) if match else ""


def _der_to_pem(der_bytes: bytes) -> str:
    """Convert DER-encoded certificate to PEM."""
    b64 = base64.b64encode(der_bytes).decode("ascii")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----"


def _get_subject(pem: str) -> str:
    """Return the subject DN of a PEM certificate."""
    result = subprocess.run(
        ["openssl", "x509", "-noout", "-subject"],
        input=pem.encode(),
        capture_output=True,
        timeout=5,
    )
    return result.stdout.decode().replace("subject=", "").strip()


def _get_issuer(pem: str) -> str:
    """Return the issuer DN of a PEM certificate."""
    result = subprocess.run(
        ["openssl", "x509", "-noout", "-issuer"],
        input=pem.encode(),
        capture_output=True,
        timeout=5,
    )
    return result.stdout.decode().replace("issuer=", "").strip()


def _find_root_in_certifi(pem: str) -> str:
    """Find the root CA for *pem* in certifi's CA bundle.

    Handles two cases:
    - Cross-signed certs: looks for a self-signed cert with the same subject
    - Regular intermediates: looks for a self-signed cert matching the issuer
    """
    try:
        import certifi
    except ImportError:
        return ""

    subject = _get_subject(pem)
    issuer = _get_issuer(pem)

    with open(certifi.where()) as f:
        candidates = _extract_pem_blocks(f.read())

    # First try: self-signed cert with same subject (cross-signed case)
    for c in candidates:
        if _is_self_signed(c) and _get_subject(c) == subject:
            return c

    # Second try: self-signed cert whose subject matches our issuer
    for c in candidates:
        if _is_self_signed(c) and _get_subject(c) == issuer:
            return c

    return ""


def _resolve_root(pem: str) -> str:
    """Follow the AIA issuer chain until we reach a self-signed root CA.

    Falls back to searching certifi's bundle when no AIA CA Issuers URL
    is available (e.g. cross-signed certificates like USERTrust RSA).
    """
    visited = set()
    current = pem
    while not _is_self_signed(current):
        aia_url = _get_aia_issuer_url(current)
        if not aia_url:
            # No AIA — try certifi's bundle as fallback
            root = _find_root_in_certifi(current)
            if root:
                return root
            raise RuntimeError(
                "Cannot resolve root CA: no AIA CA Issuers URL and no matching root found in certifi bundle"
            )
        if aia_url in visited:
            raise RuntimeError(f"AIA chain loop detected at {aia_url}")
        visited.add(aia_url)
        resp = urllib.request.urlopen(aia_url, timeout=10)  # noqa: S310
        issuer_bytes = resp.read()
        # AIA responses are typically DER-encoded
        if b"-----BEGIN CERTIFICATE-----" in issuer_bytes:
            current = issuer_bytes.decode("ascii")
        else:
            current = _der_to_pem(issuer_bytes)
    return current


def _get_root_ca_pem(hostname: str, port: int = 443) -> str:
    """Connect to *hostname* and return the root CA certificate as PEM text.

    Gets the server's certificate chain via ``openssl s_client``, takes the
    highest cert, and follows the AIA CA Issuers chain until a self-signed
    root is reached.  Servers typically don't send the root itself, so just
    taking the last cert from the chain (as we did before) gives an
    intermediate, not the root.
    """
    # Verify connectivity with Python's ssl first
    ctx = ssl.create_default_context()
    with ctx.wrap_socket(socket.socket(), server_hostname=hostname) as s:
        s.settimeout(10)
        s.connect((hostname, port))

    # Get the server-sent chain via openssl
    result = subprocess.run(
        [
            "openssl",
            "s_client",
            "-connect",
            f"{hostname}:{port}",
            "-servername",
            hostname,
            "-showcerts",
        ],
        input=b"",
        capture_output=True,
        timeout=15,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    pem_blocks = _extract_pem_blocks(stdout)
    if not pem_blocks:
        raise RuntimeError(f"No certificates returned for {hostname}:{port}")

    # The last block is the highest cert the server sent.
    # If it's self-signed, it's already the root.
    # Otherwise, follow the AIA chain to the actual root.
    highest = pem_blocks[-1]
    return _resolve_root(highest)


def main() -> None:
    seen_pems: set = set()
    unique_roots: list = []

    for host in _HOSTS:
        print(f"  {host} ... ", end="", flush=True)
        try:
            pem = _get_root_ca_pem(host)
            if pem not in seen_pems:
                seen_pems.add(pem)
                unique_roots.append((host, pem))
                print("OK (new root CA)")
            else:
                print("OK (duplicate, skipped)")
        except Exception as e:
            print(f"FAILED: {e}")
            print(f"  !! Could not retrieve root CA for {host}", file=sys.stderr)
            sys.exit(1)

    with open(_OUTPUT, "w") as f:
        f.write("# Root CA certificates for JLCPCB/EasyEDA/LCSC endpoints.\n")
        f.write("# Generated by: python -m kicad_jlcimport.easyeda.fetch_cacerts\n")
        f.write(f"# Hosts: {', '.join(_HOSTS)}\n\n")
        for host, pem in unique_roots:
            f.write(f"# First seen for: {host}\n")
            f.write(pem + "\n\n")

    print(f"\nWrote {len(unique_roots)} unique root CA(s) to {_OUTPUT}")


if __name__ == "__main__":
    main()
