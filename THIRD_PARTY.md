# Third-party source

`server/vendor/proton_bridge_mcp.py` is an unchanged copy from
[miketigerblue/proton-bridge-mcp](https://github.com/miketigerblue/proton-bridge-mcp),
commit `1ba655f2922faea6cee4f8098e30c4caaefb9255`, upstream version 0.3.0.
The original MIT license is retained in `server/vendor/LICENSE`.
`server/requirements.txt` retains the hash-pinned dependency versions and hashes
from the same commit. Absolute developer-machine paths in comments were replaced
with relative source filenames; no dependency versions or hashes were changed.

The plugin exposes its own restricted facade, not every upstream tool. The upstream
bootstrap and client configuration installer are not included or run. The facade
forces loopback and verified TLS, uses macOS Keychain exclusively, always reads
without marking mail seen, and offers optional draft/send tools only in configured modes.

Read/search handling is reused from upstream. No claim is made that prompt-injection
markers or a model-supplied acknowledgement are a reliable human approval mechanism.
Messages returned through MCP are available to the AI host.

`keychain_store.py` uses Python's hidden terminal input for both Bridge passwords
and OpenAI runtime keys. It sends hex-encoded secret data to Apple's SecurityTool
through standard input, never as process arguments, and verifies the complete
value from the selected Keychain after writing. This avoids the 128-character
limit of the native `getpass(3)` prompt used by SecurityTool's trailing `-w`.
Apple's SecurityTool implementation is available in the
[Security source](https://github.com/apple-oss-distributions/Security).

## Native tunnel deployment

`deploy_mini.py` is a small integration helper for the separately installed
[OpenAI tunnel-client](https://github.com/openai/tunnel-client). The OpenAI binary
and source are not bundled or modified. The supported CLI flags, health response
fields, environment inheritance, and macOS foreground-service behavior were checked
against the installed 0.0.18 client and official source commit
`274a5588414819f063d27100a4fb1096aae3830f`.
Install the official macOS build through `brew install openai/tools/tunnel-client`.

The helper generates configuration through the official `init` command, validates
it with `doctor`, and runs the client in the foreground under a user LaunchAgent.
It does not reimplement the control-plane protocol or issue private raw API calls.
