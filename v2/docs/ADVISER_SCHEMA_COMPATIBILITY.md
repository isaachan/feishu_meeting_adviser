# Adviser schema compatibility

The versioned migration accepts only the following verified, unversioned historical shapes:

| Fixture | Source commit | Result |
| --- | --- | --- |
| `adviser_legacy_base.sql` | `cb22cdcb5efc8c0a09d9568a3859b6694d072448` | v2 |
| `adviser_join_outbox.sql` | `26dc3f43cc66a69a8f5c142e83b8d8edf0243672` | v2 |
| `adviser_materials_unversioned.sql` | `dedb86baff901b237dbd9abdfd377b12d20ef206` | v2 |

The fixtures reproduce the DDL from those commits and contain synthetic records only. New empty databases also migrate from version 0. Adviser versions 1 and 2 are supported; versions greater than 2, unknown tables, missing core columns, and unrecognized unversioned shapes fail without schema changes.

The source database remains at version 5 and recognizes versions 0–5. Its historical fixture conversion is completed in B3 together with explicit-column INSERT cleanup.
