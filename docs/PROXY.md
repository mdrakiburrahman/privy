# HTTP proxy

Privy can expose an HTTP service bound to the listener machine through a local client port.

## Listener side

Suppose the remote machine serves an application at `http://127.0.0.1:8080`:

```bash
privy server --proxy-target http://127.0.0.1:8080
```

Python:

```python
from privy import RelayServer

RelayServer(
    namespace="my-relay",
    path="privy",
    token=listen_token,
    proxy_target="http://127.0.0.1:8080",
).serve_forever()
```

## Client side

```bash
privy proxy --local-port 3000
```

Open `http://127.0.0.1:3000`. The local server serializes each browser request, posts it through Azure Relay, and returns the remote HTTP status, headers, and body.

Python:

```python
from privy import ProxyClientServer

ProxyClientServer(
    namespace="my-relay",
    path="privy",
    token=send_token,
    local_port=3000,
).serve_forever()
```

## Behavior

- Request and response bodies are base64 encoded, preserving binary data.
- `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, and browser preflight `OPTIONS` are supported.
- Hop-by-hop headers are not forwarded.
- The client adds `Access-Control-Allow-Origin: *` for local development.
- Upstream HTTP errors preserve their status and response body.
- Relay transport failures return a local `502`.
- Token providers are resolved for every proxied request.

The local proxy binds to loopback only. It does not add application authentication to the remote service; protect the Relay credentials and do not expose the local port beyond trusted callers.
