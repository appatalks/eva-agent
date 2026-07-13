"""Reserved deterministic private-network literals for no-network policy tests.

This is the sole CI exception for RFC 1918 examples. None identify a deployed
service, and tests must never attempt to connect to them.
"""

BARK_PRIVATE_URL = "https://192.168.1.2:8888"
LM_STUDIO_PRIVATE_URL = "http://192.168.1.10:1234/v1"
PRIVATE_IPV4_HOSTS = (
    "10.1.2.3",
    "172.16.0.1",
    "192.168.1.2",
)
