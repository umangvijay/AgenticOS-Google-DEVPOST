# Managed standalone MCP builds

The factory now accepts `python_tools` through the existing durable build
service and `POST /api/v1/integrations/build-python-tools`. A user or agent
supplies named Python functions, tool schemas and validation fixtures. The
factory generates a standalone stdio server from these inputs. This is an
executable generated server, distinct from the declarative OpenAPI adapter.

## Request contract

```json
{
  "name": "Ledger total",
  "module": "def total(entries):\n    if not entries:\n        raise ValueError('No entries')\n    return {'total': sum(entries)}\n",
  "tools": [{
    "name": "total",
    "description": "Total the supplied ledger entries",
    "inputSchema": {
      "type": "object",
      "properties": {"entries": {"type": "array", "items": {"type": "number"}}},
      "required": ["entries"],
      "additionalProperties": false
    }
  }],
  "validation": {
    "total": {
      "valid_arguments": {"entries": [3, 7]},
      "expected": {"total": 10},
      "invalid_arguments": {"entries": "not an array"},
      "error_arguments": {"entries": []}
    }
  },
  "read_grants": []
}
```

Validation inputs are fixtures, not fixed production output. Each subsequent
invocation supplies its own arguments. Every tool needs a valid call with a
declared expected result, an input that violates its schema and a schema-valid
input that produces a tool error. The factory independently compares actual
responses with these declarations. This checks the supplied postconditions;
it does not prove correctness on every possible input.

Schemas support explicit object/array/string/number/integer/boolean/null types,
properties, required fields, boolean additionalProperties, typed array items,
enum and numeric/size bounds. Unsupported constraints are rejected. Host-side
invocation validates arguments with JSON Schema before executing generated code.
Modules use the runtime's Python standard library. There is no dynamic package
installation and no automatic credential injection.

## Execution and evidence

1. The normal admission service atomically persists the build, executable child
   task, optional parent wait and outbox event.
2. The worker generates `server.py`, the supplied function module and tool
   definitions. Their canonical digest also binds the runtime image and grants.
3. Each attempt gets its own disabled candidate, even for identical source;
   rebuilding cannot overwrite an active connector. Publication consumes a
   one-use token through a storage compare-and-set. A user enable/disable action
   invalidates that token; deletion cannot be undone by a finishing build.
   The container has no direct network,
   host mounts, Docker socket, validator source or signing key.
4. A supervisor-side client sends actual initialization, list and call messages
   over the container's stdio stream. It checks errors and process shutdown.
   Server-printed claims do not create validation results.
5. The trusted validator signs observed results and retains their runner
   execution reference. Revision, operation-validation and activation records
   are persisted through the ordinary connector services.
6. The parent is resumed through its durable wait and invokes the exact tested
   artifact via ToolRouter. Changed bytes, schemas, grants, image or signature
   cannot reuse the previous authorization.
7. Workflow run/task/attempt identities are retained in the runner request.
   Losing the task lease or cancellation terminates its container. A shared
   PostgreSQL journal permits cancellation from another process and restricts
   cleanup to the owning runtime host.

The wire contract follows [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
and [MCP tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
The supported protocol version is 2025-06-18. Streamable HTTP deployment is not
claimed by this implementation.

## Anonymous HTTPS reads

An optional `read_grants` list names exact HTTPS origins, for example
`["https://example.com"]`. The generated module can use
`from agentos_http import get`, then call `get(url)` to obtain
`{"status_code": ..., "body": ...}`. This is a negotiated AgentOS experimental
MCP client capability, `agentos/http_read`; ordinary clients without it cannot
provide this brokered read feature.

The container remains network-disabled. Its request crosses the stdio boundary;
the host validates the grant and public DNS answers, connects to the vetted
numeric address while verifying TLS against the original hostname, bounds
response size, and validates each redirect. Metadata/private destinations,
ungranted hosts or ports and direct sockets remain blocked. Requests are capped
at 20 per execution and only GET is exposed to generated code. No provider
secret is passed to the module. Credentialed operations and writes remain outside
this managed read product; their separate effect/credential gates remain required.
Each read has a 30-second total deadline capped by the execution deadline,
two-second DNS/network waits, and cancellation checks before connections,
between response chunks and before redirects. Blocking OS resolver calls may
finish in the background, with at most 16 outstanding per supervisor; they cannot
make the cancelled request connect afterward.
Reads request uncompressed responses and reject compressed bodies before decoding,
so a small compressed payload cannot bypass the one-megabyte response limit.

## Configuration and qualification

Use the variables documented in `.env.example`. PostgreSQL migration 0021 must
be applied to the journal database first. The normal factory migration runner
applies it when the application uses that PostgreSQL database. SQLite metadata
is migrated idempotently at startup to preserve standalone artifacts and evidence.

The current runner uses a Docker engine accessible to the trusted supervisor.
This is a privileged infrastructure connection even when that supervisor's Unix
user is non-root. Generated workloads never receive the engine socket. The
default Compose and Helm API/worker profiles do not configure this connection;
their managed-build endpoint remains unavailable. The Compose `runner` service
alone does not create an RPC service or route ordinary workflow tasks to it.
Do not infer a working cloud runner from that service name or `RUNNER_WORKER`.
An approved deployment must explicitly supply an isolated supervisor topology
and configure every process that executes these tasks, or implement a separate
authenticated runner service. A standard Cloud Run container is not the local
Docker-engine topology exercised here. Shared PostgreSQL journaling does not
remove that deployment requirement.

Local tests cover actual containers, stdio messages, independent inputs, tamper
refusal, cancellation, public HTTPS broker reads, private-destination denial and
cross-process shared-journal cancellation. They do not establish cloud deployment,
all enabled agents, hosted-provider qualification, canary/rollback or real pilot
acceptance. Those remain in the Phase 26 release register.
