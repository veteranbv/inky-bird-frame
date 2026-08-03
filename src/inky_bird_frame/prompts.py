"""Versioned prompts for profile research, plate generation, and visual QA."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from .birds import BirdSpecies, TaxonContext
from .models import ReferencePhoto, SpeciesProfileData

PROMPT_VERSION = "field-journal-v4"


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
    invariant_findings: tuple[str, ...] = (),
    has_correction_source: bool = False,
) -> str:
    measurements = profile["measurements"]
    field_marks = "\n".join(f"  - {mark}" for mark in profile["field_marks"])
    palette = ", ".join(profile["palette"])
    correction = ""
    if correction_findings or invariant_findings:
        issues = (
            "\n".join(f"- {finding}" for finding in correction_findings)
            or "- No new defect beyond the non-regression constraints below."
        )
        invariants = (
            "\n".join(f"- {finding}" for finding in invariant_findings)
            or "- No separate non-regression constraints."
        )
        if has_correction_source:
            correction = f"""
Image 1 is the previous plate and the edit target. Images 2 onward are species-accuracy
references. Correct only the concrete defects below while preserving the previous plate's
successful content, visual identity, typography, and large specimen footprint. Keep every
unflagged factual statement and design element unchanged. Do not shrink the bird or create new
empty space to make room for corrections.

Current actionable corrections required after an independent review:
{issues}

Non-regression constraints from earlier human review:
{invariants}

Use the supplied current profile as the authority for visible facts, including any text that the
review requires changing. Preserve correct anatomy and composition from Image 1 while making the
smallest complete edit that resolves every current correction. A non-regression constraint remains
mandatory, but it is not a request to redraw a feature that Image 1 already satisfies. Inspect each
one: preserve an already-correct feature exactly, and change it only if Image 1 still violates the
constraint.
"""
        else:
            correction = f"""
Current actionable corrections required after an independent review of an earlier generation
cycle:
{issues}

Mandatory constraints from earlier human review:
{invariants}

Create a new image that resolves every correction while preserving the supplied correct facts and
the required large-specimen composition. Implement every mandatory constraint in a source-free
generation; do not treat it as optional merely because it originated in an earlier review.
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
crop, or composition. When a field mark depends on unequal paired structures such as mandibles,
identify each structure before editing it. For an open skimmer bill, the lower mandible is the jaw
below the gape line: when the references show it projecting, its tip must be farther from the
shared bill base than the upper tip. Match both the direction and degree of that difference to the
clearest side-profile references. Preserve the near-full length of the shorter structure; do not
substitute a wider gape, lengthen the wrong jaw, truncate the upper mandible, or exaggerate a
qualitative field mark. In a side-profile skimmer with an open bill, keep the difference visually
legible: the lower tip must also reach farther forward in the bird's facing direction when the
references show that projection; do not rely on a steeper gape angle or lower tip position. Use the
same plausible proportional relationship in the primary specimen and every supplementary study.
{correction}

Style and composition:
- Portrait 3:4 page on warm aged cream naturalist-notebook paper.
- Fine graphite and confident ink linework with restrained transparent watercolor.
- One full-body bird, large and centered-right, in a natural perched posture.
- The bird remains the dominant page element and confidently uses the available space.
- Left margin contains compact handwritten measurements and field marks.
- Bottom margin contains a small wing-pattern study, a bill/head study, and color swatches.
- Right edge contains a thin, self-contained vertical schematic range ruler representing exactly
  the published body length "{measurements["length"]}". For a range, the ruler itself spans only
  the published endpoints, with the minimum at the bottom and maximum at the top, plus exactly
  four evenly spaced unlabeled interior ticks creating five equal proportional segments. Those
  ticks are subdivisions, not one-unit increments. For a single value, use one dimension line
  labeled with that value. Use explicit units. Do not draw a zero-based or wider full-range axis,
  and do not add a separate range bracket whose endpoints could disagree with the ruler. Label it
  "BODY LENGTH: {measurements["length"]}" and "SCHEMATIC — NOT TO SCALE". Keep it separate from
  the bird; never imply that it measures the printed illustration.
- It should look like a carefully scanned scientific field-journal page, not Audubon, not a
  decorative poster, not a collage, and not photorealistic.
- Quiet margins. No scenery, map, location, ZIP code, coordinates, date, logo, or watermark.
- Preserve the exact 3:4 portrait aspect ratio and keep all text inside safe page margins.
- Exactly one complete primary bird specimen, with one head, one beak, two wings, two legs, and
  one tail. The detached bottom-margin wing and bill/head studies are supplementary anatomical
  details, not additional birds; keep them spatially separate from the primary specimen and make
  each anatomically accurate. Do not depict a second complete bird. Feet must be plausible.
- Render the primary bird's tail as a distinct anatomical structure matching the supplied field
  mark; do not let folded wing tips obscure or impersonate it.
- When a supplied field mark calls an eye dark and inconspicuous while specifying its pupil shape,
  keep the iris dark enough to blend into the surrounding plumage while making the pupil readable,
  and render the supplied pupil shape precisely in every depicted head. When the supplied shape is
  vertical, use a thin black vertical slit rather than a round or oval pupil. Never invent a
  vertical slit for a species whose supplied facts specify another shape, and do not substitute a
  conspicuous amber, gold, or yellow iris ring.
- Render only the exact species name and supplied factual notes. Do not invent extra prose.
- Before finishing, compare every visible text line character-for-character with the supplied
  field notes and confirm that the requested edit did not alter unrelated anatomy or design. Undo
  any incidental spelling, number, punctuation, anatomy, or composition drift.

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
independently verified facts. Do not infer a seasonal-plumage correction from one image or an
unstated assumption; require explicit agreement from at least two direct allowed source pages
before requesting a seasonal qualifier or color-pattern rewrite. Inspect every ruler, scale, and
measurement diagram for internal
consistency: values must increase from bottom to top, its endpoints, ticks, and units must match the
published value or range, no separate marker may disagree with it, and it must be clearly
schematic rather than presented as the printed bird's size. A range ruler must contain exactly
four evenly spaced unlabeled interior ticks; treat them as five proportional segments, not
one-unit increments. Treat any mismatch as a material text error with a specific correction.
Read every visible text line in full rather than summarizing it. Compare its spelling, numbers,
and word order to the proposed facts; treat duplicated, omitted, substituted, or nonsensical words
as material text errors and give the exact replacement. When unequal mandibles are a field mark,
trace each one independently from its shared bill base to its tip in every depicted head; do not
confuse the open-gape angle with base-to-tip length. Compare both the direction and degree of the
projection with the clearest attached side-profile references. Fail an ambiguous, co-terminal, or
reversed rendering, an exaggerated or materially understated projection, a truncated-looking
upper mandible, or inconsistent proportions between heads—even when the lower mandible is
technically longer. Confirm that no place name, ZIP code, coordinates, map, or local-observation
detail appears. Use findings for the complete review record, including verified strengths and
concrete issues. Put only required, actionable changes in correction_findings; do not repeat
positive observations, source confirmations, or already-correct traits there. Return at least two
direct HTTPS source URLs from distinct configured domains used for verification.

Set passed=true only when all four scores are at least 4, location_free is true, the bird has
exactly one complete primary specimen with one head, one beak, two wings, two legs, and one tail,
and there are no material species or text errors. Clearly detached wing and bill/head studies are
intentional supplementary anatomy, not duplicate parts or additional birds; do not fail them for
their presence, but validate each study independently and fail malformed anatomy or a study that
appears attached to the primary specimen. When passed=false, correction_findings must contain at
least one specific change.
When passed=true, correction_findings must be empty. Return only the requested JSON.

Reference provenance:
{reference_list(references)}
"""
