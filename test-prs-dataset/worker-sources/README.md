# Worker source data bundle

These are inert source inputs, stored as `.py.txt`, never imported or executed.
The original 24-case benchmark and its corpus identity are unchanged. This bundle
has a separate content identity over manifest.json and all attested files.

`provenance.json` and `synthetic-mapping.json` retain the previously verified
preparation bytes unchanged. The six public download records cover two complete
Requests files plus identical LICENSE/NOTICE at parent and head; four physical
upstream/license files suffice. Public code bytes are unchanged Apache-2.0 inputs;
retain licenses/LICENSE and licenses/NOTICE and immutable URLs from provenance.

Synthetic NEW sources are exactly the sole added-file hunk payload bytes of the
unchanged original fixtures; OLD is absent, not an invented empty source file.
No syntax repair, padding or line renumbering is performed. The missing-colon
case intentionally remains invalid Python. Original-to-stored keys containing
`pr.diff::NEW::` identify that declared derivation; other keys identify unchanged
prepared files. The source path in each fixture remains its original `.py` path.
No derived transport diff replaces the original corpus diff; the runtime loader
may apply only the index-identity rewrite attested in synthetic-mapping.json.

The full public Requests modules exceed the production AST node cap. Preserve
and report real degraded context rather than substituting a cropped module.
Local fixture IDs/mapping diagnostics are provenance, not model request metadata.
Simulated wrapper repositories/commits must be identified as synthetic and must
not be represented as upstream Requests repository snapshots.
