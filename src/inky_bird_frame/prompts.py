"""Versioned prompts for profile research, plate generation, and visual QA."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from .birds import BirdSpecies, TaxonContext
from .models import ProfileConflict, ReferencePhoto, SpeciesProfileData

PROMPT_VERSION = "field-journal-v6"


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
    *,
    prior_profile: SpeciesProfileData | None = None,
    profile_conflicts: tuple[ProfileConflict, ...] = (),
) -> str:
    conflict_review = ""
    if prior_profile is not None and profile_conflicts:
        conflicts = json.dumps(profile_conflicts, indent=2, sort_keys=True)
        conflict_review = f"""
This is one bounded re-adjudication of a cached profile after an independent plate review reported
the following source conflicts:
{conflicts}

Prior profile:
{json.dumps(prior_profile, indent=2, sort_keys=True)}

Research each disputed fact from direct source pages. Do not assume either the prior profile or the
review claim is correct, and do not copy a review claim into the profile without source support.
Return a complete profile, preserving prior facts that remain supported and replacing only facts
that the direct sources establish are wrong or incomplete.
"""
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
{conflict_review}

Requirements:
- Preserve the exact taxon ID and names supplied above.
- Measurements must include units and faithfully represent one compatible published measurement
  set. Preserve source qualifiers such as sex, age, or season when they materially explain
  different values. Never synthesize a new range by combining endpoints from incompatible sources.
- Never invent or infer a measurement. Use a source-supported single value or a range whose
  endpoints come from one compatible published measurement set.
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

Non-regression constraints from earlier accepted reviews:
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
  the published endpoints, with the minimum at the bottom and maximum at the top. Include a small
  number of roughly even, unlabeled interior ticks as visual subdivisions; their count and spacing
  do not encode measurement units. For a single value, use one dimension line labeled with that
  value. Use explicit units. Do not draw a zero-based or wider full-range axis, and do not add a
  separate range bracket whose endpoints could disagree with the ruler. Label it
  "BODY LENGTH: {measurements["length"]}" and "SCHEMATIC — NOT TO SCALE". Keep it separate from
  the bird; never imply that it measures the printed illustration.
- It should look like a carefully scanned scientific field-journal page, not Audubon, not a
  decorative poster, not a collage, and not photorealistic.
- Quiet margins. No scenery, map, location, ZIP code, coordinates, date, logo, or watermark.
- Preserve the exact 3:4 portrait aspect ratio and keep all text inside safe page margins.
- Exactly one complete primary bird specimen, with one head, one beak, one anatomically complete
  pair of wings, one anatomically complete pair of legs, and one tail. In a natural side-on or
  overlapping pose, the far-side wing or leg may be partly or fully occluded when the visible body
  geometry clearly supports its natural attachment; do not add a duplicated limb merely to make
  the count visible. Every visible limb, joint, foot, wing, eye, and tail must remain complete and
  plausible. The detached bottom-margin wing and bill/head studies are supplementary anatomical
  details, not additional birds; keep them spatially separate from the primary specimen and make
  each anatomically accurate. Do not depict a second complete bird.
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
    *,
    prior_corrections: tuple[str, ...] = (),
    prior_profile_conflicts: tuple[ProfileConflict, ...] = (),
) -> str:
    prior_review = ""
    if prior_corrections or prior_profile_conflicts:
        corrections = "\n".join(f"- {item}" for item in prior_corrections) or "- None"
        conflicts = (
            json.dumps(prior_profile_conflicts, indent=2, sort_keys=True)
            if prior_profile_conflicts
            else "- None"
        )
        prior_review = f"""
Earlier review history for convergence checking:
Image corrections already requested:
{corrections}
Profile conflicts already reported:
{conflicts}

Check that earlier corrected image defects did not regress. Do not reverse an earlier correction
without naming the concrete visible regression or direct-source conflict that justifies doing so.
Copy an earlier correction into resolved_corrections only when the current image visibly satisfies
that exact request. Do not mark a correction resolved when a new correction reverses, refines, or
otherwise supersedes it. Every resolved_corrections entry must exactly match one earlier correction.
Repeated or contradictory profile conflicts must remain in profile_conflicts, not be converted into
an unsupported image edit. Re-evaluate every earlier conflict against the current profile; never
repeat a stale profile_value from history, and drop a conflict that the current direct sources no
longer support.
"""
    return f"""Review Image 1 as a candidate scientific field-journal plate for
{species.common_name} ({species.scientific_name}). Images 2 onward are licensed field-reference
photos of the same species.

Facts proposed by the research pass:
{json.dumps(profile, indent=2, sort_keys=True)}
{prior_review}

Independently verify the species identity, measurements, and field marks against live source pages
and the attached references. Do not assume the proposed facts are correct. Restrict browsing to
these domains: {", ".join(allowed_domains)}. Do not rely on search snippets. Inspect the candidate
for correct plumage, proportions, bill, eye, wings, tail, legs, feet, and species field marks
against the attached field-reference photos. Compare every visible factual claim to the
independently verified facts. Do not infer a seasonal-plumage correction from one image or an
unstated assumption; require explicit agreement from at least two direct allowed source pages
before requesting a seasonal qualifier or color-pattern rewrite. Inspect every ruler, scale, and
measurement diagram for internal consistency: values must increase from bottom to top, its labeled
endpoints and units must match the published value or range, no separate marker may disagree with
it, and it must be clearly schematic rather than presented as the printed bird's size. Unlabeled
interior ticks are visual subdivisions, not unit increments: their exact count or minor spacing
variation is not a factual error and must not lower a score below 4 by itself. Missing or reversed
endpoints, wrong values or units, a contradictory marker, or a ruler presented as the printed
bird's scale remains a material text error with a specific correction.
Authoritative sources may publish different measurements because they use different samples,
methods, ages, sexes, or seasons. The proposed profile is required to use one compatible published
measurement set, not synthesize a consensus range. Do not report a profile conflict merely because
another allowed source publishes a different supported set. Accept a measurement when its value
and material qualifiers faithfully match a direct allowed source. Report a measurement conflict
only when the proposed value has no direct support, misstates its supporting source, combines
incompatible endpoints, omits a material qualifier, or falsely claims broader agreement than the
source establishes.
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
concrete issues. Put only visible, required image changes in correction_findings; do not repeat
positive observations, source confirmations, or already-correct traits there. Put disagreements
between the proposed profile and independently verified direct-source facts in profile_conflicts
instead—state the supported profile field, proposed value, independently observed value, and at
least two direct HTTPS sources from distinct configured domains. Do not instruct the image
generator to apply the reviewer claim directly. profile_value must quote the current proposed
field, not an earlier review history entry. For field_marks, encode both profile_value and
observed_value as compact JSON arrays. Identity fields are not adjudicable profile conflicts.
Return at least two direct HTTPS source URLs from distinct configured domains used for overall
verification.

Set passed=true only when all four scores are at least 4, location_free is true, the bird has
exactly one complete primary specimen with one head, one beak, an anatomically complete pair of
wings, an anatomically complete pair of legs, and one tail,
and there are no material species or text errors. Clearly detached wing and bill/head studies are
intentional supplementary anatomy, not duplicate parts or additional birds; do not fail them for
their presence, but validate each study independently and fail malformed anatomy or a study that
appears attached to the primary specimen. A naturally occluded far-side wing or leg is acceptable
when pose and visible attachment geometry are anatomically convincing; never excuse a malformed,
detached, duplicated, or implausibly attached visible structure, a missing visible eye, or broken
feet as occlusion. When passed=false, at least one of correction_findings or profile_conflicts must
be nonempty. When passed=true, correction_findings and profile_conflicts must both be empty.
resolved_corrections may contain only exact earlier requests that this image now satisfies. Return
only the requested JSON.

Reference provenance:
{reference_list(references)}
"""
