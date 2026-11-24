"""aweb server/tests/dashboard_test_support.py at server-v1.27.53, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa

AWEB_TAG = 'server-v1.27.53'
AWEB_PATH = 'server/tests/dashboard_test_support.py'
AWEB_BLOB = '7c385be349baa70e388c5c9705beaae95d1ac101'
COPIED = {
    '_JWT_SECRET': '530ebacff70de0a4a41d1c543922568863ccc3ed0a0378a4cb4da2816616fbe8',
}


_JWT_SECRET = "test-dashboard-secret-at-least-32bytes!"
