# Architecture

## Roles

### Controller

The controller owns discovery and generation. One locked cycle:

1. resolves the private discovery location;
2. queries iNaturalist species counts for the configured radius and window;
3. selects taxa without a terminal local state;
4. acquires and verifies licensed references;
5. creates a sourced, structured species profile through Codex;
6. generates a plate through the built-in `$imagegen` skill;
7. prepares portrait and display assets;
8. runs a second structured Codex visual review; and
9. places passing output in the pending queue.

The controller also exposes a read-only HTTP catalog:

- `GET /health`
- `GET /v1/catalog`
- `GET /v1/assets/<catalog-relative-path>`

### Display node

The display node does not discover birds or generate art. Each timer cycle:

1. fetches the approved catalog;
2. selects the next entry from durable local state;
3. downloads the display asset;
4. verifies its SHA-256 checksum;
5. writes it to a local cache atomically; and
6. updates the Inky panel before advancing state.

This pull model avoids embedding the display node's address in controller
state. Moving the Pi from temporary Ethernet to Wi-Fi does not change catalog
or generation configuration.

## State model

| State | Meaning | Automatic generation allowed |
| --- | --- | --- |
| approved | Human accepted and published | No |
| pending | Automated review passed; awaiting human | No |
| rejected | Human rejected | No |
| failed | Generation or automated review failed | No |
| eligible | No terminal state exists | Yes |

`retry TAXON_ID` archives rejected or failed state and makes that taxon
eligible. There is deliberately no implicit replacement path for approved art.

## Privacy and licensing

The private ZIP code and observation window influence only the queue order.
They are not passed to image generation and are not stored in public catalog
manifests.

Reference acquisition accepts only iNaturalist research-grade photos marked
CC0 or CC BY, uses distinct observers, records attribution and source URLs, and
requires an 800-pixel minimum edge. Reference bitmaps stay in ignored controller
state and are not redistributed in the catalog.

## Deterministic and generative work

Deterministic code handles discovery parameters, terminal-state selection,
license filtering, reference checksums, prompt assembly, image dimensions,
rotation, catalog checksums, approval, serving, downloading, and display
rotation.

Codex handles factual synthesis, image generation, and visual review. Those
steps are intentionally bounded by structured schemas, attached references,
versioned prompts, terminal failure state, and mandatory human approval.
