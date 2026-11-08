# QA-04B public synthetic corpus

Twelve original, public synthetic examples. Each of SEC-1, PERF-1 and STYLE-1 has
exactly two violations and two clean controls. The letters in fixture IDs have no
meaning in model input: IDs, metadata, expected labels and scripted responses are
local evaluation data and must never be provider metadata or prompt content.

`rules.md` uses the selected S3 root Markdown profile (`rules-md/1`). STYLE-1 is
scored as `maintainability`, the production output category for style conventions.
`fixtures/*/expected_findings.json` contains hand-labelled locations and rule
references; clean examples contain an empty list. Every violation is the changed
return expression on NEW line 2 of `src/handler.py`. Clean controls deliberately
satisfy the corresponding policy. The tiny `client` object is an injected data or
HTTP client; `user` is the authenticated principal and `request` is untrusted.

`fixtures/*/golden_response.json` is a scripted transport answer for deterministic
integration only. It proves pipeline behavior and never measures model quality.
The scripted transport repeats the same prepared response in both passes,
including references that production correctly filters when rules are absent.
Raw provider observations and filtered canonical results are retained separately;
no scripted reference is evidence of actual model attribution quality.

Full old/new source bytes live under `sources/` as `.py.txt` data; they are not
imported or executed. `sources/manifest.json` attests each source hash and Git blob
identity against the unchanged existing dataset digest algorithm. Diffs contain
real blob headers and are checked against those source bytes. All examples use
the same neutral repository path. Licencing is in `licenses/LICENSE.txt`.
