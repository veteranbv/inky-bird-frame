"""Versioned prompts for profile research, plate generation, and visual QA."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from .birds import BirdSpecies, TaxonContext
from .models import ReferencePhoto, SpeciesProfileData

PROMPT_VERSION = "field-journal-v3"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return "".join(parser.parts).strip()


def reference_list(references: list[ReferencePhoto], *, start_index: int = 1) -> str:
    lines = []
    for index, reference in enumerate(references, start=start_index):
        lines.append(
            f"Image {index}: {reference.attribution}; {reference.license_code}; "
            f"{reference.source_url}"
        )
    return "\n".join(lines)


def profile_prompt(
    species: BirdSpecies,
    context: TaxonContext,
    references: list[ReferencePhoto],
    allowed_domains: tuple[str, ...],
) -> str:
    return f"""Create a factual, location-neutral species profile for a scientific
field-journal plate.

Identity supplied by iNaturalist:
- Taxon ID: {species.taxon_id}
- Common name: {species.common_name}
- Scientific name: {species.scientific_name}
- Family: {context.family}
- Summary: {strip_html(context.summary)}
- Source: {context.source_url}

Attached reference images:
{reference_list(references)}

The structured iNaturalist context and BirdNET fallback have already been attempted. Research only
facts still needed for the schema. Restrict browsing to these domains:
{", ".join(allowed_domains)}
Use at least two independent sources from that list and do not rely on search snippets. Use the
attached images to verify plumage colors, proportions, bill, eye, legs, wings, and tail. Return
only the requested JSON.

Requirements:
- Preserve the exact taxon ID and names supplied above.
- Measurements must include units and a compact range suitable for a field note.
- Provide 4 to 6 concise, visible field marks.
- Provide 3 to 5 plain-language palette colors tied to the species.
- Source URLs must be direct HTTPS pages used for the facts.
- Do not mention ZIP codes, cities, coordinates, observations, or any local discovery context.
"""


def plate_prompt(
    species: BirdSpecies,
    profile: SpeciesProfileData,
    references: list[ReferencePhoto],
    output_path: Path,
    correction_findings: tuple[str, ...] = (),
    *,
    has_correction_source: bool = False,
) -> str:
    measurements = profile["measurements"]
    field_marks = "\n".join(f"  - {mark}" for mark in profile["field_marks"])
    palette = ", ".join(profile["palette"])
    correction = ""
    if correction_findings:
        issues = "\n".join(f"- {finding}" for finding in correction_findings)
        if has_correction_source:
            correction = f"""
Image 1 is the previous plate and the edit target. Images 2 onward are species-accuracy
references. Correct only the concrete defects below while preserving the previous plate's
successful content, visual identity, typography, and large specimen footprint. Keep every
unflagged factual statement and design element unchanged. Do not shrink the bird or create new
empty space to make room for corrections.

Corrections required after an independent review:
{issues}

Use the supplied current profile as the authority for visible facts, including any text that the
review requires changing. Preserve correct anatomy and composition from Image 1 while making the
smallest complete edit that resolves every correction. Legacy review records may include
statements confirming correct traits; preserve those traits as invariants rather than treating
them as changes.
"""
        else:
            correction = f"""
Corrections required after an independent review of an earlier generation cycle:
{issues}

Create a new image that resolves every correction while preserving the supplied correct facts and
the required large-specimen composition. Legacy review records may include statements confirming
correct traits; preserve those traits as invariants rather than treating them as changes.
"""
    reference_start = 2 if has_correction_source else 1
    reference_roles = (
        "Image 1 is the correction edit target. The remaining attached images are licensed "
        "species-accuracy references."
        if has_correction_source
        else "Every attached image is a licensed species-accuracy reference."
    )
    return f"""$imagegen

Use case: {"precise-object-edit" if has_correction_source else "scientific-educational"}
Asset type: reusable portrait plate for a 13.3-inch color e-paper frame
Primary request: {"Correct" if has_correction_source else "Create"} one polished scientific
field-journal plate for the species below.

Species identity:
- Common name, verbatim: "{species.common_name}"
- Scientific name, verbatim: "{species.scientific_name}"
- Family: "{profile["family"]}"

Species-specific field notes:
- Length: "{measurements["length"]}"
- Wingspan: "{measurements["wingspan"]}"
- Weight: "{measurements["weight"]}"
- Habitat: "{profile["habitat"]}"
- Behavior: "{profile["behavior"]}"
- Field marks:
{field_marks}
- Plumage palette: {palette}

Reference images, in attachment order:
{reference_list(references, start_index=reference_start)}

{reference_roles} Synthesize the consistent anatomy, proportions, posture, plumage pattern, and
colors across the reference photographs. Do not reproduce any photograph's background, pose,
crop, or composition.
{correction}

Style and composition:
- Portrait 3:4 page on warm aged cream naturalist-notebook paper.
- Fine graphite and confident ink linework with restrained transparent watercolor.
- One full-body bird, large and centered-right, in a natural perched posture.
- The bird remains the dominant page element and confidently uses the available space.
- Left margin contains compact handwritten measurements and field marks.
- Bottom margin contains a small wing-pattern study, a bill/head study, and color swatches.
- Right edge contains a thin, self-contained vertical schematic ruler keyed to the body-length
  range "{measurements["length"]}". Use proportionally spaced ticks with explicit units, visibly
  mark the published range, and label it "BODY LENGTH: {measurements["length"]}" and
  "SCHEMATIC — NOT TO SCALE". Keep it separate from the bird; never imply that it measures the
  printed illustration.
- It should look like a carefully scanned scientific field-journal page, not Audubon, not a
  decorative poster, not a collage, and not photorealistic.
- Quiet margins. No scenery, map, location, ZIP code, coordinates, date, logo, or watermark.
- Preserve the exact 3:4 portrait aspect ratio and keep all text inside safe page margins.
- Exactly one bird, one head, one beak, two wings, two legs, and one tail. Feet must be plausible.
- Render only the exact species name and supplied factual notes. Do not invent extra prose.

Generate exactly one image using the built-in image generation tool. After generation, copy the
selected final bitmap to this exact path inside the current workspace:
{output_path}

Verify that the file exists before finishing. Do not merely describe the image.
"""


def review_prompt(
    species: BirdSpecies,
    profile: SpeciesProfileData,
    references: list[ReferencePhoto],
    allowed_domains: tuple[str, ...],
) -> str:
    return f"""Review Image 1 as a candidate scientific field-journal plate for
{species.common_name} ({species.scientific_name}). Images 2 onward are licensed field-reference
photos of the same species.

Facts proposed by the research pass:
{json.dumps(profile, indent=2, sort_keys=True)}

Independently verify the species identity, measurements, and field marks against live source pages
and the attached references. Do not assume the proposed facts are correct. Restrict browsing to
these domains: {", ".join(allowed_domains)}. Do not rely on search snippets. Inspect the candidate
for correct plumage, proportions, bill, eye, wings, tail, legs, feet, and species field marks
against the attached field-reference photos. Compare every visible factual claim to the
independently verified facts. Confirm that no place name, ZIP code, coordinates, map, or
local-observation detail appears. Use findings for the complete review record, including verified
strengths and concrete issues. Put only required, actionable changes in correction_findings; do not
repeat positive observations, source confirmations, or already-correct traits there. Return at
least two direct HTTPS source URLs from distinct configured domains used for verification.

Set passed=true only when all four scores are at least 4, location_free is true, the bird has
exactly one head, one beak, two wings, two legs, and one tail, and there are no material species or
text errors. When passed=false, correction_findings must contain at least one specific change.
When passed=true, correction_findings must be empty. Return only the requested JSON.

Reference provenance:
{reference_list(references)}
"""
