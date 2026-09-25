# Room databases — the shared model

The web client's model (`cinny: src/app/features/room-collab/database/dbModel.ts`)
and the agent side (`scripts/room_database.py`) read and write the same shapes.
This file is the contract between them. Change it here first, then in both.

## Where it lives

One room document of kind `db` (`?kind=db`) holds every database in the room,
in five flat maps. Keys are composite ids joined with `|`, so two people
editing different cells never write the same key.

| map | key | value |
|---|---|---|
| `dbs` | `<db>` | `{name, order, template?, created, by}` |
| `props` | `<db>\|<prop>` | `{name, type, order, options?, format?, target?}` |
| `rows` | `<db>\|<row>` | `{order, created, by}` |
| `cells` | `<db>\|<row>\|<prop>` | `{v, updated, by}` (absent = empty) |
| `views` | `<db>\|<view>` | `{name, layout, order, groupBy?, dateProp?, sort?, filter?, hidden?}` |

Order is a number; ties settle by id. `created` and `updated` are epoch
milliseconds; `by` is an mxid.

## Property types and their values (`v`)

| type | v |
|---|---|
| `title`, `text` | string (≤ 20 000 chars) |
| `number` | finite number |
| `checkbox` | boolean |
| `url` | `http(s)://…` only |
| `email` | `x@y.z` |
| `select`, `status` | an option **id** (a write may name the option; it is stored as its id) |
| `multi_select` | option ids, de-duplicated |
| `date` | `{start: "YYYY-MM-DD[THH:MM]", end?}` (a bare date string is accepted and wrapped) |
| `person` | mxids (people **or agents**) |
| `relation` | row ids in `target` (another db id) |
| `files` | `[{name, url}]` |
| `created_time`, `created_by`, `edited_time`, `edited_by` | computed from the row and its cells; never written |

`options` are `[{id, name, color, group?}]`. Colors are gray, brown, orange,
yellow, green, blue, purple, pink and red. On `status`, `group` is `todo`,
`doing` or `done`. A value that does not fit its type is **refused**, never
written; empty (null, "" or []) removes the cell.

## Views

`layout` is one of `table`, `board` (grouped by `groupBy`: a select, status or
person property), `calendar` (by `dateProp`), `list` or `gallery`.

- `sort` is `[{prop, dir: asc|desc}]`, applied in order, then by row order.
- `filter` is `[{prop, op, value?}]`, all of which must hold. `op` is one of
  `is`, `is_not`, `contains`, `empty`, `not_empty`, `gt`, `lt`, `checked` or
  `unchecked`.
- `hidden` lists prop ids.

Moving a card on a board writes the card's group value: select or status →
that option; "No value" → removes it. A card cannot be moved between groups
of a multi-select.

## Agent-side input (not part of the stored contract)

The CLI and the relay take values as typed text and turn them into what
`normalize` accepts before it runs: option names match case-blind, persons and
multi-selects split on commas, `A..B` is a date range, and CSV dates like
`Sep 25` take `--year`. What is stored is exactly the table above.

## Built-in templates

`tasks`, `meetings`, `demo_day` and `wiki`, with the properties and views
defined in `dbModel.ts` `TEMPLATES`. The two sides must stay identical.
