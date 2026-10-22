# Memory Worlds for Claude Code

This plugin adds the Memory Worlds MCP server and a skill for scoped recall,
selected capture and explicit publication. It does not capture conversations
automatically. Requires an installed `memory-worlds[mcp]` environment and a
running service.

From a reviewed checkout, load the plugin with:

```sh
claude --plugin-dir ./plugins/claude
```

Configure the absolute Memory Worlds Python interpreter, API URL and private
agent credential-file path when prompted. The Python environment must include
the MCP extra. Credentials remain in their existing files.

Alternatively, export a plain MCP configuration using the
[client guide](https://github.com/josephjohncox/memory-worlds/blob/main/docs/clients.md).
Use either connection path for a session to avoid duplicate tools.

Validate the plugin locally with `claude plugin validate ./plugins/claude`.
See the [official plugin reference](https://code.claude.com/docs/en/plugins-reference)
for persistent installation and configuration. Licensed under
[Apache-2.0](LICENSE).
