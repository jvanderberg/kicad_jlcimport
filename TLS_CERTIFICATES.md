# TLS Certificate Bundle (`cacerts.pem`)

## What this is

`cacerts.pem` is a minimal bundle of root CA certificates for the specific
HTTPS endpoints the plugin connects to. **Only the KiCad plugin uses it.**
KiCad's bundled Python uses its own OpenSSL and has no access to the macOS
system keychain or system certificate store, so it needs its own roots.

The plugin turns the bundle on at load time: `__init__.py` calls
`api.use_bundled_cacerts()` when `pcbnew` is importable. The CLI, the
standalone GUI and TUI, and the PyInstaller release binaries never call it, so
they verify against the operating system's certificates instead.

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

`api._make_ssl_context()` picks a certificate source depending on where it
runs.

**Inside KiCad** (after `use_bundled_cacerts()`):

1. **Bundled `cacerts.pem`** (this file)
2. **certifi**, if it's installed
3. **System certificate store**, via `ssl.create_default_context()`

**Everywhere else** (CLI, standalone GUI/TUI, release binaries):

1. **OS trust store via [`truststore`](https://pypi.org/project/truststore/)**,
   if it's installed. It calls the macOS Security framework, the Windows
   certificate store or the Linux system CA files directly. The release
   workflow installs it so the PyInstaller binaries get it; a frozen
   binary's own OpenSSL looks for certificates at the build machine's path,
   which usually doesn't exist on the user's machine. It also means
   corporate or proxy root CAs the OS trusts are honoured.
2. **System certificate store**, if OpenSSL loaded any CA certificates. This
   is the normal case for a pip install into a python.org, Homebrew or distro
   Python.
3. **certifi**, if it's installed
4. **System certificate store**, unconditionally

If none work, HTTPS requests proceed unverified with a warning. If Python's
`ssl` module can't be loaded at all (an OpenSSL mismatch in a Linux AppImage,
say), requests go through the system `curl` instead.

If a verified context exists but the server's certificate fails validation
(e.g. expired CA, MITM proxy), `SSLCertError` is raised. The plugin dialog
asks whether to continue without verification; the CLI and standalone GUI
take `--insecure`.

## Why not just use certifi or the system store in KiCad?

KiCad's bundled Python (OpenSSL 1.1.1n on macOS) cannot access the macOS
keychain, and certifi is not guaranteed to be installed. `truststore` needs
Python 3.10+, and KiCad on macOS bundles 3.9. A small bundled PEM file with
only the two root CAs we actually need is the most reliable option there.
