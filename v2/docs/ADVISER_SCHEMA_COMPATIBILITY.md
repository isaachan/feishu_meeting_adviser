# SQLite schema compatibility

The versioned migration accepts only the following verified, unversioned historical shapes:

| Fixture | Source commit | Result |
| --- | --- | --- |
| `adviser_legacy_base.sql` | `cb22cdcb5efc8c0a09d9568a3859b6694d072448` | v2 |
| `adviser_join_outbox.sql` | `26dc3f43cc66a69a8f5c142e83b8d8edf0243672` | v2 |
| `adviser_materials_unversioned.sql` | `dedb86baff901b237dbd9abdfd377b12d20ef206` | v2 |

The fixtures reproduce the DDL from those commits and contain synthetic records only. New empty databases also migrate from version 0. Adviser versions 1 and 2 are supported; versions greater than 2, unknown tables, missing core columns, and unrecognized unversioned shapes fail without schema changes.

The source database remains at version 5. Its migration chain accepts a new empty database and these verified historical versions:

| Fixture | Source commit | Result |
| --- | --- | --- |
| `source_v1.sql` | `9899f5937016fe271e52917743a44dea218e9553` | v5 |
| `source_v2.sql` | `3afd0885598839ab6f4f95b65faca411f8953cf5` | v5 |
| `source_v3.sql` | `abea6c79e306aa563c5d590e70ae4f88ff1c46ee` | v5 |
| `source_v4.sql` | no released commit; transitional shape accepted by the old v5 initializer | v5 |
| `source_v5.sql` | `36097c43dd4cfc2c8ee2820cfc03c8322f314133` | v5 |

Both fixture families contain synthetic records only. Migrations run as an ordered chain in one transaction, preserve records, and are safe under concurrent startup. Read-only opens require the current schema and never migrate it. Future versions, missing columns or required schema objects, unknown tables, and unrecognized version-0 shapes fail without repair. Product `INSERT` statements name their columns explicitly; an architecture test prevents regressions.
