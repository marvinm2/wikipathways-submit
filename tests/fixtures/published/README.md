# Real published pathways

Verbatim copies of two pathways from `wikipathways/wikipathways-database`, taken from `main` on
2026-08-03. WikiPathways content is CC0, so vendoring them is fine.

| file | title | what it carries |
|---|---|---|
| `WP1518.gpml` | Aspartate biosynthesis | 14 data nodes, 8 interactions, 6 references, 4 ontology terms |
| `WP1487.gpml` | Selenium metabolism | 32 data nodes, 25 interactions, 2 references, 2 ontology terms |

**They are here because the hand-written fixtures kept being weaker than reality.** Five times now
a GPML the portal rendered and validated happily has turned out to be one the content repository's
own reader refuses, or one missing something the repository asks for:

1. the original `validate_gpml` gaps;
2. a missing root `<Graphics>` canvas, which kills the `metadata` job (`gpml.board`);
3. a missing `encoding` declaration, which makes `gpml2pvjson` emit zero bytes and exit 0;
4. an interaction with no `LineThickness`, which kills `metadata` again (issue #26) — and which
   all three `demo/pathway_*.gpml` carried;
5. the quality fixture named `GOOD`, which had no literature references at all, so the test
   asserting a clean pathway reports no problems was passing a file missing something the
   repository's own reviewer checklist asks for (found while fixing issue #27).

Every one of those was a case of two readers of the same format disagreeing, with only the
stricter one on the path to publication. A hand-written fixture encodes what its author already
knew to include, so it can only ever confirm the rules they already thought of. These files encode
what PathVisio actually writes.

**What they are for, and what they are not.** They are the negative control: no rule may report a
`fail` or a `block` on a file the project itself has published. A rule that trips here is wrong
about reality, whatever it says about the schema. They are *not* a claim that either pathway is
exemplary — see `test_no_rule_fails_a_published_pathway` for the warnings they do legitimately
raise, and why those are left standing.

Sampled rather than cherry-picked: the quality ruleset was run over 30 pathways drawn from the
repository, and these two were among the six it reported entirely clean. The other 24 raise real
warnings — most often unannotated data nodes (21 of 30) — which is worth knowing before reading
any rollup over real content as a health score.
