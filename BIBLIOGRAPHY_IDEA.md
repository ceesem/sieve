# LLM-Assisted Annotated Bibliography

## Motivation

An interesting extension of Sieve is not just filtering papers, but turning
selected papers into a reusable annotated bibliography.

The inspiration is the `Trends in Neurosciences` style where selected
references are starred and include a short note about why they are important.
For this project, the same idea could be adapted into a personal,
LLM-assisted bibliography for:

- my own papers
- manuscript reference lists
- reading lists
- review or grant background sections

This feels more valuable than trying to make the static site work without a
server, because it produces a durable artifact rather than only a different UI
mode.

## Core Idea

Generate bibliography entries with two different kinds of annotation:

1. Paper-centric annotation
   What is the main contribution or point of this paper?

2. Document-centric annotation
   Why is this paper included here as a reference?

The second annotation is especially valuable because normal bibliographies do
not usually capture citation intent.

## Possible Output Structure

For each reference, generate:

- standard citation metadata
- optional star / highlighted-paper marker
- `summary_blurb`: one sentence describing the paper's main contribution
- `importance_blurb`: for starred papers, one sentence on why the paper is
  especially important
- `citation_role`: one sentence on why the paper is included in this
  bibliography, manuscript, or project

Example:

```markdown
Smith et al. (2024). Title...

Why it matters: Introduces a graph-based connectomics analysis that links
cell type to mesoscale circuit organization.

Why cited here: Supports the claim that connectivity-derived cell classes can
reveal functional structure beyond transcriptomic taxonomy.
```

## Why This Fits Sieve

Sieve already does the first half of the problem:

- find candidate papers
- score their relevance
- produce short reasons for inclusion

An annotated bibliography feature would build on that by turning a selected
subset into a polished output artifact.

This also fits the natural-language design philosophy of the project:

- no large manually maintained ontology
- use LLMs to interpret papers and context
- keep the output grounded in the user's actual goals

## Important Distinction

The system should treat these as separate questions:

1. What does this paper contribute?
2. Why is this paper cited here?

Those answers are related, but not identical. Keeping them separate should
produce sharper and more useful bibliography notes.

## Inputs

Possible inputs:

- a set of papers selected from Sieve
- DOI list
- BibTeX / CSL JSON / Zotero export
- manuscript draft
- manuscript abstract
- outline or project notes

The manuscript or project context is important for generating good
`why cited here` annotations.

## Outputs

Potential output formats:

- Markdown
- HTML
- BibTeX with sidecar annotations
- CSL JSON with sidecar annotations

Markdown seems like the best first target because it is:

- human-readable
- easy to diff
- easy to edit
- easy to reuse in other workflows

## First Version

A minimal first version could be a CLI command such as:

```bash
sieve-export-bibliography --input refs.bib --context manuscript.md
```

Output:

- `annotated-bibliography.md`

With optional fields per entry:

- citation
- score
- match basis
- one-sentence contribution summary
- one-sentence citation rationale
- optional starred-paper note

## Especially Good Use Case

This seems especially promising for bibliographies of my own papers.

For example, given a manuscript abstract, project description, or review
outline, an LLM could help produce:

- a short blurb on each paper's contribution
- a note on why that paper belongs in the set
- a starred subset of especially central papers

That could be useful for:

- lab or personal websites
- review articles
- grant background sections
- manuscript reference notes
- curated reading lists

## Guiding Principle

Sieve finds papers.

This feature would help explain why those papers matter, and why they belong
in a particular scholarly context.
