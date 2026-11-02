# Labeling codebook

Read this before labeling. The labels are the ground truth the eval and the gate measure against, so two people
following this codebook should reach the same label. When a case is truly ambiguous, label it anyway, write why in
`note`, and keep it: ambiguous cases show where the definitions need work.

## The file

`evals/labels.jsonl` has one JSON object per line:

```json
{"number": 12345, "content_sha": "…", "split": "frozen", "source": "sample", "kind": "bug", "area": "mcp", "blocked": false, "severity": "medium", "note": "", "url": "https://github.com/anthropics/claude-code/issues/12345"}
```

- Fill in `kind`, `area`, `blocked` and `severity`. Leave the other fields alone.
- `content_sha` pins the version you labeled. If the issue is edited later, the eval skips it rather than score
  against a label that may no longer fit.
- `split` is assigned by issue number. `frozen` cases are the gate's held-out set: don't tune prompts while looking
  at them. `tune` cases are for iterating. After the frozen labels are final, run `loop labels-check --update` and
  commit the hash with them. CI then fails any later change to the frozen set that wasn't made on purpose.
- Label from the issue as filed (title and body). Ignore comments, and don't look up how it was resolved.
- Label before you look at what the model said.

## kind

| Value | Use when |
|---|---|
| `bug` | The software does not do what it should: errors, crashes, wrong output, broken UI |
| `feature_request` | A capability that does not exist yet, including "make X configurable" |
| `model_behavior` | The model made poor choices: ignored instructions, made unwanted edits, refused, or its quality changed between versions. The software worked; the model's decisions were the problem |
| `docs` | Documentation is missing, wrong or unclear |
| `question` | A usage or support question with no defect or request behind it |
| `other` | Spam, duplicates written as one-liners, meta discussion |

Tie-breakers:
- "It used to do X and now it doesn't" is a `bug` when the software changed and `model_behavior` when the model's
  choices changed.
- A bug report that ends with "please add an option to…" is still a `bug` if the defect is the main point.
- The form the reporter picked is a hint, not the answer. People file feature requests on the bug form.

## area

Pick the one area in `config/taxonomy.yaml` whose owners would make the fix, not where the symptom appeared. A
crash in the TUI caused by an MCP server's output belongs to `mcp`. Use `other` only when nothing fits, and say
in `note` what area you would have wanted.

## blocked

`true` when the user cannot do their core work and reports no workaround: the tool won't start, they can't
authenticate, they lose data, or a platform they must use isn't supported.

`false` for annoyances, cosmetic problems, slowness that still lets them work, problems with a stated workaround,
and feature requests (unless the missing feature makes the product unusable for them).

When you can't tell, choose `true` and say so in `note`. The pipeline treats a missed blocked user as the costliest
error, so the labels lean the same way.

## severity

| Value | Use when |
|---|---|
| `critical` | Data loss, security exposure, or blocked with no workaround |
| `high` | A major feature is broken, or the user loses significant productivity |
| `medium` | It works, with friction or a workaround |
| `low` | Cosmetic, or nice to have |

Feature requests are usually `low` or `medium`. A `critical` label should almost always come with `blocked: true`
or a data-loss or security note.

## Coverage

Make sure the frozen set includes at least a few of each: blocked users, model-behavior reports, third-party
platform issues (Bedrock, Vertex, Foundry), and free-text issues that didn't use a form. `loop sample` stratifies
by form, but check the mix before you freeze.
