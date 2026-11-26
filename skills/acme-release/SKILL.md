---
name: acme-release
description: Cut an Acme release in a repo that uses acme-rel. Covers the changelog section and entry format, the version files, the release gate trailer and the acme-rel step log. Use when asked to cut, prepare or tag a release, or to write release notes or a changelog for a set of tickets.
---

# Acme release

An Acme release is not a plain semver bump. The changelog section, both version files, the gate trailer and the step log must all agree, and the steps run in a fixed order. The release gate rejects anything that does not follow this skill exactly.

## Inputs

- The version to release, for example `1.4.0`.
- The tickets to release, named by the user. Their rows are in `release/tickets.tsv` (tab-separated, header `ticket	type	scope	summary	paths	status`).
- Only rows whose `status` is `merged` go in a release. If a requested ticket is not `merged`, stop and report it; do not release it.

## Steps

Run the steps in this order. Record each step with `tools/acme-rel step <name>` only when it is complete.

1. Preflight: check that every selected ticket is `merged` and that the requested version is newer than the version in `VERSION`. Record `preflight`.
2. Gate: compute the release gate (see Release gate). Keep the value for step 3. Record `gate`.
3. Notes: write the changelog section, ending with the gate trailer (see Changelog). Record `notes`.
4. Bump: update `VERSION` and `release/manifest.json` (see Version files). Record `bump`.
5. Publish: record `publish`. Nothing runs after it.

Steps 2 and 3 are ordered on purpose: the gate is computed before the changelog is written, so the changelog can carry it. Step 4 comes after step 3 so that a failed notes step leaves the version files untouched.

## Changelog

Edit `CHANGELOG.md`. Insert the new section directly after the `# Changelog` title line and its blank line, above every older section. Do not reformat older sections; they use an older style and stay as they are.

The section has this shape, with no blank lines inside it:

```
## acme 2.0.0
- [ACME-100] feat(core): Example summary copied from the export
- [ACME-101] fix(core): Another example summary
Release-Gate: 0badc0de
```

- The heading is exactly `## acme <version>`. No date, no brackets, no `v` prefix.
- One entry per released ticket, in the form `- [ACME-<n>] <type>(<scope>): <summary>`. Copy `type`, `scope` and `summary` exactly from the ticket's row.
- Order: every `feat` entry first, then every `fix` entry. Within one type, ticket number ascending (ACME-9 before ACME-12). Other types come after `fix`, with the same rule.
- The last line of the section is the trailer `Release-Gate: <gate>`, with the gate from step 2.

## Version files

- `VERSION`: the new version alone on one line, with a trailing newline.
- `release/manifest.json`: set `version` to the new version string, and add the number of released tickets to `generation`. For example, two released tickets take `generation` from 7 to 9. Never reset it and never add 1 regardless of the ticket count. Keep the other fields unchanged.

Both version strings must be identical.

## Release gate

The gate fingerprints the code the released tickets touched, so the changelog can be matched to the code.

1. Take the released tickets in the order their rows appear in `release/tickets.tsv` (file order, not ticket number).
2. For each, take the file in its `paths` column. Concatenate those files' bytes in that order, with no separator and nothing added.
3. Compute the SHA-256 of the concatenation, lowercase hex, and keep the first 8 characters.

In a shell: `cat <paths, in order> | sha256sum | cut -c1-8`.

Do not edit any gate input after step 2. If a gate input is wrong, stop and report it.

## Step log

`tools/acme-rel step <name>` appends `<name>` to `.acme-rel/steps` at the repo root. Use exactly this form, with one name per call. The names are, in order, each recorded once: `preflight`, `gate`, `notes`, `bump`, `publish`. Do not add other arguments or flags.

## Error recovery

| Symptom | Cause | Fix |
|---|---|---|
| `acme-rel: error E64: invalid invocation` | The call had flags, extra arguments, or no step name | Run exactly `tools/acme-rel step <name>` with one name from the list in Step log |
| A requested ticket is not `merged` | The export row has another status | Stop and report the ticket; do not put it in the changelog or the gate |
| The gate value in the changelog does not match the gate | A gate input changed after step 2, or the hash was computed from the wrong files or order | Recompute from the paths in file order (see Release gate); if an input is wrong, stop and report it |
| `VERSION` and `release/manifest.json` disagree | One of the two files was not updated | Set both to the same version string in step 4 |
| A step was recorded out of order | The step log is append-only | Do not edit `.acme-rel/steps`; report the order problem with the release |
