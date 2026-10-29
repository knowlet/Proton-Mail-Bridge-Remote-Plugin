# Privacy and data flow

This document describes this repository's self-hosted implementation. It does not replace Proton's, OpenAI's, or your AI client's privacy policies, nor describe hypothetical future hosted services.

## Who operates the connection

You run Proton Mail Bridge, the Python MCP process, and the official tunnel client on your Mac. You create a private tunnel and private ChatGPT custom MCP connection for your own account/workspace. This repository does not provide a maintainer-operated mailbox backend, a shared mailbox, or an account registration service.

The generic setup skill provides instructions. Installing that skill does not connect a mailbox or automatically return mail to the maintainer.

## What is processed

| Component | Data involved |
| --- | --- |
| Proton Mail Bridge | Your Proton account session, decrypted mail made available to local clients, and Bridge's own local synchronization/cache. See Proton's policies and Bridge settings. |
| Local MCP process | Bridge username, ports, trusted public certificate, enabled mode, and a Bridge password retrieved from macOS Keychain. Requested tools process mailbox names, message headers, search criteria, message plain text, and attachment metadata. |
| Official tunnel client | A runtime API key, tunnel identity, MCP requests/responses, and transport/health information. The key is loaded from Keychain into the running process environment. |
| ChatGPT or another AI host | Your prompts, tool arguments, and tool results, including decrypted mail returned by mail tools. Its product policies and your account/workspace settings govern that processing. |
| Optional draft/send modes | Recipients, subject, and message text submitted for a draft or sending. These modes are disabled by default. |

The code does not introduce a separate persistent mailbox database. Bridge and your AI host can retain data under their own behavior and policies. The mail reader returns plain text and attachment metadata, not attachment files; it does not load external message images. HTML-only messages may have no returned text body.

## Content leaves the Mac when requested

Bridge decrypts mail locally, but a remote mail-tool response travels through the OpenAI tunnel to the AI host. The tunnel's private network path does not mean email content remains only on your Mac. Even a read-only header/search result can reveal senders, recipients, subjects, or sensitive search terms.

`proton_connection_status(test_connection=true)` checks Bridge login without reading mail. Once you request message searches or reading, the corresponding results are passed to the assistant. Use narrow searches and read only mail needed for the task.

## Local storage and credentials

Configuration and runtime state are stored under `~/.config/proton-mail-bridge-plugin/`; dependencies are installed under `~/.local/share/proton-mail-bridge-plugin/`. The user LaunchAgent is stored in `~/Library/LaunchAgents/`.

The local JSON/YAML configuration contains non-password connection details such as your Bridge username, paths, ports, and tunnel ID. These can still be personal information. The exported certificate, health files, handoff file, and runtime log also belong to your local deployment and should not be committed or uploaded casually.

The Bridge password and runtime API key are saved in your macOS Keychain through hidden terminal prompts. The helper accepts up to 512 characters, passes the value to the Keychain utility through standard input, and verifies the complete saved value. It does not put the literal value into command arguments or generated configuration. Secrets necessarily exist in process memory while in use; local processes with sufficient privileges remain within your trust boundary.

The MCP child removes the inherited tunnel runtime key from its environment before serving mail tools. This does not make the host immune to compromised local software or privileged inspection.

## Diagnostics, support, and removal

Service health and error diagnostics are available locally. Do not assume every log or system diagnostic is free of personal information. Redact keys, passwords, message contents, usernames, tunnel/account identifiers, and sensitive paths before sharing. Public GitHub issues are public; do not upload mailbox samples or raw authentication logs.

`python3 deploy_mini.py stop` stops/disables this project's LaunchAgent but retains configuration and Keychain entries. Disconnect the private ChatGPT connection and revoke the runtime key/tunnel in Platform to remove that remote access path. Remove local settings and the corresponding Keychain items separately if you want to remove the deployment. Bridge and the AI host manage their own retained data independently.

For project questions, use the [repository issue tracker](https://github.com/knowlet/Proton-Mail-Bridge-Remote-Plugin/issues) with redacted information. See [Security](SECURITY.md) before reporting an exposure.

## External policies

- [Proton privacy policy](https://proton.me/legal/privacy)
- [OpenAI privacy policy](https://openai.com/policies/privacy-policy/)
- [OpenAI Secure MCP Tunnel documentation](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

Your organization's agreements and settings may also apply. This project does not promise a specific third-party retention period, data location, or processing exemption.
