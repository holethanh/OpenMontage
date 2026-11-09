# Affiliate Marketing Videos

Use when a project contains `artifacts/affiliate_brief.json` (created by the Backlot
**Affiliate** page, `/affiliate`). The brief is the source of truth: product facts, hook, CTA,
caption, platform, aspect ratio and the pipeline/style to run.

## Rules

1. Run the pipeline named in the brief (`pipeline`, `style`) through the normal stage-by-stage
   process. This skill adds constraints; it does not replace the pipeline or its director skills.
2. Open with the brief's `hook.text` verbatim. Close with `cta`.
3. **Only claim what is in `product.description`.** No invented statistics, reviews, prices,
   discounts or scarcity. If a fact is missing, ask the user.
4. Show the disclosure (`copy.disclosure`) on screen for at least 2 seconds, and keep it in the
   caption. Amazon products also need the Amazon Associate statement.
5. No guaranteed results, medical/cure claims, income promises or "risk-free" language. Re-run
   the compliance check (`POST /api/affiliate/compliance`) on the final script and caption.
6. Include one honest limitation or "who this is not for" in review/comparison formats.
7. If `product.image_path` is set it is the user's own product photo (copied into the project): use it as the product reference and do not invent a different-looking product.
8. `needs_footage: true` means the user must supply real product footage. Do not substitute
   generated footage of the product; run `source_media_review` first.
9. Respect `aspect_ratio` and `duration_seconds`. Vertical formats need captions inside the safe
   zone (keep text out of the bottom ~20% and right edge).
10. Do not put the affiliate URL inside the video. Platforms that do not support clickable
   caption links (`copy.link_note`) should point viewers to the bio or shop tab.

## After publishing

Tell the user to log views, clicks, orders and commission on the Backlot Affiliate page
(Results tab) so hook angles and templates can be compared by profit and ROI.
