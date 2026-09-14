"""Consumes the shared clients. Nothing here names urllib3 or requests, yet
every one of these lines is urllib3/requests usage."""

from clients import http, session


def fetch(url):
    resp = http.request("GET", url)
    return resp.getheader("Content-Type"), resp.data


def post(url, payload):
    return session.post(url, json=payload, verify=False)
