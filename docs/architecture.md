# Architecture

## Roles

### Controller

The controller owns discovery and generation through independent schedules.
An observation refresh:

1. resolves the private discovery location;
2. queries iNaturalist species counts for the configured radius and window;
3. atomically stores the private observation snapshot; and
4. publishes a private active catalog containing only observed taxa that
   already have approved plates.

A locked generation cycle reads the latest non-stale snapshot, then:

1. selects taxa without a terminal local state;
2. acquires and verifies licensed references;
3. creates a sourced, structured species profile through Codex;
4. generates a plate through the built-in `$imagegen` skill;
5. prepares portrait and display assets;
6. runs an independent, sourced Codex factual and visual review;
7. regenerates failed reviews with corrective findings, within a configured
   attempt limit; and
8. atomically publishes passing output through the pending queue.

The controller also exposes a read-only HTTP catalog:

- `GET /health`
- `GET /v1/catalog`
- `GET /v1/assets/<catalog-relative-path>`

### Display node

The display node does not discover birds or generate art. Each timer cycle:

1. fetches the private active catalog;
2. selects an entry using the configured sequential, shuffle, or
   observation-weighted policy and durable local state;
3. downloads the display asset;
4. verifies its SHA-256 checksum;
5. writes it to a local cache atomically; and
6. updates the Inky panel before advancing state.

This pull model keeps display addressing out of controller state and limits the
node to a read-only catalog relationship. If refresh, generation, or controller
access fails, the current e-paper image remains visible.

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

The private ZIP code and observation window influence the generation queue and
active rotation. They are not passed to image generation and are not stored in
public catalog manifests. Observation snapshots and counts stay in ignored
controller state.

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
