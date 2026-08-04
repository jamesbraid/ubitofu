# ubitofu Architecture Roadmap

Date: 2026-08-03
Status: superseded

The six phase plans dated 2026-08-03 are retained as review history. They are
not implementation instructions.

The active direction is the single-cutover design in
`docs/superpowers/specs/2026-08-03-ubitofu-0.10-cutover-design.md`:

1. Build the complete replacement architecture on one ubitofu branch.
2. Switch every production command to it once.
3. Release ubitofu 0.10.0 without compatibility paths.
4. Make one Ansible change that adopts 0.10 and deletes the replaced shell
   machinery.

There is no legacy writer, shadow production mode, dual source index, feature
flag, compatibility release, or dual-run consumer period.
