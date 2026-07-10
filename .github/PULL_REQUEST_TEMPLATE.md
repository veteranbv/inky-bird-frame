## Summary

Describe the problem and the smallest complete change that solves it.

## Change type

- [ ] Code
- [ ] Documentation
- [ ] Catalog plate
- [ ] Tests or continuous integration
- [ ] Other maintenance

## Validation

List the commands you ran and their results. The standard checks are:

```text
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run inky-bird-frame catalog validate --catalog catalog
```

## Catalog contribution

Delete this section when the pull request does not add a bird plate.

- Common name:
- Scientific name:
- eBird taxon ID:
- [ ] The plate was produced and approved through the project pipeline.
- [ ] I ran `inky-bird-frame catalog prepare` rather than hand-editing catalog JSON.
- [ ] The manifest records factual sources, reference provenance, generation details, and independent review.
- [ ] I have the right to submit the generated images and included metadata under this repository's license.
- [ ] The contribution contains no location, observation history, downloaded reference image, local path, or credential.
- [ ] The pull request adds a new taxon and does not modify an approved catalog entry.

## Review notes

Call out security implications, compatibility concerns, decisions that need maintainer attention,
or checks that could not be run. Write `None` when there are no additional notes.
