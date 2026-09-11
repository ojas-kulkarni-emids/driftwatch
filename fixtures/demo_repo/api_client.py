"""A small, realistic client module used as the demo fixture for the
dependency-bump agent. Two libraries, two call sites each -- one call site
per library is genuinely touched by a real, documented breaking/security
change; the other is not. This contrast is what the agent needs to get
right to prove it isn't just flagging "library X was mentioned."

urllib3 v1 -> v2 removed HTTPResponse.getheader()/getheaders() (use the
.headers mapping instead) -- fetch_raw() below is touched, fetch_simple()
is not.

requests' 2.31.0 fixed CVE-2023-32681 (Proxy-Authorization header could
leak to a redirected host when a proxy was configured) -- fetch_with_proxy()
below is touched, fetch_plain() is not.
"""

import urllib3
import requests

http = urllib3.PoolManager()


def fetch_raw(url: str) -> tuple[bytes, str | None]:
    resp = http.request("GET", url)
    content_type = resp.getheader("Content-Type")
    return resp.data, content_type


def fetch_simple(url: str) -> bytes:
    resp = http.request("GET", url)
    return resp.data


def fetch_with_proxy(url: str, proxy_url: str) -> requests.Response:
    session = requests.Session()
    session.proxies = {"https": proxy_url}
    return session.get(url, allow_redirects=True)


def fetch_plain(url: str) -> requests.Response:
    return requests.get(url)
