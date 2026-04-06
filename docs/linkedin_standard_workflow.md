# LinkedIn Standard Workflow

This is the only LinkedIn flow that counts as a real submission.

Baseline release: [v0.3.0](CHANGELOG.md)

Verified examples from the current baseline:

- [genisys-after-submit-click.png](runs/genisys-after-submit-click.png)
- [amodal-after-submit-click.png](runs/amodal-after-submit-click.png)
- [turing-after-submit-page.png](runs/turing-after-submit-page.png)

## Search setup

1. Open the LinkedIn Jobs results page.
2. Apply the saved search query.
3. Open `All filters`.
4. Set `Date posted` to `Past week`.
5. Set `Experience level` to `Entry level` and `Associate`.
6. Set `Employment type` to `Contract`.
7. Turn `Easy Apply` on.
8. Leave `Remote` off unless the user explicitly asks for it.
9. Show results and stay on the filtered results page.

## Review loop

1. Select a visible job from the filtered results list.
2. Open the job detail pane.
3. Scroll the description and expand `See more` when present.
4. Check the description against the user criteria:
   - AI-related or strong software role with clear AI/LLM/ML work in the description
   - contract or C2C fit
   - less than 5 years, preferably 3 to 4
   - no USC, GC, clearance, public trust, W2-only, or no-C2C restrictions
   - no sponsorship conflict for an H1B contractor
5. Skip immediately if any hard restriction fails.

## Apply loop

1. Click `Easy Apply`.
2. Fill visible required fields from the profile and saved answers.
3. Continue only through these real LinkedIn steps:
   - `Next`
   - `Review`
   - `Submit application`
4. If the flow stops on missing required answers, mark it `review_required`.

## Verification rule

A LinkedIn job is only `submitted` when both of these are true:

1. LinkedIn shows a success state such as `Application sent`.
2. After the dialog closes, the job page itself no longer shows `Easy Apply` and shows an applied marker such as `Applied`, `Application submitted`, or `Resume downloaded`.

Anything less is not a submission.
