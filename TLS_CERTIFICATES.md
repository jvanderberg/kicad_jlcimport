# TLS Certificate Bundle (`cacerts.pem`)

## What this is

`cacerts.pem` is a minimal bundle of root CA certificates for the specific
HTTPS endpoints the plugin connects to. It allows TLS verification to work in
KiCad's bundled Python, which uses OpenSSL and has no access to the macOS
system keychain or system certificate store.

## Endpoints covered

| Host | Used for | Root CA |
|------|----------|---------|
| `easyeda.com` | Component data API | USERTrust RSA |
| `modules.easyeda.com` | 3D model downloads | USERTrust RSA |
| `jlcpcb.com` | Parts search API | ISRG Root X1 |
| `www.jlcpcb.com` | Product page fetch (images) | USERTrust RSA |
| `lcsc.com` | Product page fetch (images) | USERTrust RSA |
| `www.lcsc.com` | Product page fetch (images) | USERTrust RSA |
| `assets.lcsc.com` | Product image downloads | USERTrust RSA |
| `so.szlcsc.com` | China (SZLCSC) parts search | USERTrust RSA |
| `alimg.szlcsc.com` | China (SZLCSC) product images | USERTrust RSA |

The host list is defined in `fetch_cacerts.py` (`_HOSTS`). The image hosts
come from `api.py` (`_ALLOWED_IMAGE_HOSTS`, `_ALLOWED_DIRECT_IMAGE_HOSTS` and
the `assets.lcsc.com` pattern in `fetch_product_image()`). Both files live in
`src/kicad_jlcimport/easyeda/`.

## When to regenerate

Regenerate when:
- TLS verification starts failing (servers changed their CA)
- A new API endpoint is added to `api.py`
- Root CAs are approaching expiration

Current root CA expiry dates can be checked with:
```bash
openssl crl2pkcs7 -nocrl -certfile src/kicad_jlcimport/easyeda/cacerts.pem \
  | openssl pkcs7 -print_certs \
  | openssl x509 -noout -subject -enddate
```

## How to regenerate

```bash
python -m kicad_jlcimport.easyeda.fetch_cacerts
```

This connects to every endpoint, retrieves the server's certificate chain,
and resolves each chain to its self-signed root CA by:

1. Following AIA (Authority Information Access) CA Issuers URLs up the chain
2. Falling back to certifi's Mozilla CA bundle for certs without AIA
   (e.g. cross-signed intermediates)

The script requires:
- Network access to all endpoints
- `openssl` CLI on PATH
- `certifi` package installed (fallback only, `pip install certifi`)

After regenerating, test with KiCad's Python to confirm:
```bash
/path/to/kicad/python3 -c "
import ssl, urllib.request
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.load_verify_locations(cafile='src/kicad_jlcimport/easyeda/cacerts.pem')
for host in ['jlcpcb.com', 'easyeda.com', 'lcsc.com', 'assets.lcsc.com', 'so.szlcsc.com']:
    req = urllib.request.Request(f'https://{host}', headers={'User-Agent': 'test'})
    urllib.request.urlopen(req, timeout=10, context=ctx)
    print(f'{host}: OK')
"
```

## How verification works at runtime

`api._make_ssl_context()` tries certificate sources in order:

1. **Bundled `cacerts.pem`** (this file) — preferred, works in KiCad's Python
2. **certifi** package — fallback if cacerts.pem fails to load
3. **System certificate store** — fallback via `ssl.create_default_context()`

If none work, HTTPS requests proceed unverified with a warning. If Python's
`ssl` module can't be loaded at all (an OpenSSL mismatch in a Linux AppImage,
say), requests go through the system `curl` instead.

If a verified context exists but the server's certificate fails validation
(e.g. expired CA, MITM proxy), `SSLCertError` is raised. The plugin dialog
asks whether to continue without verification; the CLI and standalone GUI
take `--insecure`.

## Why not just use certifi or the system store?

KiCad's bundled Python (OpenSSL 1.1.1n on macOS) cannot access the macOS
keychain, and certifi is not guaranteed to be installed. A small bundled PEM
file with only the two root CAs we actually need is the most reliable option.
