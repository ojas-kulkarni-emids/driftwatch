"""Relative import from a sibling package, to exercise `from ..clients import`."""

from ..clients import http


def head(url):
    return http.request("HEAD", url)
