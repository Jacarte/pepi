---
name: wayfinder-notes
description: Write Wayfinder map notes, decision tickets, and implementation tickets in the exact frontmatter form that the Visor graph viewer (~/DEV/visor) reads. Use when creating or editing any Wayfinder note, or when Visor shows a Wayfinder note as unknown or not indexed.
---

# Wayfinder notes that Visor reads

Visor (`~/DEV/visor`) is a read-only graph viewer for Wayfinder folders. It trusts
only explicit, machine-readable frontmatter and never guesses readiness. Write every
Wayfinder note in exactly the form below; other shapes show as unknown or not indexed.

The global `wayfinder` skill plans the map and its tickets. It does not define this
Visor frontmatter; use this skill for the note format.

## Steps

1. Pick the note type: map note, decision ticket, implementation ticket, or other.
2. Create the folder layout: one folder per map, with the map note at its root.
   Tickets and other artifacts go anywhere beneath it (`tickets/`, `specs/`,
   `impl-tickets/`, ...). Open the map folder in Visor; sibling map folders stay one
   level up.
3. Write the frontmatter for the type, using the templates below.
4. Quote every YAML value that contains `:`.
5. Check blockers: each one is a quoted wikilink to a note inside the map folder.
6. Run the Troubleshooting checks if Visor still shows the note as unknown.

## Map note

```yaml
---
title: <Map title>
labels: [wayfinder:map]
tags: [<project>, wayfinder]
status: active
---
```

## Decision ticket (research | prototype | grilling | task)

```yaml
---
title: <Question as a title>
labels: [wayfinder:research]     # or wayfinder:prototype | wayfinder:grilling | wayfinder:task
parent: "[[<map-note-name>]]"    # quoted wikilink to the map; required
status: open                     # exactly `open`, `closed` or `done`
assignee:                        # empty = unassigned; a name = assigned
blocked-by: []                   # always present; [] means none
---
```

With blockers, always quote each wikilink:
`blocked-by: ["[[t04-bulkhead-cap-design]]", "[[t05-rollout]]"]`.

## Implementation ticket (a build slice, not a decision)

```yaml
---
title: <Slice title>
labels: [implementation:ticket]
status: ready-for-agent          # free text; shown, never used for readiness
blocked-by: []
---
```

Visor draws it with the ticket graph but infers no readiness from it. Put real
dependencies in `blocked-by`, not only in the body text.

## Everything else

Specs, briefs, handoffs, indexes and session notes need no Wayfinder fields.
Link them with `[[wikilinks]]` from the map or a ticket and they appear as context
notes. Do not give them a ticket label or the tag `wayfinder-ticket`.

## Rules that avoid the usual failures

- Always quote YAML values that contain `:` (`title: "Agent image: tools"`); an
  unquoted colon makes the whole note invisible to Visor.
- `status` is exactly `open`, `closed` or `done` on decision tickets. Any other value
  (`built-unmerged`, `active`, ...) makes the ticket and everything that depends on it
  `unknown`. Track finer states in the body.
- Blockers are wikilinks to notes inside the map folder. A bare slug, an unquoted
  `[[[a]]]`, or a missing note is flagged "malformed blocker" and the dependent can
  never show as ready.
- `parent` points at exactly one map. Two maps in the same folder cannot be told
  apart, so give each map its own folder.
- `frontier` = open, unassigned, all blockers closed/done. `assigned` = same but with
  an `assignee`. Cycles, unknown statuses and unavailable blockers make a ticket
  `unknown`.

## Troubleshooting

- Note invisible in Visor: check for an unquoted `:` in any value.
- Ticket shows `unknown`: check `status` is exactly `open`, `closed` or `done`, then
  check every blocker resolves to an existing note in the map folder.
- Dependent never shows as ready: a blocker is a bare slug, unquoted, or missing.
- Two maps look merged: move one map into its own folder.
