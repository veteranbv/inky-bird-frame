"""Versioned prompts for profile research, plate generation, and visual QA."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from .birds import BirdSpecies, TaxonContext
from .models import ReferencePhoto, SpeciesProfileData

PROMPT_VERSION = "field-journal-v2"


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


def reference_list(references: list[ReferencePhoto]) -> str:
    lines = []
    for index, reference in enumerate(references, start=1):
        lines.append(
            f"Image {index}: {reference.attribution}; {reference.license_code}; "
            f"{reference.source_url}"
        )
    return "\n".join(lines)


def profile_prompt(
    species: BirdSpecies,
    context: TaxonContext,
    references: list[ReferencePhoto],
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

Use web search to verify measurements, field marks, habitat, and behavior against at least two
authoritative ornithology, museum, government, or university sources. Use the attached images to
verify plumage colors, proportions, bill, eye, legs, wings, and tail. Return only the requested
JSON.

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
) -> str:
    measurements = profile["measurements"]
    field_marks = "\n".join(f"  - {mark}" for mark in profile["field_marks"])
    palette = ", ".join(profile["palette"])
    correction = ""
    if correction_findings:
        issues = "\n".join(f"- {finding}" for finding in correction_findings)
        correction = f"""
Correction required after an independent review of the previous attempt:
{issues}

Create a new image that corrects every issue above. Do not copy or lightly edit the previous
attempt.
"""
    return f"""$imagegen

Use case: scientific-educational
Asset type: reusable portrait plate for a 13.3-inch color e-paper frame
Primary request: Create one polished scientific field-journal plate for the species below.

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
{reference_list(references)}

Treat every attached image as a species-accuracy reference. Synthesize the consistent anatomy,
proportions, posture, plumage pattern, and colors across them. Do not reproduce any photograph's
background, pose, crop, or composition.
{correction}

Style and composition:
- Portrait 3:4 page on warm aged cream naturalist-notebook paper.
- Fine graphite and confident ink linework with restrained transparent watercolor.
- One full-body bird, large and centered-right, in a natural perched posture.
- Left margin contains compact handwritten measurements and field marks.
- Bottom margin contains a small wing-pattern study, a bill/head study, and color swatches.
- Right edge contains a thin measurement ruler.
- It should look like a carefully scanned scientific field-journal page, not Audubon, not a
  decorative poster, not a collage, and not photorealistic.
- Quiet margins. No scenery, map, location, ZIP code, coordinates, date, logo, or watermark.
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
) -> str:
    return f"""Review Image 1 as a candidate scientific field-journal plate for
{species.common_name} ({species.scientific_name}). Images 2 onward are licensed field-reference
photos of the same species.

Facts proposed by the research pass:
{json.dumps(profile, indent=2, sort_keys=True)}

Independently verify the species identity, measurements, and field marks with web search against
at least two authoritative ornithology, museum, government, or university sources. Do not assume
the proposed facts are correct. Inspect the candidate for correct plumage, proportions, bill, eye,
wings, tail, legs, feet, and species field marks against the attached field-reference photos.
Compare every visible factual claim to the independently verified facts. Confirm that no place
name, ZIP code, coordinates, map, or local-observation detail appears. Record every concrete issue
and return the direct HTTPS pages used for verification.

Set passed=true only when all four scores are at least 4, location_free is true, the bird has
exactly one head, one beak, two wings, two legs, and one tail, and there are no material species or
text errors. Return only the requested JSON.

Reference provenance:
{reference_list(references)}
"""
