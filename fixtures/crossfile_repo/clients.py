"""Shared HTTP clients -- the pattern almost every real codebase uses, and the
one a per-file scanner cannot see through."""

import urllib3
from requests import Session

http = urllib3.PoolManager()
session = Session()
