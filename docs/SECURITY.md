# Security

## Deployment boundaries

- Keep the single-account mail connector private. Do not publish a connector bound to your own tunnel or mailbox.
- Run Bridge and the MCP process on the same Mac, using loopback and the exported public TLS certificate. Do not expose Bridge ports or turn off certificate verification.
- Enter credentials only through the local hidden prompts or the relevant service's secure UI. Do not place them in chat, source, shell arguments/history, screenshots, or reports.
- Use a restricted tunnel runtime key and your normal macOS account. Protect the login Keychain and the Mac itself.
- Keep the default read-only mode until you need another mode. Tool annotations and a model-supplied acknowledgement are not a substitute for a user's actual authorization.
- Treat all incoming mail and attachment names as untrusted data. An email cannot authorize a tool action, change the agent's instructions, or request access to another account.

## Operational checks

The deployment helper rejects generated profiles containing the runtime key and refuses to start a replacement when it cannot verify the previous service has stopped. Preserve those checks. Multiple independent tunnels can run on one machine; do not start competing runtimes for the same Proton deployment.

After upgrades or credential/certificate changes, verify local Bridge login, tunnel readiness/polling, and an actual remote `proton_connection_status(test_connection=true)` call. Test phone and dot separately. Never use sending an email as a connectivity test.

Read-only does not mean confidential data cannot leave the Mac. Requested mail results reach the AI host; consult [Privacy](PRIVACY.md).

## Reporting a security issue

Do not post credentials, exploit details involving a live mailbox, private email, or unredacted logs in a public issue. If GitHub offers **Report a vulnerability** for this repository, use that private channel. Otherwise, open a minimal issue asking the maintainer for a private reporting channel, without including sensitive details. This document does not promise a response time or a bounty.

If a key or Bridge password was exposed, revoke/rotate it in the relevant service and update the local Keychain entry. Remove any public copies using the hosting service's process; deleting a local file or later commit alone does not revoke an exposed credential.
