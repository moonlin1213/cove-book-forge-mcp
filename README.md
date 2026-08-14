# cove-book-forge-mcp

A local-first, headless MCP server for turning PDF/EPUB books and external
reading-system snapshots into Obsidian knowledge and reusable Agent Skills.

This repository contains the independent open-source core. It does not ship an
official reading UI and does not depend on private Cove/栖渡 code. A later MCP
phase will let existing reading systems submit stable snapshots, while an
optional managed library will serve users who do not already have one.

> Status: early development. The current foundation establishes public
> contracts, safe configuration, and diagnostics before parser, provider,
> output, job, and MCP phases are added.

## Privacy defaults

- local library and generated files stay local;
- telemetry, cloud sync, and remote logging are disabled;
- API-key values come from environment variables and are never stored in YAML;
- all output roots require explicit configuration.

## Acknowledgements

`cove-book-forge-mcp` is inspired by and builds upon ideas and tooling from
[book-to-skill](https://github.com/virgiliojr94/book-to-skill), created by
Virgilio Jr. We are grateful for its document extraction work, Agent Skill
structure, and open-source contribution.

## License

MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
