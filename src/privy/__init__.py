"""privy — remote Python, Bash, and PowerShell execution over Azure Relay."""

from privy._relay import (
    RelayTokenAudienceError,
    RelayTokenError,
    RelayTokenExpiredError,
    create_sas_token,
    parse_sas_token,
    validate_sas_token,
)
from privy.batch import (
    DEFAULT_MAX_PARALLEL,
    BatchManifest,
    BatchResult,
    BatchValidationError,
    CommandOutcome,
    CommandSpec,
    parse_batch_manifest,
)
from privy.client import RELAY_RESPONSE_LIMIT_S, ExecResult, RelayClient
from privy.executor import cancel_job, poll_job, submit_job
from privy.protocol import ExecRequest, ExecResponse
from privy.proxy import ProxyClientServer
from privy.server import RelayServer
from privy.transfer import (
    DEFAULT_CHUNK_SIZE,
    MAX_CHUNK_SIZE,
    TransferError,
    TransferRequest,
    TransferResponse,
    TransferResult,
)

__all__ = [
    "RELAY_RESPONSE_LIMIT_S",
    "DEFAULT_MAX_PARALLEL",
    "BatchManifest",
    "BatchResult",
    "BatchValidationError",
    "CommandOutcome",
    "CommandSpec",
    "ExecRequest",
    "ExecResponse",
    "ExecResult",
    "DEFAULT_CHUNK_SIZE",
    "MAX_CHUNK_SIZE",
    "ProxyClientServer",
    "RelayClient",
    "RelayServer",
    "RelayTokenAudienceError",
    "RelayTokenError",
    "RelayTokenExpiredError",
    "TransferError",
    "TransferRequest",
    "TransferResponse",
    "TransferResult",
    "cancel_job",
    "create_sas_token",
    "parse_sas_token",
    "parse_batch_manifest",
    "poll_job",
    "submit_job",
    "validate_sas_token",
]

__version__ = "0.1.0"
