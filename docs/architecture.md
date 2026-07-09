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
8. runs an independent, sourced Codex factual and visual review;
9. regenerates failed reviews with corrective findings, within a configured
   attempt limit; and
10. atomically publishes passing output through the pending queue.

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

This pull model keeps display addressing out of controller state and limits the
node to a read-only catalog relationship.

## State model

| State | Meaning | Automatic generation allowed |
| --- | --- | --- |
| approved | Independent AI review passed; published immutably | No |
| pending | Passing candidate awaiting atomic publication or crash recovery | No |
| rejected | Operator override rejected a candidate | No |
| failed | Generation exhausted its bounded attempts | No |
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
rotation, catalog checksums, publication, serving, downloading, and display
rotation.

Codex handles factual synthesis, image generation, and independent factual and
visual review. Those steps are bounded by structured schemas, attached
references, sourced verification, versioned prompts, configurable attempts,
and a terminal failure state. Human approval is not required for normal flow.
