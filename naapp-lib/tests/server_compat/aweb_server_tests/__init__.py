"""aweb's server test helpers at server-v1.27.53, one module per aweb file."""

# Module names that copied definitions import inside their bodies, as aweb
# lays its tests out, with the copy each one names.
AWEB_MODULE_NAMES = {
    'test_messages_http': 'messages_http',
}
