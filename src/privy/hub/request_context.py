from contextvars import ContextVar

REQUEST_ID: ContextVar[str] = ContextVar("privy_request_id", default="-")
