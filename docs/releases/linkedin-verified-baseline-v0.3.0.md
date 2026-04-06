# LinkedIn Verified Baseline v0.3.0

Date locked: 2026-03-28

This document freezes the current LinkedIn workflow before any new MCP-style browser layer work.

## What counts as success

A LinkedIn application is only counted as submitted when all of the following are true:

1. The Easy Apply flow reaches the real review page.
2. The final button clicked is `Submit application`.
3. LinkedIn shows a post-submit success state such as `Application sent`.
4. After the dialog closes, the job page itself shows an applied marker such as `Application submitted`, `Applied`, or `Resume downloaded`.

Anything less stays `pending` or `review_required`.

## Verified LinkedIn submissions in this baseline

1. `GENISYSAPP | Artificial Intelligence Engineer`
   Proof: [genisys-after-submit-click.png](genisys-after-submit-click.png)
2. `Amodal AI | Developer Content Engineer`
   Proof: [amodal-after-submit-click.png](amodal-after-submit-click.png)
   Review screen with the selected resume: [amodal-after-manual-review.png](amodal-after-manual-review.png)
3. `Turing | Remote Software Developer`
   Final page proof: [turing-after-submit-page.png](turing-after-submit-page.png)
   Final review proof before submit: [turing-modal-review.png](turing-modal-review.png)

## Workflow shape that worked

1. Start from the visible LinkedIn jobs results page with filters already applied.
2. Open one job at a time and read the description before applying.
3. Only continue if the description passes the saved criteria.
4. Drive the real Easy Apply flow step by step:
   `Contact info -> Resume -> Optional LinkedIn extras -> Additional questions -> Review -> Submit application`
5. Save a proof screenshot only after LinkedIn confirms the post-submit state.

## Known invalid prior behavior

Older LinkedIn runs in this project sometimes marked jobs as submitted too early, while the flow was still sitting on `Next` or `Review`. That behavior is not valid for this baseline.

The code now treats those old LinkedIn entries as unverified and invalidates them on load unless a real verified submit state exists.

## Resume and answer assumptions used in this baseline

- Active resume: `Deep resume.pdf`
- Consent/privacy confirmation answers: `Yes`
- Amodal custom answers used in this run:
  - technical work examples: `No`
  - hands-on experience with developer tools or AI APIs: `No`
  - comfortable on camera / screen walkthrough with voiceover: `Yes`

## Next step after this baseline

Any future Chrome MCP-style improvement should preserve this verification rule and should not regress into counting incomplete LinkedIn flows as submitted.
